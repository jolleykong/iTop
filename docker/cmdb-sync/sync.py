#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
zbx2itop 同步主循环 v2：
  Zabbix host.get 全量（含模板/tags）→ 模板证据优先分类 → CSV → iTop synchro_exec.php

v2 分类规则（按优先级，模板名为硬证据）：
  1. VMware Guest                    -> VirtualMachine（vCenter 自动发现，确证）
  2. Oracle by ODBC / backup checker / MySQL by ODBC -> DBServer（实例视角 host）
  3. Cisco/F5/Brocade/ASA 等 SNMP    -> NetworkDevice
  4. XSKY/OceanStor/Unity/深信服     -> StorageSystem
  5. BMC/iDRAC/iLO/IMM/PowerEdge/ProLiant/Supermicro/Hypervisor/ESXi -> Server（物理机/宿主）
     - svr-{ilo|idrac|bmc|imm}-IP-OS名 命名的 BMC host：解析出 OS 名，若本轮存在同名
       Server CI 行则合并（serial/model 充实目标行），BMC host 不单独建 CI；
       关联失败则自行建 Server
  6. 无任何模板证据（agent-only/裸 host）-> Server + description "phys:unverified"
     （等 Zabbix 侧补 systemd-detect-virt / inventory.type 证据后自动迁移）

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
# 暂存区位置（引擎不管理 location：staged 该字段恒空 + 空值不覆盖规则，
# 故位置标记由每轮引擎外对账维护，跟随 phys:unverified 判定自动进出）
STAGING_LOCATION_NAME = os.environ.get("STAGING_LOCATION_NAME", "ZBX待分类暂存区")
INTERVAL = int(os.environ.get("SYNC_INTERVAL_SECONDS", "14400"))
TIMEOUT = int(os.environ.get("SYNC_HTTP_TIMEOUT", "120"))

PLACEHOLDERS = ("", "CHANGE_ME", "CHANGE_ME_ADMIN_PWD")
# VirtualMachine.virtualhost_id 为必填外键；Zabbix vCenter 发现不带宿主信息，
# 暂统一归属占位宿主（provision 会确保该 Server 存在），宿主线索补齐后 v3 关联真实宿主
VM_HOST_PLACEHOLDER = os.environ.get("VM_HOST_PLACEHOLDER", "ZBX-VM-Unassigned-Host")
# 公有云资源（virt tag = ecs 等云类型）的宿主桶：宿主为 Cloud 平台而非 Hypervisor
CLOUD_PLACEHOLDER = os.environ.get("CLOUD_PLACEHOLDER", "ZBX-Cloud-Unassigned")
# DBServer.system_id 为必填外键；承载 OS host 未纳管/命名无关时挂占位桶，
# Zabbix 侧补齐 OS 纳管或 {$ORACLE.HOST} 规范后自动重新挂接
DB_HOST_PLACEHOLDER = os.environ.get("DB_HOST_PLACEHOLDER", "ZBX-DB-Host-Unassigned")

# 名称兜底规则（仅无模板证据的 host）
RE_STORAGE = re.compile(r"存储|storage|NAS\b|SAN\b", re.I)
# BMC host 命名钥匙：svr-iLo-{带外IP}-{OS主机名}（146/148 硬件 host 符合）
RE_BMC_NAME = re.compile(r"^svr-(ilo|idrac|bmc|imm)-(\d+(?:\.\d+){3})-(.+)$", re.I)

# 模板名 → 类别（按序匹配，先命中先定类）
TPL_PATTERNS = [
    ("VirtualMachine", re.compile(r"^VMware Guest$", re.I), False),
    ("DBServer", re.compile(r"^Oracle by ODBC|Oracle backup checker|MySQL by ODBC", re.I), False),
    ("NetworkDevice", re.compile(r"Cisco|\bASA\b|Router|\bF5\b|Brocade|\bSwitch\b|Juniper|Generic by SNMP", re.I), False),
    ("StorageSystem", re.compile(r"XSKY|OceanStor|Unity|SANGFOR|Storage|\bNAS\b", re.I), False),
    ("Server", re.compile(r"Hypervisor|ESXi", re.I), False),  # 虚拟化宿主
    ("Server", re.compile(r"BMC|iDRAC|\biLO\b|\bIMM\b|PowerEdge|ProLiant|Supermicro", re.I), True),  # BMC
]


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


def tpl_class(host: dict) -> tuple:
    """按模板名证据定类。返回 (cls, src_note, is_bmc)，无证据返回 (None, '', False)。"""
    for t in host.get("parentTemplates") or []:
        tname = t.get("name") or ""
        for cls, rex, is_bmc in TPL_PATTERNS:
            if rex.search(tname):
                return cls, "tpl:" + tname, is_bmc
    return None, "", False


def main_ip(host: dict) -> str:
    # iTop managementip 仅接受 IPv4 点分格式，跳过 IPv6（如 fe80:: 链路本地地址）
    itfs = host.get("interfaces") or []
    for i in itfs:  # agent 优先
        ip = (i.get("ip") or "").strip()
        if i.get("type") == "1" and ip and ":" not in ip:
            return ip
    for i in itfs:
        ip = (i.get("ip") or "").strip()
        if ip and ":" not in ip:
            return ip
    return ""


def status_of(host: dict) -> str:
    # iTop FunctionalCI status 枚举: stock/implementation/production/obsolete（无 inactive）
    return "production" if str(host.get("status")) == "0" else "stock"


def clean_serial(inv: dict) -> str:
    return re.sub(r"^\s*SN\s*[:#\s]\s*", "", str(inv.get("serialno_a") or "")).strip()


def virt_evidence(h: dict, tg: dict) -> tuple:
    """v3 双来源判定物理性（覆盖 agent 关机/未装 agent 场景）：
    来源1（自动，优先）：inventory.type —— hw.virt item 上报的物理性词表。
      inventory 是最后已知值持久化，设备关机后仍保留，只对"从未采集过"的设备失效。
      词表宽松匹配（contains，大小写不敏感）：兼容 "Physical server"、
      "Physical (physical)"、"Virtual machine"、"Virtual machine (virtual)" 等
      item 侧各种写法；"unknown"、"Rack Mount Chassis" 等无关键词值不判定。
    来源2（手工兜底）：host tag `virt`，值 physical|virtual|ecs（大小写不敏感），
      用于新上架/老 agent 未部署 hw_serial.conf 的设备人工标注，以及公有云资源
      类型指定（inv 来源无云概念，tag:ecs 比 inv 的 generic virtual 更具体，
      故 ecs 优先于 inv）。
    返回 (physical|virtual|virtual-ecs|None, 来源note)。"""
    t_all = {str(k).strip().lower(): str(v or "").strip().lower() for k, v in tg.items()}
    if t_all.get("virt") == "ecs":
        return "virtual-ecs", "virt:tag-ecs"
    it = clean((h.get("inventory") or {}).get("type")).lower()
    if "physical" in it:
        return "physical", "phys:inv-virt"
    if "virtual" in it:
        return "virtual", "virt:inv-virt"
    t = (tg.get("virt") or "").strip().lower()
    if t == "physical":
        return "physical", "phys:tag-virt"
    if t == "virtual":
        return "virtual", "virt:tag-virt"
    return None, None


FALLBACK_BRAND = os.environ.get("FALLBACK_BRAND", "Generic")


def build_dictionary(hosts: list, by_cls: dict) -> tuple:
    """v3.5: 从 inventory vendor/model 构造 Brand/Model 字典行。
    Server.model_id 是指向 Model 类的外键（按 name 解析），推送前 Model 行必须存在，
    否则引擎 FK 解析失败留空（此前 467/469 台 model 空的根因）。
    同一 model 文本出现多个 vendor 时取多数票；无 vendor 的 fallback Generic。
    Model.type（设备类型，必填枚举）按资产分类多数票：Server/NetworkDevice/StorageSystem。
    Brand/Model 是 Typology 全局字典，无 org_id 字段。"""
    vendors: dict = {}
    types: dict = {}
    for h in hosts:
        inv = h.get("inventory") or {}
        mdl = clean(inv.get("model"))
        if not mdl:
            continue
        vnd = clean(inv.get("vendor")) or FALLBACK_BRAND
        vendors.setdefault(mdl, {})
        vendors[mdl][vnd] = vendors[mdl].get(vnd, 0) + 1
    for cls, tval in (("Server", "Server"), ("NetworkDevice", "NetworkDevice"),
                      ("StorageSystem", "StorageSystem")):
        for row in by_cls.get(cls, []):
            mdl = clean(row.get("model_id"))
            if mdl:
                types.setdefault(mdl, {})
                types[mdl][tval] = types[mdl].get(tval, 0) + 1
    brands = {FALLBACK_BRAND}
    models = []
    for mdl, vs in sorted(vendors.items()):
        vendor = max(vs.items(), key=lambda kv: (kv[1], kv[0]))[0]
        brands.add(vendor)
        tval = max(types[mdl].items(), key=lambda kv: kv[1])[0] if mdl in types else "Server"
        models.append({"name": mdl, "brand_id": vendor, "type": tval})
    brand_rows = [{"name": b} for b in sorted(brands)]
    return brand_rows, models


def base_row(h: dict, name: str, cls: str, notes: list) -> dict:
    inv = h.get("inventory") or {}
    row = {
        "name": name,
        "org_id": ORG_NAME,
        "status": status_of(h),
        "managementip": clean(main_ip(h)),
        "serialnumber": clean_serial(inv),
        "model_id": clean(inv.get("model")),
        # v3.6: Server/PhysicalDevice 有独立品牌字段（前端"品牌"列），
        # vendor 直填；空值 staged 不覆盖，待 hw.vendor 采集后自动归位
        "brand_id": clean(inv.get("vendor")),
        "_notes": notes,
        "_cls": cls,
    }
    if cls == "NetworkDevice":
        row["networkdevicetype_id"] = os.environ.get("NET_DEVICE_TYPE", "Switch")
    return row


RE_DB_TPL = re.compile(r"^Oracle by ODBC|Oracle backup checker|MySQL by ODBC", re.I)


def is_db_host(h: dict) -> bool:
    return any(RE_DB_TPL.search(t.get("name") or "") for t in h.get("parentTemplates") or [])


def build_oracle_map(hosts: list) -> dict:
    """解析 DB 实例 host -> 承载 OS host 的 name。
    双通道：{$ORACLE.HOST} 宏值 + DB host 自身接口 IP（Zabbix 侧 ODBC host
    常直接复用 OS IP 建 agent 接口）。映射表排除 BMC 与 DB host 自身。"""
    ip2name, name2name = {}, {}
    for h in hosts:
        n = clean(h.get("name") or h.get("host"))
        if RE_BMC_NAME.match(n) or is_db_host(h):
            continue
        for i in h.get("interfaces") or []:
            ip = (i.get("ip") or "").strip()
            if ip and ":" not in ip:
                ip2name.setdefault(ip, n)
        name2name.setdefault(n.lower(), n)
        name2name.setdefault(clean(h.get("host")).lower(), n)

    macro_by_host = {}
    for mac in zbx_rpc("usermacro.get", {
        "output": ["macro", "value"],
        "selectHosts": ["host", "name"],
        "filter": {"macro": ["{$ORACLE.HOST}"]},
    }):
        db_host = clean((mac.get("hosts") or [{}])[0].get("name"))
        v = clean(mac.get("value"))
        if db_host and v and "<" not in v:  # <Put oracle host here> 为模板占位值
            macro_by_host.setdefault(db_host, v)

    m = {}
    for h in hosts:
        if not is_db_host(h):
            continue
        name = clean(h.get("name") or h.get("host"))
        cands = []
        if macro_by_host.get(name):
            cands.append(macro_by_host[name])
        itf_ip = main_ip(h)
        if itf_ip:
            cands.append(itf_ip)
        for c in cands:
            tgt = ip2name.get(c) or name2name.get(c.lower())
            if tgt and tgt != name:
                m[name] = tgt
                break
    return m


def build_all(hosts: list, oracle_map: dict = None) -> tuple:
    """分类 + BMC 关联合并。返回 {cls: [rows]} 及统计。"""
    stats = {"merged_bmc": [], "unlinked_bmc": [], "counts": {}}
    by_cls = {"Server": [], "VirtualMachine": [], "DBServer": [],
              "NetworkDevice": [], "StorageSystem": []}

    for h in hosts:
        name = clean(h.get("name") or h.get("host"))
        if not name:
            continue
        tg = {t.get("tag"): t.get("value") for t in h.get("tags") or []}
        cls, src, is_bmc = tpl_class(h)
        notes = [src] if src else []

        if cls is None:
            # 无模板证据：v3 双来源判定（inv hw.virt 优先，tag virt 手工兜底），
            # 两来源均无 → 保守 Server 暂挂（phys:unverified）
            if RE_STORAGE.search(name):
                cls = "StorageSystem"
            else:
                verdict, vsrc = virt_evidence(h, tg)
                if verdict is None:
                    cls = "Server"
                    notes.append("phys:unverified")
                else:
                    cls = ("VirtualMachine" if verdict.startswith("virtual")
                           else "Server")
                    notes.append(vsrc)
        if cls == "Server" and tg.get("类型") == "DB":
            notes.append("tag:类型=DB")
        if cls == "VirtualMachine":
            notes.append("phys:virtual-confirmed")
        row = base_row(h, name, cls, notes)
        row["description"] = clean("zbx: " + "; ".join(n for n in notes if n))
        if cls == "VirtualMachine":
            # tag 判定的公有云资源挂 Cloud 占位桶，其余挂 Hypervisor 占位桶
            row["virtualhost_id"] = (CLOUD_PLACEHOLDER if any("ecs" in n for n in notes)
                                     else VM_HOST_PLACEHOLDER)
        if cls == "DBServer":
            row["system_id"] = (oracle_map or {}).get(name) or DB_HOST_PLACEHOLDER
        by_cls[cls].append(row)

    # BMC host 关联合并：svr-iLo-IP-OS名 -> 同名 Server 行（OS host 视角）
    def is_bmc_row(r):
        return any(RE_BMC_NAME.match(r["name"]) and n.startswith("tpl:") and
                   re.search(r"BMC|iDRAC|iLO|IMM|PowerEdge|ProLiant|Supermicro", n, re.I)
                   for n in r["_notes"])

    keep_servers, bmc_rows = [], []
    for r in by_cls["Server"]:
        (bmc_rows if is_bmc_row(r) else keep_servers).append(r)
    server_by_name = {r["name"]: r for r in keep_servers}

    for b in bmc_rows:
        m = RE_BMC_NAME.match(b["name"])
        tgt = server_by_name.get(m.group(3).strip()) if m else None
        if tgt is not None:
            if not tgt.get("serialnumber") and b.get("serialnumber"):
                tgt["serialnumber"] = b["serialnumber"]
            if not tgt.get("model_id") and b.get("model_id"):
                tgt["model_id"] = b["model_id"]
            # v3.6: OS 侧 vendor 空时用 BMC 侧品牌回填（BMC SNMP 普遍带 vendor）
            if not tgt.get("brand_id") and b.get("brand_id"):
                tgt["brand_id"] = b["brand_id"]
            # 带外信息落点：iLo/iDRAC IP 写入 description，前端可见可搜
            # （iTop 无原生 BMC 类，合并模式下 IP 是唯一的带外承载点）
            tgt["_notes"].append(
                f"bmc:{m.group(1).lower()}:{m.group(2)}")
            tgt["description"] = clean(
                "zbx: " + "; ".join(n for n in tgt["_notes"] if n))
            stats["merged_bmc"].append(f"{b['name']} -> {m.group(3)}")
        else:
            keep_servers.append(b)
            stats["unlinked_bmc"].append(b["name"])
    by_cls["Server"] = sorted(keep_servers, key=lambda r: r["name"])
    by_cls["VirtualMachine"].sort(key=lambda r: r["name"])
    by_cls["DBServer"].sort(key=lambda r: r["name"])
    by_cls["NetworkDevice"].sort(key=lambda r: r["name"])
    by_cls["StorageSystem"].sort(key=lambda r: r["name"])

    for cls, rows in by_cls.items():
        stats["counts"][cls] = len(rows)
    # v3 物理性判定来源细分（Server 类 unverified 暂挂量是治理重点）
    for cls, rows in by_cls.items():
        if cls != "Server":
            continue
        stats["counts"]["Server_unverified"] = sum(
            1 for r in rows if "phys:unverified" in r["_notes"])
        stats["counts"]["Server_by_tag"] = sum(
            1 for r in rows if "phys:tag-virt" in r["_notes"])
        stats["counts"]["Server_by_inv"] = sum(
            1 for r in rows if "phys:inv-virt" in r["_notes"])
    return by_cls, stats


def split_dbserver_unlinked(rows: list) -> tuple:
    """统计 DBServer 挂接情况：已挂真主机 vs 落占位桶。"""
    linked = [r for r in rows if r.get("system_id") != DB_HOST_PLACEHOLDER]
    unlinked = [r["name"] for r in rows if r.get("system_id") == DB_HOST_PLACEHOLDER]
    return linked, unlinked


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
    """增量装载（官方采集器模式）：存在暂存行 UPDATE（touch last_seen）、新行 INSERT。
    stale 淘汰由引擎 Job1 依据 full_load_periodicity（provision 设置，与
    SYNC_INTERVAL_SECONDS 一致：开发期 600s / 生产 14400s）判定：
    一个窗口内未被任何轮次 touch 的 replica 判为 disappeared -> delete_policy=update
    标 obsolete。增量模式避免 replica 表膨胀（全量 TRUNCATE+INSERT 每轮都会新建 replica）。"""
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
    k, v = list(objs.items())[0]
    ds_id = v.get("id") or str(k).split("::")[-1]
    table = (v.get("fields") or {}).get("database_table_name", "")
    if not table:
        log(f"{ds_name}: database_table_name empty for id={ds_id}")
        return False

    # 暂存表内已存在的对账键
    existing = {ln.strip() for ln in mysql_exec(
        f"SELECT primary_key FROM `{table}`;").splitlines() if ln.strip()}

    # 暂存表必须含 name 列（引擎对账读暂存表属性列，primary_key 只是副本列）
    data_cols = ["name"] + [c for c in cols if c != "name"]
    stmts = []
    n_ins = n_upd = 0
    for row in rows:
        pk = row["name"]
        vals = {c: row.get(c, "") for c in data_cols}
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
        f"stale(untouched {INTERVAL}s) will be obsoleted by engine")

    # 触发同步执行（synchro_exec.php 仅执行暂存表已装载的数据）
    # 不传 max_chunk_size：分块模式仅支持 CLI；HTTP 模式整体执行（web memory_limit=1024M）
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

    def num(label: str) -> int:
        try:
            return int(stat(label))
        except ValueError:
            return 0

    errors = num("Objects creation errors") + num("Objects update errors") + \
        num("Objects reconciliation errors")
    log(f"{ds_name}: executed -> created={created}, updated={updated}, "
        f"obsoleted={obsoleted}, errors={errors}" + (f" | {err.group(1).strip()}" if err else ""))
    ok = err is None and (created != "?" or updated != "?")
    return ok


def itop_db_sql(sql: str) -> bool:
    """mysql CLI 直连 iTop 库执行 SQL（引擎外对账用，如暂存区位置）"""
    try:
        r = subprocess.run(
            ["mysql", "-h", os.environ.get("ITOP_DB_HOST", "itop-db"),
             "-u", os.environ.get("ITOP_DB_USER", "itop"),
             f"-p{os.environ.get('ITOP_DB_PASS', '')}",
             "--default-character-set=utf8mb4",
             os.environ.get("ITOP_DB_NAME", "itop"), "-N", "-e", sql],
            capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            log(f"itop-db sql FAILED: {(r.stderr or '').strip()[:200]}")
            return False
        return True
    except Exception as e:  # noqa: BLE001
        log(f"itop-db sql ERROR: {e}")
        return False


def reconcile_staging_location():
    """暂存区位置对账：location 跟随 phys:unverified 判定自动进出。
    - 已归位（description 不再含 phys:unverified）→ 位置清空
    - 新增 unverified 的在册物理设备 → 挂入暂存位置
    - obsolete 遗留对象不参与（前端默认隐藏）"""
    if not os.environ.get("ITOP_DB_PASS") or \
            os.environ.get("ITOP_DB_PASS") in PLACEHOLDERS:
        return
    q = lambda s: s.replace("'", "''")
    sql = f"""
    SELECT id FROM location WHERE name='{q(STAGING_LOCATION_NAME)}'
      AND org_id=(SELECT id FROM organization WHERE name='{q(ORG_NAME)}')
    """
    try:
        r = subprocess.run(
            ["mysql", "-h", os.environ.get("ITOP_DB_HOST", "itop-db"),
             "-u", os.environ.get("ITOP_DB_USER", "itop"),
             f"-p{os.environ.get('ITOP_DB_PASS', '')}",
             "--default-character-set=utf8mb4",
             os.environ.get("ITOP_DB_NAME", "itop"), "-N", "-e", sql],
            capture_output=True, text=True, timeout=60)
        loc_id = (r.stdout or "").strip()
    except Exception as e:  # noqa: BLE001
        log(f"staging-reconcile: locate id ERROR: {e}")
        return
    if not loc_id.isdigit():
        log(f"staging-reconcile: location '{STAGING_LOCATION_NAME}' not found, skip")
        return
    ok = itop_db_sql(f"""
    UPDATE physicaldevice p JOIN functionalci f ON f.id=p.id
    SET p.location_id = IF(f.description LIKE '%phys:unverified%', {loc_id}, 0)
    WHERE f.finalclass='Server'
      AND f.org_id=(SELECT id FROM organization WHERE name='{q(ORG_NAME)}')
      AND p.status != 'obsolete'
      AND (p.location_id={loc_id} OR f.description LIKE '%phys:unverified%')
    """)
    if ok:
        log(f"staging-reconcile: location '{STAGING_LOCATION_NAME}' synced (id={loc_id})")


def run_once() -> bool:
    hosts = zbx_rpc("host.get", {
        "output": ["host", "name", "status"],
        "selectGroups": ["name"],
        "selectInterfaces": ["type", "ip", "main"],
        "selectInventory": ["os", "os_full", "serialno_a", "model", "vendor", "location", "type"],
        "selectParentTemplates": ["name"],
        "selectTags": ["tag", "value"],
    })
    log(f"zabbix: {len(hosts)} hosts pulled")

    oracle_map = build_oracle_map(hosts)
    log(f"oracle hosts resolved via {{$ORACLE.HOST}}/ip: {len(oracle_map)}")

    by_cls, stats = build_all(hosts, oracle_map)
    log("v2 分类统计: " + json.dumps(stats["counts"], ensure_ascii=False))
    log(f"BMC 关联合并 {len(stats['merged_bmc'])} 台: " +
        ", ".join(stats["merged_bmc"][:8]) + (" ..." if len(stats["merged_bmc"]) > 8 else ""))
    log(f"BMC 未关联(自建 Server) {len(stats['unlinked_bmc'])} 台")

    if ITOP_PWD in PLACEHOLDERS:
        log("SKIPPED push: ITOP_ADMIN_PWD not set — fill /opt/itop/.env and 'docker compose restart sync'")
        return True

    db_linked, db_unlinked = split_dbserver_unlinked(by_cls["DBServer"])
    log(f"DBServer 挂接: {len(db_linked)} 台解析到承载主机, {len(db_unlinked)} 台落占位桶")
    if db_unlinked:
        log(f"DBServer 待治理(承载 OS 未纳管): {', '.join(db_unlinked[:12])}"
            + (f" ... 共{len(db_unlinked)}台" if len(db_unlinked) > 12 else ""))

    ok = True
    # v3.5: 字典先行——Brand/Model 先于资产推送并立即执行，
    # 否则同轮 servers 的 model_id 外键按 name 解析找不到目标行
    brand_rows, model_rows = build_dictionary(hosts, by_cls)
    log(f"字典: Brand {len(brand_rows)} / Model {len(model_rows)}")
    ok &= push("zbx-brands", brand_rows, ["name"])
    ok &= push("zbx-models", model_rows, ["name", "type", "brand_id"])
    # v2: 模板证据优先分类；v2.1: DB 实例挂承载主机、VM 挂占位宿主、全量装载启用 obsolete 判定
    ok &= push("zbx-vms", by_cls["VirtualMachine"],
               ["name", "org_id", "status", "managementip", "description", "virtualhost_id"])
    ok &= push("zbx-servers", by_cls["Server"],
               ["name", "org_id", "status", "managementip", "serialnumber", "brand_id", "model_id", "description"])
    ok &= push("zbx-dbservers", by_cls["DBServer"], ["name", "org_id", "system_id"])
    ok &= push("zbx-network", by_cls["NetworkDevice"],
               ["name", "org_id", "status", "managementip", "serialnumber", "brand_id", "model_id", "networkdevicetype_id"])
    ok &= push("zbx-storage", by_cls["StorageSystem"],
               ["name", "org_id", "status", "managementip", "serialnumber", "brand_id", "model_id"])

    reconcile_staging_location()
    return ok


def main():
    if not ZBX_URL:
        log("FATAL: ZBX_URL not set")
        sys.exit(1)
    # --once: 单次运行后退出（手动触发刷新用，不进入定时循环）
    once = "--once" in sys.argv
    while True:
        try:
            ok = run_once()
            log(f"cycle finished ok={ok}"
                + ("" if once else f", next in {INTERVAL}s"))
        except Exception as e:  # noqa: BLE001
            log(f"cycle FAILED: {e}; retry in {INTERVAL}s")
            if once:
                sys.exit(1)
        if once:
            break
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
