#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
zbx2itop 同步主循环：
  Zabbix host.get 全量 → 名称模式+接口类型分类 → CSV → iTop synchro_exec.php
幂等：对账键=name，重复推送只产生增量更新；未在载荷中的旧设备由
数据源 user_delete_policy=update 标记 obsolete。
凭据来自环境变量（/opt/itop/.env 经 compose 注入）。
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime

ZBX_URL = os.environ.get("ZBX_URL", "").strip()
ZBX_TOKEN = os.environ.get("ZBX_TOKEN", "").strip()
ITOP_URL = os.environ.get("ITOP_URL", "http://itop-web:80").rstrip("/")
ITOP_USER = os.environ.get("ITOP_USER", "admin")
ITOP_PWD = os.environ.get("ITOP_ADMIN_PWD", "")
ITOP_DB_HOST = os.environ.get("ITOP_DB_HOST", "itop-db")
ITOP_DB_NAME = os.environ.get("ITOP_DB_NAME", "itop")
ITOP_DB_USER = os.environ.get("ITOP_DB_USER", "itop")
ITOP_DB_PASS = os.environ.get("ITOP_DB_PASS", "ItopDb#2026")
ORG_NAME = os.environ.get("CMDB_ORG", "ITOPS")
INTERVAL = int(os.environ.get("SYNC_INTERVAL_SECONDS", "14400"))
TIMEOUT = int(os.environ.get("SYNC_HTTP_TIMEOUT", "120"))

PLACEHOLDERS = ("", "CHANGE_ME", "CHANGE_ME_ADMIN_PWD")

# 名称分类规则（v1：组不可靠，用名称+接口类型）
RE_STORAGE = re.compile(r"存储|storage|NAS\b|SAN\b", re.I)
RE_ILO = re.compile(r"\bi?lo\b", re.I)


def log(msg: str):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}][sync] {msg}", flush=True)


def zbx_rpc(method: str, params: dict):
    headers = {"Content-Type": "application/json-rpc"}
    if ZBX_TOKEN:
        headers["Authorization"] = f"Bearer {ZBX_TOKEN}"
    body = json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": 1}).encode()
    req = urllib.request.Request(ZBX_URL, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        j = json.loads(resp.read().decode())
    if "error" in j:
        raise RuntimeError("Zabbix RPC error: " + json.dumps(j["error"]))
    return j["result"]


def itop_rest(operation: str, extra: dict):
    payload = {"operation": operation}
    payload.update(extra)
    data = urllib.parse.urlencode({"version": "1.0", "json_data": json.dumps(payload)}).encode()
    # iTop 3.x REST 仅接受 HTTP Basic 认证（json_data 内嵌凭据会被拒）
    import base64
    req = urllib.request.Request(ITOP_URL + "/webservices/rest.php", data=data)
    req.add_header("Authorization", "Basic " + base64.b64encode(f"{ITOP_USER}:{ITOP_PWD}".encode()).decode())
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        body = resp.read().decode("utf-8", "replace")
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return {"code": -1, "message": "non-JSON: " + body[:300]}


def clean(v: str) -> str:
    return str(v or "").replace(";", " ").replace(",", " ").replace("\r", " ").replace("\n", " ").strip()


def classify(host: dict) -> str:
    name = host.get("name") or host.get("host") or ""
    itfs = host.get("interfaces") or []
    has_agent = any(i.get("type") == "1" for i in itfs)
    if RE_STORAGE.search(name):
        return "StorageSystem"
    if RE_ILO.search(name):
        return "Server"
    if has_agent:
        return "Server"
    return "NetworkDevice"


def main_ip(host: dict) -> str:
    itfs = host.get("interfaces") or []
    for i in itfs:  # agent 优先
        if i.get("type") == "1" and i.get("ip"):
            return i["ip"]
    for i in itfs:
        if i.get("ip"):
            return i["ip"]
    return ""


def os_fields(inv: dict) -> tuple:
    full = clean(inv.get("os_full") or inv.get("os") or "")
    if not full:
        return "", ""
    low = full.lower()
    if low.startswith("linux version"):
        return "Linux", full[:120]
    if "microsoft windows" in low or low.startswith("windows"):
        return "Windows", full[:120]
    if "vmware" in low or "esxi" in low:
        return "VMware ESXi", full[:120]
    return "Linux", full[:120]  # 默认按 Linux 家族（存量环境以 Linux 为主）


def status_of(host: dict) -> str:
    return "production" if str(host.get("status")) == "0" else "inactive"


def build_rows(hosts: list, cls: str) -> list:
    rows = []
    for h in hosts:
        if classify(h) != cls:
            continue
        inv = h.get("inventory") or {}
        name = clean(h.get("name") or h.get("host"))
        serial = re.sub(r"^\s*SN\s*[:#\s]\s*", "", str(inv.get("serialno_a") or "")).strip()
        base = {
            "name": name,
            "org_id": ORG_NAME,
            "status": status_of(h),
            "managementip": clean(main_ip(h)),
            "serial_number": clean(serial),
            "model_id": clean(inv.get("model")),
        }
        if cls == "Server":
            fam, ver = os_fields(inv)
            base["os_family"] = fam
            base["os_version"] = ver
        rows.append(base)
    rows.sort(key=lambda r: r["name"])
    return rows


def build_csv(rows: list, cols: list) -> str:
    header = ["primary_key::name"] + [c for c in cols if c != "name"]

    def val(r: dict, c: str) -> str:
        return clean(r["name"]) if c == "primary_key::name" else clean(r.get(c))

    lines = [";".join(header)]
    for r in rows:
        lines.append(";".join(val(r, c) for c in header))
    return "\n".join(lines) + "\n"


def q(v: str) -> str:
    """SQL 字符串转义"""
    return str(v or "").replace("\\", "\\\\").replace("'", "\\'")


def mysql_exec(sql: str) -> str:
    """执行 SQL 批（密码走 MYSQL_PWD 环境变量，不进 argv）"""
    env = dict(os.environ, MYSQL_PWD=ITOP_DB_PASS)
    p = subprocess.run(
        ["mysql", "-h", ITOP_DB_HOST, "-u", ITOP_DB_USER, "--default-character-set=utf8mb4", ITOP_DB_NAME],
        input=sql.encode(), capture_output=True, timeout=TIMEOUT, env=env)
    if p.returncode != 0:
        raise RuntimeError("mysql failed: " + p.stderr.decode(errors="replace")[:300])
    return p.stdout.decode(errors="replace")


def push(ds_name: str, rows: list, cols: list) -> bool:
    """官方采集器模式：存在的暂存行 UPDATE（触摸 last_seen）、新行 INSERT；
    消失的行不动——引擎 Job1 按 delete_policy 自动淘汰。"""
    if not rows:
        log(f"{ds_name}: no rows, skip")
        return True

    # 解析数据源 id 与暂存表名
    r = itop_rest("core/get", {"class": "SynchroDataSource",
                               "key": f"SELECT SynchroDataSource WHERE name='{ds_name}'"})
    objs = r.get("objects") or {}
    if not objs:
        log(f"{ds_name}: data source NOT FOUND — run 'docker compose up provision'")
        return False
    _k, v = list(objs.items())[0]
    ds_id = v.get("id")
    table = v.get("fields", {}).get("database_table_name", "")
    if not table:
        log(f"{ds_name}: database_table_name empty for id={ds_id}")
        return False

    # 暂存表内已存在的对账键
    existing = {ln.strip() for ln in mysql_exec(
        f"SELECT primary_key FROM `{table}`;").splitlines() if ln.strip()}

    data_cols = [c for c in cols if c != "name"]
    stmts = []
    n_ins = n_upd = 0
    for row in rows:
        pk = row["name"]
        vals = {c: row[c] for c in data_cols}
        col_list = ", ".join(["`primary_key`"] + [f"`{c}`" for c in data_cols])
        val_list = ", ".join(["'" + q(pk) + "'"] + ["'" + q(vals[c]) + "'" for c in data_cols])
        if pk in existing:
            set_clause = ", ".join([f"`{c}`='" + q(vals[c]) + "'" for c in data_cols])
            stmts.append(f"UPDATE `{table}` SET {set_clause} WHERE `primary_key`='" + q(pk) + "';")
            n_upd += 1
        else:
            stmts.append(f"INSERT INTO `{table}` ({col_list}) VALUES ({val_list});")
            n_ins += 1

    # 分批执行（每批 500 条语句，避免单次管道过大）
    BATCH = 500
    for i in range(0, len(stmts), BATCH):
        mysql_exec("\n".join(stmts[i:i + BATCH]))
    log(f"{ds_name}: staged rows -> insert={n_ins}, update={n_upd}, "
        f"stale(untouched)={max(0, len(existing) - n_upd)}")

    # 触发同步执行（synchro_exec.php 仅执行暂存表已装载的数据）
    form = {"auth_user": ITOP_USER, "auth_pwd": ITOP_PWD, "data_sources": str(ds_id)}
    data = urllib.parse.urlencode(form).encode()
    import base64
    req = urllib.request.Request(ITOP_URL + "/synchro/synchro_exec.php", data=data)
    req.add_header("Authorization", "Basic " + base64.b64encode(f"{ITOP_USER}:{ITOP_PWD}".encode()).decode())
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        body = resp.read().decode("utf-8", "replace")
    body = re.sub(r"<script.*?</script>", " ", body, flags=re.S)
    body = re.sub(r"<[^>]+>", " ", body)

    def stat(label: str) -> str:
        m = re.search(label + r":\s*(\d+)", body)
        return m.group(1) if m else "?"

    err = re.search(r"ERROR:\s*([^\n<]{0,200})", body)
    created = stat("Objects created")
    updated = stat("Objects updated")
    obsoleted = stat("Objects obsoleted")
    errors = int(stat("Objects creation errors")) + int(stat("Objects update errors")) + \
        int(stat("Objects reconciliation errors"))
    log(f"{ds_name}: executed -> created={created}, updated={updated}, "
        f"obsoleted={obsoleted}, errors={errors}" + (f" | {err.group(1).strip()}" if err else ""))
    ok = err is None and (created != "?" or updated != "?")
    return ok


def run_once() -> bool:
    hosts = zbx_rpc("host.get", {
        "output": ["host", "name", "status"],
        "selectGroups": ["name"],
        "selectInterfaces": ["type", "ip", "main"],
        "selectInventory": ["os", "os_full", "serialno_a", "model", "vendor", "location"],
    })
    log(f"zabbix: {len(hosts)} hosts pulled")

    if ITOP_PWD in PLACEHOLDERS:
        log("SKIPPED push: ITOP_ADMIN_PWD not set — fill /opt/itop/.env and 'docker compose restart sync'")
        return True

    ok = True
    ok &= push("zbx-servers", build_rows(hosts, "Server"),
               ["name", "org_id", "status", "managementip", "os_family", "os_version", "serial_number", "model_id"])
    ok &= push("zbx-network", build_rows(hosts, "NetworkDevice"),
               ["name", "org_id", "status", "managementip", "serial_number", "model_id"])
    ok &= push("zbx-storage", build_rows(hosts, "StorageSystem"),
               ["name", "org_id", "status", "managementip", "serial_number", "model_id"])
    return ok


def main():
    if not ZBX_URL:
        log("FATAL: ZBX_URL not set")
        sys.exit(1)
    while True:
        try:
            ok = run_once()
            log(f"cycle finished ok={ok}, next in {INTERVAL}s")
        except Exception as e:  # noqa: BLE001
            log(f"cycle FAILED: {e}; retry in {INTERVAL}s")
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
