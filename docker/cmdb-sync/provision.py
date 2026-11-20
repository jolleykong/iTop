#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
itop-sync 一次性供给：通过 iTop REST API 创建
  1) Organization（如不存在）
  2) 三个 Synchro Data Source（zbx-servers / zbx-network / zbx-storage）
凭据来自环境变量（compose 注入 /opt/itop/.env）。
缺 ITOP_ADMIN_PWD 时跳过并给出配置指引，不视为错误。
"""
import json
import os
import sys
import urllib.parse
import urllib.request

ITOP_URL = os.environ.get("ITOP_URL", "http://itop-web:8081").rstrip("/")
ADMIN_USER = os.environ.get("ITOP_USER", "admin")
ADMIN_PWD = os.environ.get("ITOP_ADMIN_PWD", "")
ORG_NAME = os.environ.get("CMDB_ORG", "ITOPS")

PLACEHOLDERS = ("", "CHANGE_ME", "CHANGE_ME_ADMIN_PWD")

# ── 数据源定义（目标类 + 属性映射；reconcile=1 仅用于主键列）──────────────
# attribute_list 条目: {attcode, reconcile, update, target_attcode(仅外键)}
DS_DEFS = [
    {
        "name": "zbx-servers",
        "scope_class": "Server",
        "desc": "Zabbix host sync -> Server (agent hosts + iLo hardware)",
        "attrs": [
            {"attcode": "name", "reconcile": 1, "update": 0},
            {"attcode": "org_id", "reconcile": 0, "update": 1, "target_attcode": "name"},
            {"attcode": "status", "reconcile": 0, "update": 1},
            {"attcode": "managementip", "reconcile": 0, "update": 1},
            {"attcode": "os_family", "reconcile": 0, "update": 1},
            {"attcode": "os_version", "reconcile": 0, "update": 1},
            {"attcode": "serial_number", "reconcile": 0, "update": 1},
            {"attcode": "model_id", "reconcile": 0, "update": 1, "target_attcode": "name"},
        ],
    },
    {
        "name": "zbx-network",
        "scope_class": "NetworkDevice",
        "desc": "Zabbix host sync -> NetworkDevice (SNMP-only hosts)",
        "attrs": [
            {"attcode": "name", "reconcile": 1, "update": 0},
            {"attcode": "org_id", "reconcile": 0, "update": 1, "target_attcode": "name"},
            {"attcode": "status", "reconcile": 0, "update": 1},
            {"attcode": "managementip", "reconcile": 0, "update": 1},
            {"attcode": "serial_number", "reconcile": 0, "update": 1},
            {"attcode": "model_id", "reconcile": 0, "update": 1, "target_attcode": "name"},
        ],
    },
    {
        "name": "zbx-storage",
        "scope_class": "StorageSystem",
        "desc": "Zabbix host sync -> StorageSystem (name-pattern storage hosts)",
        "attrs": [
            {"attcode": "name", "reconcile": 1, "update": 0},
            {"attcode": "org_id", "reconcile": 0, "update": 1, "target_attcode": "name"},
            {"attcode": "status", "reconcile": 0, "update": 1},
            {"attcode": "managementip", "reconcile": 0, "update": 1},
            {"attcode": "serial_number", "reconcile": 0, "update": 1},
            {"attcode": "model_id", "reconcile": 0, "update": 1, "target_attcode": "name"},
        ],
    },
]


def rest(operation: str, extra: dict):
    payload = {"operation": operation}
    payload.update(extra)
    data = urllib.parse.urlencode({"version": "1.0", "json_data": json.dumps(payload)}).encode()
    # iTop 3.x REST 仅接受 HTTP Basic 认证（json_data 内嵌凭据会被拒）
    req = urllib.request.Request(ITOP_URL + "/webservices/rest.php", data=data)
    import base64
    req.add_header("Authorization", "Basic " + base64.b64encode(f"{ADMIN_USER}:{ADMIN_PWD}".encode()).decode())
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = resp.read().decode("utf-8", "replace")
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return {"code": -1, "message": "non-JSON response: " + body[:300]}


def find_id(cls: str, oql: str):
    # iTop REST 无 core/search；OQL 查询走 core/get（key=OQL）
    r = rest("core/get", {"class": cls, "key": oql})
    if r.get("code") != 0:
        raise RuntimeError(f"search {cls} failed: {r.get('message')}")
    items = r.get("objects") or {}
    for k, v in items.items():
        return int(v["fields"].get("id") or k.split("::")[-1])
    return None


def main():
    if ADMIN_PWD in PLACEHOLDERS:
        print("[provision] SKIPPED: ITOP_ADMIN_PWD not set in /opt/itop/.env")
        print("[provision] HINT: fill ITOP_ADMIN_PWD=<itop admin password>, then:")
        print("[provision]        cd /opt/itop && docker compose restart sync")
        return 0
    print(f"[provision] admin user: {ADMIN_USER} -> {ITOP_URL}")

    # 1. Organization
    org_id = find_id("Organization", f"SELECT Organization WHERE name='{ORG_NAME}'")
    if org_id is None:
        r = rest("core/create", {"comment": "provisioned by itop-sync", "class": "Organization",
                                 "fields": {"name": ORG_NAME, "status": "active"}})
        if r.get("code") != 0:
            raise RuntimeError("create Organization failed: " + str(r.get("message")))
        org_id = int(r["id"])
        print(f"[provision] Organization '{ORG_NAME}' created id={org_id}")
    else:
        print(f"[provision] Organization '{ORG_NAME}' exists id={org_id}")

    # 2. 同步账号 user_id（数据源属主）
    user_id = find_id("User", f"SELECT User WHERE login='{ADMIN_USER}'")
    if user_id is None:
        raise RuntimeError(f"User '{ADMIN_USER}' not found in iTop")
    print(f"[provision] owner user id={user_id}")

    # 3. Synchro Data Sources
    for d in DS_DEFS:
        ds_id = find_id("SynchroDataSource", f"SELECT SynchroDataSource WHERE name='{d['name']}'")
        if ds_id is not None:
            print(f"[provision] DS '{d['name']}' exists id={ds_id}")
            continue
        fields = {
            "name": d["name"],
            "description": d["desc"],
            "status": "production",
            "user_id": user_id,
            "scope_class": d["scope_class"],
            "scope_restriction": "",
            "reconciliation_list": json.dumps(["name"]),
            "attribute_list": json.dumps(d["attrs"]),
            "user_delete_policy": "update",
            "full_load_periodicity": 0,
        }
        r = rest("core/create", {"comment": "provisioned by itop-sync", "class": "SynchroDataSource", "fields": fields})
        if r.get("code") != 0:
            raise RuntimeError(f"create DS {d['name']} failed: {r.get('message')}")
        print(f"[provision] DS '{d['name']}' created id={r['id']} target={d['scope_class']}")

    print("[provision] ALL DONE")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001
        print(f"[provision] FAILED: {e}")
        print("[provision] will retry on next container restart")
        sys.exit(1)
