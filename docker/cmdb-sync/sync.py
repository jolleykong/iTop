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
     - svr-MGMT-{IP}-OS名 命名的 BMC host（旧规范 svr-{ilo|idrac|bmc|imm}-IP-OS名
       兼容）：解析出 OS 名，若本轮存在同名
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
# BMC host 命名钥匙（v3.12 起新规范）：svr-MGMT-{带外IP}-{OS主机名} =
# 服务器硬件管理地址（覆盖 iLo/iDRAC/IMM/Inspur BMC 等全部带外口）。
# 兼容旧前缀 svr-{ilo|idrac|bmc|imm}-（切换期两套并存，Zabbix 侧已批量更名）
RE_BMC_NAME = re.compile(
    r"^svr-(mgmt|ilo|idrac|bmc|imm)-(\d+(?:\.\d+){3})-(.+)$", re.I)

# v3.15: vCenter 模板 VM 不入 CMDB（安全隔离：模板机名常带账号密码，如
# "centos6.5模板（root/qweMNB@123）"，不宜广泛展示与搜集）。
# Zabbix 官方 vmware.vm.discovery 宏集无 {#VM.TEMPLATE}（5.2/6.4/8.0 文档
# 均确认），LLD 层无法过滤 → 只能在本层按名称跳过；已入库 CI 由引擎
# obsolete 机制自动淘汰（staging 不再包含即失效）。
RE_TEMPLATE_GUEST = re.compile(r"模板|template|tmpl|\.vmtx", re.I)


def _uuid_field_swap(hexstr: str) -> str:
    """SMBIOS GUID 前 3 个字段按 16/32bit 小端存储：hex pair 保内容、倒顺序。
    （4c4c4544 类二进制 UUID 不适用；仅全 ASCII UUID 解码路径使用）"""
    pairs = [hexstr[i:i + 2] for i in range(0, len(hexstr), 2)]
    return "".join(reversed(pairs))


def decode_uuid_sn(uuid: str) -> str:
    """宿主 SMBIOS UUID 全 ASCII 时解码为 SN。

    v3.16 修正字节序：vCenter 返回的 UUID 前 3 个字段是小端存储的，
    直接 hex->ascii 会得到错序串（HPE 实证：30393137-3436-4336-5537-...
    raw 解出 "091746C6U750N5A8"，规范序后取尾 10 位 = "6CU750N5A8"，
    与机身标签/HPE SN 格式一致）。raw 解码即 8-12 位 SN 时（部分机型
    直接把 SN 写入 UUID）优先 raw。Dell 4c4c4544-* 为二进制不可解码，
    SN 走 vmware.hv.hw.serialnumber item。"""
    try:
        h = bytes.fromhex(uuid.replace("-", ""))
        s = h.decode("ascii")
    except Exception:
        return ""
    if not re.match(r"^[A-Za-z0-9\-]{8,32}$", s):
        return ""
    if re.match(r"^[A-Za-z0-9\-]{8,12}$", s):
        return s
    parts = uuid.split("-")
    if len(parts) == 5:
        try:
            canon = bytes.fromhex(
                _uuid_field_swap(parts[0]) + _uuid_field_swap(parts[1])
                + _uuid_field_swap(parts[2]) + parts[3] + parts[4]
            ).decode("ascii")
        except Exception:
            canon = ""
        tail = canon[-10:] if canon else ""
        if re.match(r"^[A-Za-z0-9]{10}$", tail):
            return tail
    return s

# 模板名 → 类别（按序匹配，先命中先定类）
TPL_PATTERNS = [
    ("VirtualMachine", re.compile(r"^VMware Guest$", re.I), False),
    ("DBServer", re.compile(r"^Oracle by ODBC|Oracle backup checker|MySQL by ODBC", re.I), False),
    ("NetworkDevice", re.compile(r"Cisco|\bASA\b|Router|\bF5\b|Brocade|\bSwitch\b|Juniper|Generic by SNMP", re.I), False),
    ("StorageSystem", re.compile(r"XSKY|OceanStor|Unity|SANGFOR|Storage|\bNAS\b", re.I), False),
    ("Hypervisor", re.compile(r"Hypervisor|ESXi", re.I), False),  # 虚拟化宿主（v3.9: iTop Hypervisor 类，供 VM.virtualhost_id 挂接）
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
    for cls, tval in (("Server", "Server"), ("Hypervisor", "Server"),
                      ("NetworkDevice", "NetworkDevice"),
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
        # v3.10: 技术名（host 字段）——agent host 常改可见名（如 VS-BJDNS01 -> "130"），
        # 与 vCenter guest 可见名配对去重时需要原始技术名
        "_tech": clean(h.get("host") or ""),
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


def has_db_tag(tg: dict) -> bool:
    """v3.7: host tag type=Database —— 用户治理口径，显式标注的数据库项目。
    仅在无模板证据时兜底（模板证据优先）。"""
    return (tg.get("type") or "").strip().lower() == "database"


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


def build_all(hosts: list, oracle_map: dict = None, hv_sn: dict = None) -> tuple:
    """分类 + BMC 关联合并。返回 {cls: [rows]} 及统计。"""
    stats = {"merged_bmc": [], "unlinked_bmc": [], "merged_osagent": [],
             "merged_hv_snmp": [], "counts": {}}
    by_cls = {"Server": [], "VirtualMachine": [], "DBServer": [],
              "NetworkDevice": [], "StorageSystem": [], "Hypervisor": []}
    # v3.8: phys:unverified 且与 VMware Guest 同 IP 的 Server 行，按 IP 归组
    # 待并入 VM 行（见下方 OS agent 合并阶段）
    osagent_by_ip = {}

    # v3.9: 宿主索引 —— Hypervisor 类 host（VMware Hypervisor LLD 技术名={#HV.UUID}，
    # display 名={#HV.ID}/IP；ESXi SNMP host 名=IP）建立 uuid/name/ip 三钥匙索引，
    # 供 VM 行按 guest host tag（hv_uuid/hv_id/hv_name，Zabbix Guest host prototype
    # 自动下发）挂接真实宿主；无 tag 或未解析 -> 占位桶 ZBX-VM-Unassigned-Host
    hv_uuid_map, hv_name_map = {}, {}
    for h in hosts:
        cls0, _, _ = tpl_class(h)
        if cls0 != "Hypervisor":
            continue
        disp = clean(h.get("name") or h.get("host"))
        tech = clean(h.get("host") or "")
        if disp and tech:
            hv_uuid_map[tech.lower()] = disp
        if disp:
            hv_name_map[disp.lower()] = disp
            ip0 = main_ip(h)
            if ip0:
                hv_name_map[ip0] = disp

    def resolve_hv(tg: dict):
        """按 tag 优先级解析宿主 display 名，未命中返回 None"""
        for k in ("hv_uuid", "hv_id", "hv", "hv_name"):
            v = (tg.get(k) or "").strip().lower()
            if not v:
                continue
            if v in hv_uuid_map:
                return hv_uuid_map[v]
            if v in hv_name_map:
                return hv_name_map[v]
        return None

    for h in hosts:
        name = clean(h.get("name") or h.get("host"))
        if not name:
            continue
        tg = {t.get("tag"): t.get("value") for t in h.get("tags") or []}
        cls, src, is_bmc = tpl_class(h)
        notes = [src] if src else []

        # v3.15: vCenter 模板 VM 跳过 —— 不入 CMDB（安全隔离，见 RE_TEMPLATE_GUEST）
        if cls == "VirtualMachine" and RE_TEMPLATE_GUEST.search(name):
            stats["counts"]["VM_template_skipped"] = \
                stats["counts"].get("VM_template_skipped", 0) + 1
            stats.setdefault("skipped_template_guest", []).append(name)
            continue

        if cls is None and has_db_tag(tg):
            # v3.7: tag type=Database 显式标注 —— 双产：
            #   DBServer（数据库实例层，system_id 指向自身同名对象）
            #   + OS 层继续常规判定（Server/VirtualMachine，承载 hw 信息）
            notes.append("tag:type=Database")
            db_row = base_row(h, name, "DBServer", list(notes))
            db_row["description"] = clean("zbx: " + "; ".join(n for n in notes if n))
            db_row["system_id"] = name  # 自身 Server/VM 对象（同名，引擎按 name 对账）
            by_cls["DBServer"].append(db_row)
            stats["counts"]["DBServer_by_tag"] = stats["counts"].get("DBServer_by_tag", 0) + 1
            notes = []  # OS 层不带 DB 标注，避免 description 重复
        if cls is None:
            # 无模板证据：v3.12 命名规范补充证据 —— svr-MGMT-{IP}-{OS名}
            # （兼容旧 svr-{ilo|idrac|bmc|imm}- 前缀）= 服务器硬件管理地址，
            # 按物理机 Server 建模（不再保守暂挂 phys:unverified）
            if RE_BMC_NAME.match(name):
                cls = "Server"
                notes.append("naming:bmc")
            elif RE_STORAGE.search(name):
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
        # v3.9: VM 挂真实宿主（tag 解析），解析成功记 host:线索进 description
        hv_tgt = resolve_hv(tg) if cls == "VirtualMachine" else None
        if hv_tgt:
            notes.append(f"host:{hv_tgt}")
        row = base_row(h, name, cls, notes)
        row["description"] = clean("zbx: " + "; ".join(n for n in notes if n))
        if cls == "VirtualMachine":
            # tag 判定的公有云资源挂 Cloud 占位桶；已解析宿主挂真实 Hypervisor，
            # 其余挂占位桶
            row["virtualhost_id"] = (CLOUD_PLACEHOLDER if any("ecs" in n for n in notes)
                                     else (hv_tgt or VM_HOST_PLACEHOLDER))
            if hv_tgt:
                stats["counts"]["vm_on_real_host"] = stats["counts"].get("vm_on_real_host", 0) + 1
        if cls == "DBServer":
            row["system_id"] = (oracle_map or {}).get(name) or DB_HOST_PLACEHOLDER
        by_cls[cls].append(row)
        if cls == "Server" and "phys:unverified" in notes and row.get("managementip"):
            osagent_by_ip.setdefault(row["managementip"], []).append(row)

    # BMC host 关联合并：svr-MGMT-IP-OS名 -> 同名 Server 行（OS host 视角）。
    # BMC 证据两来源：模板（tpl:*.BMC/iDRAC/...）或命名规范（naming:bmc）
    def is_bmc_row(r):
        return any(RE_BMC_NAME.match(r["name"]) and
                   (n.startswith("tpl:") or n.startswith("naming:")) and
                   re.search(r"BMC|iDRAC|iLO|IMM|PowerEdge|ProLiant|Supermicro|bmc", n, re.I)
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

    # v3.10: VM 双产去重 —— 同一台 VM 在 Zabbix 常有两个 host（OS agent host 带
    # virt tag/inventory 判成 VirtualMachine + vCenter 发现的 VMware Guest host）。
    # guest 侧有模板硬证据和真实宿主，作为保留对象；agent 侧行（无模板证据）并入：
    # description 追加 os-agent:{OS host 名}，agent 侧更准的 managementip 回填 guest
    # 空缺。配对钥匙（v3.13 分层命名后双向归一化）：
    #   IP 相等；agent 技术名 = guest 可见名；agent 技术名剥 -OS 后缀 = guest
    #   可见名剥 -VM 后缀。分层命名规范：OS 层 agent host 占原生主机名
    #   （active agent HostnameItem 自动匹配），VM 层 guest 可见名={vCenter名}-VM
    def _is_guest_row(r):
        return any(n.startswith("tpl:") and "VMware Guest" in n for n in r["_notes"])

    def _norm_name(s: str, suffix: str) -> str:
        t = (s or "").strip().lower()
        return t[:-len(suffix)] if t.endswith(suffix.lower()) else t

    vm_guest_by_key = {}
    for r in by_cls["VirtualMachine"]:
        if not _is_guest_row(r):
            continue
        keys = set()
        if r.get("managementip"):
            keys.add(r["managementip"])
        if r["name"]:
            keys.add(r["name"].lower())
            keys.add(_norm_name(r["name"], "-vm"))
        for k in keys:
            vm_guest_by_key.setdefault(k, r)
    kept_vms, touch_vms = [], []
    for r in by_cls["VirtualMachine"]:
        if _is_guest_row(r):
            kept_vms.append(r)
            continue
        tgt = None
        if r.get("managementip"):
            tgt = vm_guest_by_key.get(r["managementip"])
        if tgt is None and r.get("_tech"):
            tgt = vm_guest_by_key.get(r["_tech"].lower()) \
                or vm_guest_by_key.get(_norm_name(r["_tech"], "-os"))
        if tgt is None or tgt is r:
            kept_vms.append(r)
            continue
        tgt["_notes"].append(f"os-agent:{r['name']}")
        if not tgt.get("managementip") and r.get("managementip"):
            tgt["managementip"] = r["managementip"]
        stats["merged_osagent"].append(f"{r['name']} -> {tgt['name']} (vm-dedup)")
        touch_vms.append(tgt)
    for tgt in touch_vms:
        tgt["description"] = clean("zbx: " + "; ".join(n for n in tgt["_notes"] if n))
    by_cls["VirtualMachine"] = kept_vms

    # v3.8: OS agent 视角并入 VM —— 同 IP 已有 VMware Guest（vCenter 硬证据）时，
    # 该 host 是 VM 的 OS 层 agent。iTop 模型里 VM 的 OS 层就是 VirtualMachine
    # 本身，再单建 Server 会形成"孤儿 Server"（Server 视图看不到任何 VM 关联，
    # 10.240.24.177/171 调查结论）：VM 行 description 补记 os-agent 线索，Server 行丢弃
    vm_by_ip = {}
    for r in by_cls["VirtualMachine"]:
        if r.get("managementip"):
            vm_by_ip.setdefault(r["managementip"], r)
    drop_ids, touch_targets = set(), []
    for ip, rows in sorted(osagent_by_ip.items()):
        tgt = vm_by_ip.get(ip)
        if tgt is None:
            continue
        for r in rows:
            tgt["_notes"].append(f"os-agent:{r['name']}")
            drop_ids.add(id(r))
            stats["merged_osagent"].append(f"{r['name']} -> {tgt['name']}")
        touch_targets.append(tgt)
    for tgt in touch_targets:
        tgt["description"] = clean("zbx: " + "; ".join(n for n in tgt["_notes"] if n))
    if drop_ids:
        by_cls["Server"] = [r for r in by_cls["Server"] if id(r) not in drop_ids]

    # v3.14: ESXi 宿主去重 —— 同一物理宿主在 Zabbix 可能有两条 host：
    # VMware Hypervisor LLD host（tech=UUID，display={#HV.NAME}=IP）与
    # ESXi SNMP 独立 host（tech=VMWareServerNNN / 裸 IP，SNMP 硬件告警）。
    # 二者 managementip 相同时 SNMP 行并入 UUID 行：CI 名=UUID 行 display（IP），
    # VM.virtualhost_id 按 name 解析零漂移；SNMP 侧 inventory 证据回填
    # （空值不覆盖），SNMP 证据以 snmp:{tech} 记入 description。
    # 注意 SNMP host 不可见名不得与 LLD {#HV.NAME} 撞车（撞车会导致 LLD
    # 建不出 UUID host，guest hv_uuid 首选钥匙解析失败——2026-11-26 实测）。
    hv_rows = by_cls["Hypervisor"]
    hv_by_ip = {}
    for r in hv_rows:
        ip = r.get("managementip") or ""
        if ip:
            hv_by_ip.setdefault(ip, []).append(r)
    drop_hv = set()
    hv_uuid_re = re.compile(
        r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
    for ip, rs in hv_by_ip.items():
        if len(rs) < 2:
            continue
        uuid_rows = [r for r in rs if hv_uuid_re.match(r.get("_tech") or "")]
        keep = uuid_rows[0] if uuid_rows else rs[0]
        for r in rs:
            if r is keep:
                continue
            keep["_notes"].append("snmp:" + (r.get("_tech") or r["name"]))
            if not keep.get("serialnumber") and r.get("serialnumber"):
                keep["serialnumber"] = r["serialnumber"]
            if not keep.get("model_id") and r.get("model_id"):
                keep["model_id"] = r["model_id"]
            if not keep.get("brand_id") and r.get("brand_id"):
                keep["brand_id"] = r["brand_id"]
            drop_hv.add(id(r))
            stats["merged_hv_snmp"].append(
                f"{r['name']} -> {keep['name']}")
    if drop_hv:
        by_cls["Hypervisor"] = [r for r in hv_rows if id(r) not in drop_hv]
        for r in by_cls["Hypervisor"]:
            r["description"] = clean("zbx: " + "; ".join(n for n in r["_notes"] if n))

    # v3.15: 宿主挂物理机 —— Hypervisor.server_id 按 SN 解析到 Server 行
    # （svr-MGMT-{IP}-{OS名} 带外口 CI，inventory.serialno_a）。SN 来源：
    # a) vmware.hv.hw.serialnumber item（vCenter SDK 精确，实测仅 Dell 机型返回）
    # b) UUID 全 ASCII 解码（华为/浪潮机型，见 decode_uuid_sn）
    # SN 证据同时记入 description（Hypervisor 类无 serialnumber 字段）。
    server_sn = {}
    for r in by_cls["Server"]:
        sn0 = (r.get("serialnumber") or "").strip().upper()
        if sn0:
            server_sn.setdefault(sn0, r["name"])
    hv_server_linked = []
    for r in by_cls["Hypervisor"]:
        tech = r.get("_tech") or ""
        sn = ((hv_sn or {}).get(tech) or "").strip()
        note = "sn:vcenter-item" if sn else ""
        if not sn:
            sn = decode_uuid_sn(tech)
            note = "sn:uuid-decode" if sn else ""
        if not sn:
            continue
        r["_notes"].append(note + ":" + sn)
        tgt = server_sn.get(sn.upper())
        if tgt and tgt != r["name"]:
            r["server_id"] = tgt
            hv_server_linked.append(f"{r['name']} -> {tgt}")
    if hv_server_linked:
        for r in by_cls["Hypervisor"]:
            r["description"] = clean("zbx: " + "; ".join(n for n in r["_notes"] if n))
        stats["hv_server_linked"] = hv_server_linked

    by_cls["VirtualMachine"].sort(key=lambda r: r["name"])
    by_cls["Hypervisor"].sort(key=lambda r: r["name"])
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


def enrich_bmc_fru(hosts: list) -> int:
    """v3.11: BMC 主机 inventory 缺 serial/model/vendor 时，从 Inspur FRU LLD
    item 现值回填。背景：Server Inspur BMC SNMPv3 模板的 FRU Discovery 已在采集
    Product Serial Number/Product Name/Manufacturer Name（每台 20 个 FRU item），
    但原型为 {#SNMPVALUE} 宏通配、无法在原型级映射 inventory_link（会把 Board
    Manufacturer 等一起映射），故在同步侧补：一次 item.get 批量拉 FRU 现值，
    注入 host['inventory']，后续 BMC 关联合并/字典构建自动消费。HP iLO 侧网络
    不通（ICMP 不可达）与 Supermicro 模板缺采集项不在本函数范围。"""
    def _inv(h):
        return h.get("inventory") or {}

    need = []
    for h in hosts:
        name = clean(h.get("name") or h.get("host"))
        if not RE_BMC_NAME.match(name):
            continue
        inv = _inv(h)
        if not inv.get("serialno_a") or not inv.get("model") or not inv.get("vendor"):
            need.append(h)
    if not need:
        return 0
    items = zbx_rpc("item.get", {
        "output": ["hostid", "name", "lastvalue"],
        "hostids": [h["hostid"] for h in need],
        "search": {"key_": "fru.info"},
        "limit": len(need) * 30,
    })
    by_host: dict = {}
    for it in items:
        by_host.setdefault(it["hostid"], {})[clean(it.get("name"))] = clean(it.get("lastvalue"))

    def good(v):
        return v and v not in ("0", "0.0")

    n = 0
    for h in need:
        vals = by_host.get(h["hostid"]) or {}
        inv = _inv(h)
        changed = False
        for k, src in (("serialno_a", "FRU INFO: Product Serial Number"),
                       ("model", "FRU INFO: Product Name"),
                       ("vendor", "FRU INFO: Manufacturer Name")):
            v = vals.get(src, "")
            if good(v) and not inv.get(k):
                inv[k] = v
                changed = True
        if changed:
            h["inventory"] = inv
            n += 1
    return n


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

    n_fru = enrich_bmc_fru(hosts)
    if n_fru:
        log(f"BMC FRU 补充: {n_fru} 台 serial/model/vendor 从 Inspur FRU LLD item 回填")

    oracle_map = build_oracle_map(hosts)
    log(f"oracle hosts resolved via {{$ORACLE.HOST}}/ip: {len(oracle_map)}")

    # v3.15: 宿主 SN —— vmware.hv.hw.serialnumber item 值（key 带 UUID 参数，
    # 按宿主 hostid 关联取 tech 名）
    hv_sn = {}
    hostid_tech = {h["hostid"]: clean(h.get("host") or "") for h in hosts}
    sn_items = zbx_rpc("item.get", {
        "search": {"key_": "vmware.hv.hw.serialnumber"},
        "output": ["hostid", "lastvalue", "state", "status"],
        "limit": 500,
    })
    for it in sn_items:
        v = (it.get("lastvalue") or "").strip()
        tech = hostid_tech.get(it.get("hostid") or "")
        if v and tech and it.get("status") == "0" and it.get("state") == "0":
            hv_sn[tech] = v
    log(f"hypervisor SN via vCenter item: {len(hv_sn)} 台")

    by_cls, stats = build_all(hosts, oracle_map, hv_sn)
    if stats.get("skipped_template_guest"):
        log("vCenter 模板 VM 跳过(不入 CMDB) " +
            str(stats["counts"].get("VM_template_skipped", 0)) + " 台: "
            + ", ".join(stats["skipped_template_guest"][:10]))
    log("v2 分类统计: " + json.dumps(stats["counts"], ensure_ascii=False))
    log(f"BMC 关联合并 {len(stats['merged_bmc'])} 台: " +
        ", ".join(stats["merged_bmc"][:8]) + (" ..." if len(stats["merged_bmc"]) > 8 else ""))
    log(f"BMC 未关联(自建 Server) {len(stats['unlinked_bmc'])} 台")
    log(f"OS agent 并入 VM {len(stats['merged_osagent'])} 台: " +
        ", ".join(stats["merged_osagent"][:8]) +
        (" ..." if len(stats["merged_osagent"]) > 8 else ""))
    n_vm = stats["counts"].get("VirtualMachine", 0)
    n_real = stats["counts"].get("vm_on_real_host", 0)
    log(f"VM 宿主挂接: {n_real}/{n_vm} 台挂真实 Hypervisor"
        f", 其余 {n_vm - n_real} 台待 Zabbix Guest host tag 下发后自动迁移")
    if stats["merged_hv_snmp"]:
        log(f"ESXi SNMP 宿主并入 UUID 宿主 {len(stats['merged_hv_snmp'])} 台: "
            + ", ".join(stats["merged_hv_snmp"]))
    if stats.get("hv_server_linked"):
        log(f"宿主按 SN 挂物理机 {len(stats['hv_server_linked'])} 台: "
            + ", ".join(stats["hv_server_linked"]))

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
    # v3.9: 宿主先行——VM.virtualhost_id 按 name 解析，Hypervisor 行须先入库
    ok &= push("zbx-hypervisors", by_cls["Hypervisor"],
               ["name", "org_id", "status", "description", "server_id"])
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
