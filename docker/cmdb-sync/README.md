# cmdb-sync：Zabbix → iTop CMDB 同步容器

## 架构

```
Zabbix (10.240.22.34, API 只读)                      iTop (itop-web:8081)
  host.get 全量(1290台)        ┌──────────────┐      Synchro Data Source
  agent+SNMP 接口       ──读──▶│  itop-sync   │──推──▶  zbx-servers    -> Server
  inventory(os/serial)         │  (本容器)     │        zbx-network   -> NetworkDevice
                               └──────────────┘        zbx-storage   -> StorageSystem
```

- 对账键 = `name`（Zabbix 主机名）；重复推送只产生增量更新
- 数据源 `user_delete_policy=update`：上一轮有、本轮未推到的设备自动标记 obsolete
- 分类（v1，主机组在 Zabbix 里不可靠，改用名称+接口类型）：
  - 名称含 存储/storage/NAS/SAN → StorageSystem
  - 名称含 iLo 或有 agent 接口 → Server（含 iLo 硬件：model/serial 来自 inventory）
  - 其余（SNMP-only，多为交换机/网络设备）→ NetworkDevice

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

1. 容器启动：等 20s → `provision.py`（一次性建 Organization + 3 个数据源）→ 进入同步循环
2. 每轮：拉 Zabbix 全量 → 分类 → 每类一个 CSV（`;` 分隔）→ POST `synchro/synchro_exec.php`
3. 日志：`docker logs itop-sync`，含每轮 created/updated/errors/obsolete 统计

## 人工核对入口（iTop UI）

- 数据同步：管理工具 → 数据同步（每轮执行报告）
- CI 列表：配置管理 → 服务器 / 网络设备 / 存储系统

## 后续演进（v2 候选）

- 分类规则接 Zabbix 主机组（当组规范后切换）
- location/brand 字段、VirtualMachine 类
- itops 平台接入：告警反查 CI（同一数据源通道）
