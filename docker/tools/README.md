# docker/tools — iTop 平台侧运维工具

## mkcrop.php — 裁剪非 CMDB 模块

### 为什么需要它

只把 iTop 当 CMDB 用时，问题单、变更、服务、门户、iTop Hub 这些模块纯属负担：
菜单冗余、每轮升级都要重新做一遍选择，且**在 setup 向导里改选项并不可靠**
（实测 2026-11-26：向导虽勾了 "No Tickets / No Change Management" 且走到
"installation completed successfully"，但 `selected_modules` 仍是完整 40 项，
一个模块都没被移除）。

真正决定「编译进数据模型的模块集合」的是 setup 参数文件里的 `selected_modules`。

### 两个关键事实（源码依据）

1. `setup/unattended-install/unattended-install.php`
   - **不传 `--installation_xml` 时**，直接采用参数文件的 `selected_modules`
     （脚本内注释：`Modules to install listed in <file> (selected_modules section)`）；
   - 传了 `--installation_xml` 时，改由 `selected_extensions`（向导选项）反推模块，
     `selected_modules` 会被覆盖——这就是向导路线不可控的原因。
2. `setup/runtimeenv.class.inc.php::RecordInstallation()`
   - 只对 `selected_modules` 里的条目**追加** `priv_module_install` 记录，
     从不删除旧行 ⇒ 该表是追加式日志，按 `MAX(installed)` 批次读取最新状态。

因此裁剪 = 生成一份只含目标模块的参数文件 + 跑一次无人值守 upgrade。

### 用法

```bash
# 1. 备份（务必）
docker exec itop-db sh -c 'exec mysqldump -u root -p"$MARIADB_ROOT_PASSWORD" --single-transaction --routines itop' | gzip > /root/backups/itop-$(date +%F-%H%M%S).sql.gz
tar czf /root/backups/itop-env-$(date +%F-%H%M%S).tar.gz -C /opt/itop env-production

# 2. 生成参数文件（脚本自行从 conf/production/config-itop.php 读库连接，不打印口令）
docker cp mkcrop.php itop-web:/tmp/mkcrop.php
docker exec -u root itop-web php /tmp/mkcrop.php /tmp/params-crop.xml
docker exec -u root itop-web chmod 644 /tmp/params-crop.xml

# 3. 干跑（只校验备份前提，不落盘）
docker exec -u 33 itop-web php /var/www/html/setup/unattended-install/unattended-install.php \
  --param-file=/tmp/params-crop.xml --install=0

# 4. 正式执行（必须以 www-data 身份，否则 conf/ 与 env-production 会被 root 拥有）
docker exec -u root itop-web chmod 660 /var/www/html/conf/production/config-itop.php
docker exec -u 33 itop-web php /var/www/html/setup/unattended-install/unattended-install.php \
  --param-file=/tmp/params-crop.xml

# 5. 重启让新数据模型生效
docker restart itop-web
```

### 裁剪清单（2026-11-26 实盘：45 → 29 个模块）

移除（16 个，全部非 CMDB）：

| 模块 | 用途 |
| --- | --- |
| `itop-tickets` | 工单/事件基础类 |
| `itop-request-mgmt` | 服务请求 |
| `itop-change-mgmt` | 变更管理 |
| `itop-problem-mgmt` | 问题管理 |
| `itop-knownerror-mgmt` | 已知错误 |
| `itop-faq-light` | FAQ |
| `itop-service-mgmt` | 服务管理 |
| `itop-portal` | 自助服务门户 |
| `itop-endusers-devices` | 终端用户设备 |
| `itop-hub-connector` | iTop Hub 连接器 |
| `itop-bridge-cmdb-ticket` | CI↔工单桥 |
| `itop-bridge-cmdb-services` | CI↔服务桥 |
| `itop-bridge-datacenter-mgmt-services` | 机房↔服务桥 |
| `itop-bridge-storage-mgmt-services` | 存储↔服务桥 |
| `itop-bridge-virtualization-mgmt-services` | 虚拟化↔服务桥 |
| `itop-bridge-endusers-devices-services` | 终端设备↔服务桥 |

保留 29 个：core/structure/config-mgmt/datacenter/storage/virtualization/container/
flow-map、认证、主题、备份、附件、升级、profiles、sla-computation、portal-base（mandatory）。

### 注意

- 依赖闭包：`itop-bridge-*-services` 与 `itop-request-mgmt`/`itop-change-mgmt`/
  `itop-service-mgmt` 依赖 `itop-tickets`；漏删任一模块，编译期会报缺失依赖。
- 卸载**不删表**：iTop setup 从不 `DROP TABLE`（`grep -r 'DROP TABLE' setup/` 为空），
  因此 `ticket`/`service`/`contract`/`faq` 等表会留成孤儿表（无 ORM 引用、不影响运行）。
  确认无需回溯数据后再手工清理。
- `priv_extension_install` 也要对齐：本路线不提交 `selected_extensions`，
  登记表最新批次仍是裁剪前的旧集合，需重建（只保留 6 条 config-mgmt 系扩展），
  否则下次 setup 会误判「扩展被选中但目录缺失」。
