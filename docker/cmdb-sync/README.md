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
2. OS agent 主机加 `systemd-detect-virt` 自定义 item → inventory.type（KVM/physical/...），
   消除 461 台 phys:unverified
3. BMC host 打 tag `os_host=<OS主机名>`（命名规范已有：svr-MGMT-IP-OS名，146/148 符合）
4. Oracle DB host 的 OS 侧补建 agent host（当前 96 台实例无独立 OS 监控）
5. vCenter discovery 补宿主信息（tag/hypervisor 名称），消除 VM 占位桶

## 后续演进（v3 候选）

- ESXi 宿主建 Hypervisor CI（zbx-vhosts DS），VM.virtualhost_id 关联真实宿主
- RAC 集群（Cluster 类）、存储连接关系（SAN/FiberChannelLink）
- 业务域归属（ApplicationSolution / tag），承载 DB 的 OS 主机与业务关联
