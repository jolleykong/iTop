# cmdb-sync：Zabbix → iTop CMDB 同步容器

## 架构

```
Zabbix (10.240.22.34, API 只读)                      iTop (itop-web:8081)
  host.get 全量(1290台)        ┌──────────────┐      Synchro Data Source
  模板/标签/接口/宏      ──读──▶│  itop-sync   │──推──▶  zbx-vms        -> VirtualMachine
  inventory(os/serial)         │  (本容器)     │        zbx-servers    -> Server
                               └──────────────┘        zbx-dbservers  -> DBServer
                                                       zbx-network   -> NetworkDevice
                                                       zbx-storage   -> StorageSystem
```

- 对账键 = `name`（Zabbix 主机名）；增量装载（UPDATE touch + INSERT 新行）
- 引擎 Job1 依据 `full_load_periodicity=14400` 判定 stale：4h 内未被 touch 的 replica
  自动 obsolete（`delete_policy=update` → CI status=obsolete）
- **分类规则（v2，模板证据优先）**：
  1. `VMware Guest` 模板 → VirtualMachine（vCenter 自动发现，确证）
  2. `Oracle by ODBC` / `Oracle backup checker` / `MySQL by ODBC` → DBServer（实例视角 host）
  3. Cisco/ASA/F5/Brocade/Generic SNMP → NetworkDevice
  4. XSKY/OceanStor/Unity/深信服/Storage → StorageSystem
  5. BMC/iDRAC/iLO/IMM/PowerEdge/ProLiant/Supermicro → Server（物理硬件视角）
     - 命名 `svr-MGMT-{带外IP}-{OS名}`（v3.12 起的硬件管理地址规范；旧前缀
       `svr-{ilo|idrac|bmc|imm}-` 兼容）的 BMC host 解析出 OS 名，若与本轮 Server
       行同名则合并（serial/model 充实目标行），不单独建 CI；无模板时命名本身
       即证据（naming:bmc），按物理机 Server 建模，不再暂挂
  6. ESXi/Hypervisor → Server（宿主机）
  7. 无模板证据（agent-only/裸 host）→ Server + description `phys:unverified`
     （等 Zabbix 侧补 systemd-detect-virt / inventory.type 证据后自动迁移）
  8. 名称含 存储/storage/NAS/SAN 的无模板 host → StorageSystem

## 关系挂接（v2.1）

- **DBServer.system_id（承载主机，必填）**：`{$ORACLE.HOST}` 宏值 + DB host 自身接口 IP
  反查承载 OS host；当前 15/111 可解析，其余挂占位桶 `ZBX-DB-Host-Unassigned`
- **VirtualMachine.virtualhost_id（宿主，必填）**：Zabbix vCenter discovery 不提供宿主线索，
  统一挂占位桶 `ZBX-VM-Unassigned-Host`（iTop 类：Hypervisor/VirtualHost）
- 占位桶由 provision 自动创建（幂等）；Zabbix 侧数据补齐后同步自动重新挂接

## 配置（/opt/itop/.env，chmod 600）

```
ZBX_URL=http://10.240.22.34/zabbix/api_jsonrpc.php
ZBX_TOKEN=<zabbix api token>
ITOP_USER=admin
ITOP_ADMIN_PWD=<iTop 管理员密码>
CMDB_ORG=ITOPS
SYNC_INTERVAL_SECONDS=14400
```

`ITOP_ADMIN_PWD` 未配置时：provision 跳过、推送跳过，日志有明确指引。
填写后 `docker compose restart sync` 即完成供给+首轮同步。

## 行为

1. 容器启动：等 20s → 进入同步循环
2. 每轮：拉 Zabbix 全量（含模板/标签/宏）→ 分类 + 关联解析 → 5 个 DS 增量装载 →
   POST `synchro/synchro_exec.php`（逐 DS）
3. 日志：`docker logs itop-sync`，含分类统计、BMC 合并/未关联清单、DBServer 挂接统计
4. 一次性供给（建占位桶 + 归一化数据源映射）：`docker compose up provision`

## 命名分层与 VM 双产（v3.13 定稿）

同一台 VM 在 Zabbix 常有两个 host：OS 层 agent host（Windows/Linux 模板，
active agent 按技术名匹配）与 VM 层 vCenter LLD guest host。命名规范：

- **OS 层 agent host 占原生主机名**（技术名=可见名），active agent 的
  HostnameItem 自动匹配；Windows 模板等 OS 级监控对象挂在此层
- **VM 层 guest 可见名 = `{vCenter名}-VM`**：由 guest 根 host prototype 的
  name 宏 `name={#VM.NAME}-VM` 统一下发（存量 host 随 LLD 重算自动改名，
  不允许对 LLD host 直接 host.update 改可见名）
- sync 配对钥匙（`_norm_name` 双向归一化）：IP 相等 / agent 技术名 = guest 可见名 /
  agent 技术名剥 `-OS` = guest 可见名剥 `-VM`。agent 行并入 guest 行
  （description 追加 `os-agent:{名}`），保留带宿主证据的 guest 对象
- 已完成迁移：存量 323/358 guest 可见名下发 `-VM`；70 台可见名沦为 IP 的
  agent host 归还真主机名（VR-SSAPP03 为代表案例：10967=VR-SSAPP03，
  guest 12000=VR-SSAPP03-VM）
- ext key 撞名坑：`DBServer.system_id` 目标类 FunctionalCI 按 name 解析，
  同名 Server+VirtualMachine 残留双对象会导致解析不唯一 → create 失败
  （errors=10 案例：finedb/FAQDB 等 8 名 10 行）。v3.10 已 obsolete 的
  Server 残留对象需手工清理（REST core/delete），清理后自愈

## 宿主 → 物理机挂接（v3.19 定稿）

Hypervisor 类无 `serialnumber` 字段，宿主用 SN 解析到 `Server` 行（带外口 CI
`svr-MGMT-{带外IP}-{OS名}`），SN 证据写入 `description` 便于审计。SN 来源
**四级兜底，命中即止**：

| 级 | 来源 | 证据标记 | 覆盖面 |
|---|---|---|---|
| a | `vmware.hv.hw.serialnumber` item（vCenter SDK） | `sn:vcenter-item` | 少量 Dell |
| b | `vc_sn_map.json`（`vc_sn_pull.py` 直读 vCenter otherIdentifyingInfo） | `sn:vc-api` | 28/36 |
| c | SMBIOS UUID 全 ASCII 解码（HPE/Lenovo 部分机型） | `sn:uuid-decode` | 少量 |
| d | **BMC FRU 自动兜底**（Zabbix 已采，见下） | `sn:bmc-auto` | 6/36 |

### d) BMC 自动兜底原理（v3.19）

带外 BMC 主机 `svr-MGMT-*` 本身就在全自动采集 FRU SN，无需 vCenter 参与。
定位规则：`{前两段相同, 末段相同, 第三段不同}`，即机房带外网段与业务网段的
末位一一对应（实测 `10.240.48.N` ↔ `10.240.0.N`）。

验证依据（2026-11-26 实测）：

- 已挂接的 30 台宿主，BMC `inventory.serialno_a` 与 vCenter SN **30/30 完全一致**
- 反向验证：把 `vc_sn_map.json` 里 6 台 ESXi 6.5 Lenovo 的手工条目删除后重跑，
  sync 自动以 `sn:bmc-auto` 挂回，SN 全部正确（J32MEVT/VR/VL/VP/VM/VN）

安全约束：

- 仅在 a/b/c 全缺失时启用，不覆盖已有更硬证据
- 同末段命中多个候选 → **跳过并写日志**（`宿主 BMC 兜底存在歧义`），不猜
- `enrich_bmc_item_sn`：BMC 主机 SN 优先取 `system.hw.serialnumber` item 现值
  覆盖 `inventory.serialno_a`。原因：BMC 主机 `inventory_mode` 多为 manual，
  资产值不会随采集刷新，换机后会变死数据；item 是活跃数据源

### ESXi 6.5 老主机为何拿不到 SN

`otherIdentifyingInfo` 在 ESXi 6.5 主机上稳定返回**空数组**（非字段缺失、非
报错），同批 6.7 主机全部正常 —— 这是 vCenter 侧限制，不是取法问题（
`vc_sn_pull.py` 已用子路径单取 `hardware.systemInfo.otherIdentifyingInfo`）。
这 6 台 Lenovo x3650 M5 只有 443 端口开放（SSH/CIM/SNMP 全关），vCenter 是唯一
现成 API 通道，因此启用 d) 兜底。同批 UUID 也非 ASCII（`77829ff0-...`），c) 级无效。

### vc_sn_map.json 的正确用法

由 `vc_sn_pull.py` **自动产出**，不要手工编辑：

```bash
python3 vc_sn_pull.py <vc_host> info_reader@vsphere.local @/root/kk/.vc_pass /tmp/vc_hosts.json
```

外源 map 的定位是"补 vCenter 的盲区"，不是人工维护台账。若某台需临时补录，
写成 `{"sn": "...", "src": "bmc-fru"}` 标注来源（v3.18 兼容格式），并在
下一次 `vc_sn_pull` 产出后复核。

## 人工核对入口（iTop UI）

- 数据同步：管理工具 → 数据同步（每轮执行报告）
- CI 列表：配置管理 → 虚拟机 / 服务器 / 数据库实例（DBServer）/ 网络设备 / 存储系统
- 待治理清单（OQL）：
  - 承载 OS 未纳管的 DB 实例：`SELECT DBServer WHERE system_id->name = 'ZBX-DB-Host-Unassigned'`
  - 宿主未知的 VM：`SELECT VirtualMachine WHERE virtualhost_id->name = 'ZBX-VM-Unassigned-Host'`
  - 物理性未证实的 Server：`SELECT Server WHERE description LIKE '%phys:unverified%'`

## Zabbix 侧治理建议（与 iTop 对齐）

1. OS agent 主机补采 `system.hw.serialnumber`（需 sudo）→ inventory.serialno_a，
   与 BMC host 序列号对账，形成不依赖命名的硬关联
   - 已完成（2026-11-26）：36 台 ESXi-SVR LLD 宿主 `inventory_mode` 由 disabled
     改 manual（host prototype 层下发），回填 `serialno_a` 34 台；余 2 台为托管平台
2. OS agent 主机加 `systemd-detect-virt` 自定义 item → inventory.type（KVM/physical/...），
   消除 461 台 phys:unverified
3. BMC host 打 tag `os_host=<OS主机名>`（命名规范已有：svr-MGMT-IP-OS名，146/148 符合）
4. Oracle DB host 的 OS 侧补建 agent host（当前 96 台实例无独立 OS 监控）
5. vCenter discovery 补宿主信息（tag/hypervisor 名称），消除 VM 占位桶

## 后续演进（v3 候选）

- ESXi 宿主建 Hypervisor CI（zbx-vhosts DS），VM.virtualhost_id 关联真实宿主
- RAC 集群（Cluster 类）、存储连接关系（SAN/FiberChannelLink）
- 业务域归属（ApplicationSolution / tag），承载 DB 的 OS 主机与业务关联
