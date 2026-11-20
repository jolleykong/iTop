<?php
/**
 * cmdb-sync 一次性供给（iTop 原生通道版）
 * 在 itop-web 镜像内以 CLI 运行，用 MetaModel 原生 API 创建：
 *   Organization（如缺）+ 3 个 Synchro Data Source（幂等，存在即跳过）
 * 由 compose 的 provision 服务调用：
 *   php provision_ds.php --auth_user=admin --auth_pwd=... --org=ITOPS
 * REST 建 linkedset 无法区分子类（SynchroAttribute/SynchroAttExtKey），
 * 故数据源创建走本脚本（原生 API 可精确控制子类）。
 */

use Combodo\iTop\Application\WebPage\CLIPage;

require_once(__DIR__ . '/../approot.inc.php');
require_once(APPROOT . '/application/application.inc.php');
require_once(APPROOT . '/application/startup.inc.php');

if (!utils::IsModeCLI()) {
    die("CLI only\n");
}
utils::UseParamFile();

$sAuthUser = utils::ReadParam('auth_user', null, true, 'raw_data');
$sAuthPwd = utils::ReadParam('auth_pwd', null, true, 'raw_data');
$sOrgName = utils::ReadParam('org', 'ITOPS', true, 'raw_data');

if ($sAuthUser === null || (string)$sAuthPwd === '') {
    echo "[provision-ds] SKIP: --auth_user/--auth_pwd not provided (fill /opt/itop/.env)\n";
    exit(0);
}
if (!UserRights::CheckCredentials($sAuthUser, $sAuthPwd)) {
    echo "[provision-ds] FAILED: invalid credentials for '$sAuthUser'\n";
    exit(1);
}
UserRights::Login($sAuthUser);
echo "[provision-ds] logged in as $sAuthUser\n";

// 与 sync.py build_rows 的列保持一致
$aDsDefs = [
    [
        'name' => 'zbx-vms',
        'scope_class' => 'VirtualMachine',
        'desc' => 'Zabbix host sync -> VirtualMachine (VMware Guest template, vCenter confirmed)',
        'attrs' => [
            ['attcode' => 'name', 'reconcile' => true, 'update' => true],
            ['attcode' => 'org_id', 'reconcile' => false, 'update' => true, 'extkey' => true],
            ['attcode' => 'status', 'reconcile' => false, 'update' => true],
            ['attcode' => 'managementip', 'reconcile' => false, 'update' => true],
            ['attcode' => 'description', 'reconcile' => false, 'update' => true],
            // virtualhost_id 必填：宿主线索缺失期统一指向占位宿主
            ['attcode' => 'virtualhost_id', 'reconcile' => false, 'update' => true, 'extkey' => true],
        ],
    ],
    [
        'name' => 'zbx-dbservers',
        'scope_class' => 'DBServer',
        'desc' => 'Zabbix host sync -> DBServer (Oracle/MySQL by ODBC, linked to host via {$ORACLE.HOST})',
        'attrs' => [
            ['attcode' => 'name', 'reconcile' => true, 'update' => true],
            ['attcode' => 'org_id', 'reconcile' => false, 'update' => true, 'extkey' => true],
            // system_id 必填：承载 FunctionalCI（由 {$ORACLE.HOST} 反查 OS host 得出）
            ['attcode' => 'system_id', 'reconcile' => false, 'update' => true, 'extkey' => true],
        ],
    ],
    [
        'name' => 'zbx-servers',
        'scope_class' => 'Server',
        'desc' => 'Zabbix host sync -> Server (physical/BMC/ESXi hosts; agent-only marked phys:unverified)',
        'attrs' => [
            ['attcode' => 'name', 'reconcile' => true, 'update' => true],
            ['attcode' => 'org_id', 'reconcile' => false, 'update' => true, 'extkey' => true],
            ['attcode' => 'status', 'reconcile' => false, 'update' => true],
            ['attcode' => 'managementip', 'reconcile' => false, 'update' => true],
            ['attcode' => 'serialnumber', 'reconcile' => false, 'update' => true],
            ['attcode' => 'model_id', 'reconcile' => false, 'update' => true, 'extkey' => true],
            ['attcode' => 'description', 'reconcile' => false, 'update' => true],
        ],
    ],
    [
        'name' => 'zbx-network',
        'scope_class' => 'NetworkDevice',
        'desc' => 'Zabbix host sync -> NetworkDevice (SNMP-only hosts)',
        'attrs' => [
            ['attcode' => 'name', 'reconcile' => true, 'update' => true],
            ['attcode' => 'org_id', 'reconcile' => false, 'update' => true, 'extkey' => true],
            ['attcode' => 'status', 'reconcile' => false, 'update' => true],
            ['attcode' => 'managementip', 'reconcile' => false, 'update' => true],
            ['attcode' => 'serialnumber', 'reconcile' => false, 'update' => true],
            ['attcode' => 'model_id', 'reconcile' => false, 'update' => true, 'extkey' => true],
            ['attcode' => 'networkdevicetype_id', 'reconcile' => false, 'update' => true, 'extkey' => true],
        ],
    ],
    [
        'name' => 'zbx-storage',
        'scope_class' => 'StorageSystem',
        'desc' => 'Zabbix host sync -> StorageSystem (name-pattern storage hosts)',
        'attrs' => [
            ['attcode' => 'name', 'reconcile' => true, 'update' => true],
            ['attcode' => 'org_id', 'reconcile' => false, 'update' => true, 'extkey' => true],
            ['attcode' => 'status', 'reconcile' => false, 'update' => true],
            ['attcode' => 'managementip', 'reconcile' => false, 'update' => true],
            ['attcode' => 'serialnumber', 'reconcile' => false, 'update' => true],
            ['attcode' => 'model_id', 'reconcile' => false, 'update' => true, 'extkey' => true],
        ],
    ],
];

function findByOQL($sOql)
{
    $oSearch = DBSearch::FromOQL($sOql);
    $oSet = new DBObjectSet($oSearch);
    return $oSet->Fetch();
}

// 1. Organization
$oOrg = findByOQL("SELECT Organization WHERE name='" . $sOrgName . "'");
if ($oOrg === null) {
    $oOrg = MetaModel::NewObject('Organization');
    $oOrg->Set('name', $sOrgName);
    $oOrg->Set('status', 'active');
    $iOrgId = $oOrg->DBInsert();
    echo "[provision-ds] Organization '$sOrgName' created id=$iOrgId\n";
} else {
    echo "[provision-ds] Organization '$sOrgName' exists id=" . $oOrg->GetKey() . "\n";
}

// 1.5 占位宿主（VirtualMachine.virtualhost_id / DBServer.system_id 必填；
//     宿主线索补齐前的统一归属桶）
function ensurePlaceholder($sClass, $oOrg, $sName, $sDesc)
{
    $o = findByOQL("SELECT $sClass WHERE name='" . $sName . "'");
    if ($o !== null) {
        echo "[provision-ds] placeholder $sClass '$sName' exists id=" . $o->GetKey() . "\n";
        return;
    }
    $o = MetaModel::NewObject($sClass);
    $o->Set('name', $sName);
    $o->Set('org_id', $oOrg->GetKey());
    $o->Set('status', 'implementation');
    $o->Set('description', $sDesc);
    $iId = $o->DBInsert();
    echo "[provision-ds] placeholder $sClass '$sName' created id=$iId\n";
}
// VirtualMachine.virtualhost_id 目标类为 VirtualHost（抽象）——占位用具体类 Hypervisor
ensurePlaceholder('Hypervisor', $oOrg, 'ZBX-VM-Unassigned-Host',
    '占位虚拟化宿主：Zabbix VMware discovery 未提供 VM->宿主线索，宿主信息补齐前 VM 统一挂接于此（由 zbx2itop 自动维护）');
ensurePlaceholder('Server', $oOrg, 'ZBX-DB-Host-Unassigned',
    '占位宿主：DB 实例的承载 OS 在 Zabbix 未单独纳管或无法通过 {$ORACLE.HOST}/IP 解析，治理补齐后自动迁移（由 zbx2itop 自动维护）');

// 2. Synchro Data Sources
foreach ($aDsDefs as $aDef) {
    $oExisting = findByOQL("SELECT SynchroDataSource WHERE name='" . $aDef['name'] . "'");
    if ($oExisting !== null) {
        echo "[provision-ds] DS '{$aDef['name']}' exists id=" . $oExisting->GetKey() . "\n";
        continue;
    }

    $oUser = findByOQL("SELECT User WHERE login='" . $sAuthUser . "'");
    if ($oUser === null) {
        echo "[provision-ds] FAILED: owner user '$sAuthUser' not found\n";
        exit(1);
    }

    $oDS = MetaModel::NewObject('SynchroDataSource');
    $oDS->Set('name', $aDef['name']);
    $oDS->Set('description', $aDef['desc']);
    $oDS->Set('status', 'production');
    $oDS->Set('user_id', $oUser->GetKey());
    $oDS->Set('scope_class', $aDef['scope_class']);
    $oDS->Set('scope_restriction', '');
    $oDS->Set('delete_policy', 'update');
    $oDS->Set('delete_policy_update', 'status:obsolete');
    $oDS->Set('full_load_periodicity', 0);
    // 不传 attribute_list：iTop 会自动为 scope_class 全属性生成默认映射行，
    // 随后按本定义逐行归一化（与 UI 表单编辑等效）
    $iDsId = $oDS->DBInsert();
        echo "[provision-ds] DS '{$aDef['name']}' created id=$iDsId target={$aDef['scope_class']}\n";
}

// 2.5 full_load_periodicity 必须 > 0：引擎在 exec-only 阶段（periodicity=0）会跳过
//     disappeared->obsolete 处理（LimitDate=1970）。取与 SYNC_INTERVAL_SECONDS 一致的
//     600s（开发/验证期节奏），保证每轮装载后上一轮未出现的 replica 被正确标记 obsolete。
//     生产化时与 SYNC_INTERVAL_SECONDS 一并调回 14400。
$iLoadPeriodicity = 600;
foreach ($aDsDefs as $aDef) {
    $oDS = findByOQL("SELECT SynchroDataSource WHERE name='" . $aDef['name'] . "'");
    if ($oDS === null) {
        continue;
    }
    if ((int)$oDS->Get('full_load_periodicity') !== $iLoadPeriodicity) {
        $oDS->Set('full_load_periodicity', $iLoadPeriodicity);
        $oDS->DBUpdate();
        echo "[provision-ds] DS '{$aDef['name']}' full_load_periodicity -> {$iLoadPeriodicity}\n";
    }
}

// ── 归一化映射行（对已存在/新建的数据源都执行，幂等）────────────────
foreach ($aDsDefs as $aDef) {
    $oDS = findByOQL("SELECT SynchroDataSource WHERE name='" . $aDef['name'] . "'");
    if ($oDS === null) {
        continue;
    }
    $iDsId = $oDS->GetKey();
    $oSearch = DBSearch::FromOQL("SELECT SynchroAttribute WHERE sync_source_id=$iDsId");
    $oRows = new DBObjectSet($oSearch);
    if ($oRows->Count() == 0) {
        // 历史 DS 可能因创建时 attribute_list 非空但条目失效而缺映射行——删除重建触发自动生成
        echo "[provision-ds] DS '{$aDef['name']}' has 0 attribute rows -> recreating\n";
        $oDS->DBDelete();
        $oUser2 = findByOQL("SELECT User WHERE login='" . $sAuthUser . "'");
        $oDS = MetaModel::NewObject('SynchroDataSource');
        $oDS->Set('name', $aDef['name']);
        $oDS->Set('description', $aDef['desc']);
        $oDS->Set('status', 'production');
        $oDS->Set('user_id', $oUser2->GetKey());
        $oDS->Set('scope_class', $aDef['scope_class']);
        $oDS->Set('scope_restriction', '');
        $oDS->Set('delete_policy', 'update');
        $oDS->Set('delete_policy_update', 'status:obsolete');
        $oDS->Set('full_load_periodicity', $iLoadPeriodicity);
        $iDsId = $oDS->DBInsert();
        echo "[provision-ds] DS '{$aDef['name']}' recreated id=$iDsId\n";
        $oRows = new DBObjectSet(DBSearch::FromOQL("SELECT SynchroAttribute WHERE sync_source_id=$iDsId"));
    }
    $iChanged = 0;
    while ($oAtt = $oRows->Fetch()) {
        $sCode = $oAtt->Get('attcode');
        $aMy = null;
        foreach ($aDef['attrs'] as $a) {
            if ($a['attcode'] === $sCode) { $aMy = $a; break; }
        }
        if ($aMy === null) {
            // 未纳入映射的属性：不参与对账、不更新（CSV 不含该列本就不会更新，保险起见关掉）
            if ($oAtt->Get('reconcile') || $oAtt->Get('update')) {
                $oAtt->Set('reconcile', false);
                $oAtt->Set('update', false);
                $oAtt->DBUpdate();
                $iChanged++;
            }
            continue;
        }
        if ($oAtt->Get('finalclass') === 'SynchroAttExtKey') {
            if ($oAtt->Get('reconciliation_attcode') !== 'name') {
                $oAtt->Set('reconciliation_attcode', 'name'); // 外键按目标对象 name 对账（Organization/Model）
                $oAtt->DBUpdate();
                $iChanged++;
            }
        }
        if ((bool)$oAtt->Get('reconcile') !== (bool)$aMy['reconcile'] || (bool)$oAtt->Get('update') !== (bool)$aMy['update']) {
            $oAtt->Set('reconcile', $aMy['reconcile']);
            $oAtt->Set('update', $aMy['update']);
            $oAtt->DBUpdate();
            $iChanged++;
        }
    }
    echo "[provision-ds] DS '{$aDef['name']}' normalized (rows changed: $iChanged)\n";
}

echo "[provision-ds] ALL DONE\n";
exit(0);
