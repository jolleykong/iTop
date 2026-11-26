#!/usr/bin/env python3
"""vc_sn_pull.py — 用 vCenter SOAP API 拉全部宿主硬件识别信息（零依赖）

用法: python3 vc_sn_pull.py <vc_host> <user> <password | @passfile> [output.json]
  密码推荐用 @/root/kk/.vc_pass 文件（chmod 600，单行密码），避免进 shell history
拉取字段: name / uuid / vendor / model / otherIdentifyingInfo(全类型 SN)
输出: JSON 列表 [{name, uuid, vendor, model, identifiers: [{type, label, value}]}]
"""
import ssl, sys, json, urllib.request
import xml.etree.ElementTree as ET

SOAP_LOGIN = """<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:vim="urn:vim25">
<soapenv:Body><vim:Login><vim:_this type="SessionManager">SessionManager</vim:_this>
<vim:userName>{u}</vim:userName><vim:password>{p}</vim:password></vim:Login></soapenv:Body></soapenv:Envelope>"""

SOAP_CONTENT = """<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:vim="urn:vim25">
<soapenv:Body><vim:RetrieveServiceContent><vim:_this type="ServiceInstance">ServiceInstance</vim:_this></vim:RetrieveServiceContent></soapenv:Body></soapenv:Envelope>"""

SOAP_HVPROPS = """<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:vim="urn:vim25">
<soapenv:Body>
<vim:RetrievePropertiesEx><vim:_this type="PropertyCollector">propertyCollector</vim:_this>
<vim:specSet>
 <vim:propSet><vim:type>HostSystem</vim:type><vim:all>false</vim:all>
  <vim:pathSet>name</vim:pathSet>
  <vim:pathSet>hardware.systemInfo</vim:pathSet>
  <vim:pathSet>summary.hardware.vendor</vim:pathSet>
  <vim:pathSet>summary.hardware.model</vim:pathSet>
 </vim:propSet>
 <vim:objectSet>
  <vim:obj type="ContainerView">{view}</vim:obj>
 </vim:objectSet>
</vim:specSet>
<vim:options><vim:maxObjects>2000</vim:maxObjects></vim:options>
</vim:RetrievePropertiesEx>
</soapenv:Body></soapenv:Envelope>"""

SOAP_CREATEVIEW = """<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:vim="urn:vim25">
<soapenv:Body>
<vim:CreateContainerView><vim:_this type="ViewManager">ViewManager</vim:_this>
<vim:container type="Folder">Folder-group-d1</vim:container>
<vim:type>HostSystem</vim:type><vim:recursive>true</vim:recursive>
</vim:CreateContainerView>
</soapenv:Body></soapenv:Envelope>"""


def soap(url, body, cookie=None):
    req = urllib.request.Request(url, data=body.encode(), headers={'Content-Type': 'text/xml; charset=utf-8'})
    if cookie:
        req.add_header('Cookie', cookie)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        resp = urllib.request.urlopen(req, timeout=120, context=ctx)
        return resp.read().decode('utf-8', 'replace'), resp.headers.get('Set-Cookie', '')
    except urllib.error.HTTPError as e:
        # vCenter 对 SOAP fault 返回 HTTP 500，读出 body 交给上层解析
        return e.read().decode('utf-8', 'replace'), e.headers.get('Set-Cookie', '')


def load_pwd(arg):
    """密码来源：'@/path/file' 读文件（首行，去换行）；否则用字面值。"""
    if arg.startswith('@'):
        with open(arg[1:]) as f:
            return f.readline().rstrip('\r\n')
    return arg


def main():
    vc, user = sys.argv[1], sys.argv[2]
    pwd = load_pwd(sys.argv[3])
    out = sys.argv[4] if len(sys.argv) > 4 else '/tmp/vc_hosts.json'
    url = f'https://{vc}/sdk'
    # 1) 登录
    body, hdr = soap(url, SOAP_CONTENT)
    root = ET.fromstring(body)
    si = root.iter('{urn:vim25}about').__next__()
    api_ver = si.find('{urn:vim25}apiVersion').text
    body, cookie = soap(url, SOAP_LOGIN.format(u=user, p=pwd))
    if 'LoginFault' in body or 'InvalidLogin' in body:
        print('AUTH FAIL: InvalidLogin'); sys.exit(2)
    print(f'login OK, apiVersion={api_ver}')
    # 2) 建 ContainerView（全部 HostSystem）
    body, _ = soap(url, SOAP_CREATEVIEW, cookie)
    root = ET.fromstring(body)
    rv = root.iter('{urn:vim25}returnval').__next__()
    mor_type, mor_val = rv.get('type'), rv.text.strip()
    print(f'view: {mor_type}={mor_val[:40]}')
    view_ref = f'<vim:obj type="{mor_type}">{mor_val}</vim:obj>'
    body2 = SOAP_HVPROPS.replace('{view}', mor_val)
    # 3) 拉 HostSystem 属性
    body, _ = soap(url, body2, cookie)
    root = ET.fromstring(body)
    hosts = []
    for obj in root.iter('{urn:vim25}objects'):
        props = {p.find('{urn:vim25}name').text: p for p in obj.findall('{urn:vim25}propSet')}
        row = {'name': '', 'uuid': '', 'vendor': '', 'model': '', 'identifiers': []}
        nm = props.get('name')
        if nm is not None:
            row['name'] = (nm.find('{urn:vim25}val').text or '')
        r = props.get('summary.hardware.vendor')
        if r is not None: row['vendor'] = (r.find('{urn:vim25}val').text or '')
        r = props.get('summary.hardware.model')
        if r is not None: row['model'] = (r.find('{urn:vim25}val').text or '')
        r = props.get('hardware.systemInfo')
        if r is not None:
            si2 = r.find('{urn:vim25}val')
            u = si2.find('{urn:vim25}uuid')
            if u is not None: row['uuid'] = (u.text or '').lower()
            for ident in si2.findall('{urn:vim25}identification'):
                for info in ident.findall('{urn:vim25}identifierValue'):
                    t = info.find('{urn:vim25}identifierType')
                    ttype = t.find('{urn:vim25}key').text if t is not None else '?'
                    tlabel = t.find('{urn:vim25}label').text if t is not None else ''
                    row['identifiers'].append({'type': ttype, 'label': tlabel,
                                               'value': (info.find('{urn:vim25}identifierValue').text or '')})
        hosts.append(row)
    json.dump(hosts, open(out, 'w'), ensure_ascii=False, indent=1)
    print(f'hosts: {len(hosts)} -> {out}')
    # 摘要
    withsn = sum(1 for h in hosts if any(i['value'] for i in h['identifiers']))
    print(f'identifiers 有值的宿主: {withsn}/{len(hosts)}')
    # 注销
    try:
        soap(url, '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:vim="urn:vim25"><soapenv:Body><vim:Logout><vim:_this type="SessionManager">SessionManager</vim:_this></vim:Logout></soapenv:Body></soapenv:Envelope>', cookie)
    except Exception:
        pass

if __name__ == '__main__':
    main()
