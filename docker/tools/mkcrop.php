<?php
/**
 * 生成裁剪后的 iTop 无人值守升级参数文件（仅保留 CMDB 相关模块）
 * 用法：php mkcrop.php <输出路径>
 * 数据库口令从 conf/production/config-itop.php 读取，不落盘到日志
 */
require_once('/var/www/html/approot.inc.php');

$sOut = $argv[1] ?? '/tmp/params-crop.xml';

$sConfigFile = '/var/www/html/conf/production/config-itop.php';
if (!file_exists($sConfigFile)) {
    fwrite(STDERR, "config not found: $sConfigFile\n");
    exit(2);
}
$MySettings = [];
$oConfig = null;
if (class_exists('Config')) {
    $oConfig = new Config($sConfigFile);
    $sDbHost = $oConfig->Get('db_host');
    $sDbUser = $oConfig->Get('db_user');
    $sDbPwd  = $oConfig->Get('db_pwd');
    $sDbName = $oConfig->Get('db_name');
    $sDbPre  = $oConfig->Get('db_subname');
    $sUrl    = $oConfig->Get('app_root_url');
} else {
    include($sConfigFile);
    $sDbHost = $MySettings['db_host'] ?? '';
    $sDbUser = $MySettings['db_user'] ?? '';
    $sDbPwd  = $MySettings['db_pwd'] ?? '';
    $sDbName = $MySettings['db_name'] ?? '';
    $sDbPre  = $MySettings['db_subname'] ?? '';
    $sUrl    = $MySettings['app_root_url'] ?? '';
}
if ($sDbHost === '' || $sDbPwd === '') {
    fwrite(STDERR, "cannot read db settings from config\n");
    exit(4);
}

// 保留的模块（仅 CMDB + 平台必需）
$aKeep = [
    'authent-cas',
    'authent-external',
    'authent-ldap',
    'authent-local',
    'combodo-backoffice-darkmoon-theme',
    'combodo-backoffice-fullmoon-high-contrast-theme',
    'combodo-backoffice-fullmoon-protanopia-deuteranopia-theme',
    'combodo-backoffice-fullmoon-tritanopia-theme',
    'combodo-data-feature-removal',
    'combodo-db-tools',
    'itop-attachments',
    'itop-backup',
    'itop-bridge-virtualization-storage',
    'itop-config',
    'itop-config-mgmt',
    'itop-container-mgmt',
    'itop-core-update',
    'itop-datacenter-mgmt',
    'itop-files-information',
    'itop-flow-map',
    'itop-oauth-client',
    'itop-portal-base',
    'itop-profiles-itil',
    'itop-sla-computation',
    'itop-storage-mgmt',
    'itop-structure',
    'itop-themes-compat',
    'itop-virtualization-mgmt',
    'itop-welcome-itil',
];

$sEsc = function ($s) { return htmlspecialchars((string)$s, ENT_QUOTES | ENT_XML1, 'UTF-8'); };

$sXml  = "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n";
$sXml .= "<installation>\n";
$sXml .= "  <mode>upgrade</mode>\n";
$sXml .= "  <optional_steps>\n";
$sXml .= "    <log-parameters>1</log-parameters>\n";
$sXml .= "    <migrate-before>1</migrate-before>\n";
$sXml .= "    <migrate-after>1</migrate-after>\n";
$sXml .= "    <copy>1</copy>\n";
$sXml .= "  </optional_steps>\n";
$sXml .= "  <source_dir>datamodels/2.x/</source_dir>\n";
$sXml .= "  <datamodel_version>3.3.0</datamodel_version>\n";
$sXml .= "  <previous_configuration_file>".$sEsc($sConfigFile)."</previous_configuration_file>\n";
$sXml .= "  <extensions_dir>extensions</extensions_dir>\n";
$sXml .= "  <target_env>production</target_env>\n";
$sXml .= "  <workspace_dir></workspace_dir>\n";
$sXml .= "  <database>\n";
$sXml .= "    <server>".$sEsc($sDbHost)."</server>\n";
$sXml .= "    <user>".$sEsc($sDbUser)."</user>\n";
$sXml .= "    <pwd>".$sEsc($sDbPwd)."</pwd>\n";
$sXml .= "    <name>".$sEsc($sDbName)."</name>\n";
$sXml .= "    <db_tls_enabled></db_tls_enabled>\n";
$sXml .= "    <db_tls_ca></db_tls_ca>\n";
$sXml .= "    <prefix>".$sEsc($sDbPre)."</prefix>\n";
$sXml .= "  </database>\n";
$sXml .= "  <url>".$sEsc($sUrl)."</url>\n";
$sXml .= "  <graphviz_path>/usr/bin/dot</graphviz_path>\n";
$sXml .= "  <admin_account>\n    <user></user>\n    <pwd></pwd>\n    <language></language>\n  </admin_account>\n";
$sXml .= "  <language></language>\n";
$sXml .= "  <selected_modules type=\"array\">\n";
foreach ($aKeep as $m) {
    $sXml .= "    <item>".$sEsc($m)."</item>\n";
}
$sXml .= "  </selected_modules>\n";
$sXml .= "  <sample_data></sample_data>\n";
$sXml .= "  <options type=\"array\"/>\n";
$sXml .= "  <mysql_bindir></mysql_bindir>\n";
$sXml .= "  <use_symbolic_links></use_symbolic_links>\n";
$sXml .= "</installation>\n";

if (file_put_contents($sOut, $sXml) === false) {
    fwrite(STDERR, "write failed: $sOut\n");
    exit(3);
}
@chmod($sOut, 0600);
printf("written: %s (%d modules kept)\n", $sOut, count($aKeep));
