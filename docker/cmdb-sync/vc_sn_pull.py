#!/usr/bin/env python3
"""vc_sn_pull.py — 用 vCenter SOAP API 拉全部宿主硬件识别信息（零依赖）

用法: python3 vc_sn_pull.py <vc_host> <user> <password | @passfile> [output.json]
  密码推荐用 @/root/kk/.vc_pass 文件（chmod 600，单行密码），避免进 shell history
拉取字段: name / uuid / vendor / model / otherIdentifyingInfo(全类型 SN)
输出: JSON 列表 [{name, uuid, vendor, model, identifiers: [{type, label, value}]}]

实现说明（2026-11-26 实测定稿）:
- 本 vCenter SDK 的 WSDL 不含 RetrievePropertiesEx（"Unable to resolve WSDL
  method name"），只支持老版 RetrieveProperties。
- 老版 RetrieveProperties 不支持 ContainerView 视图展开（视图当 obj 传入时
  类型不匹配，静默返回空列表，无任何报错）。
- 因此宿主枚举走 Folder→Datacenter→hostFolder→ComputeResource→HostSystem
  的 BFS 直读，每层权限均已实测可读。
"""
import ssl, sys, json, re, urllib.request
import xml.etree.ElementTree as ET

NS = '{urn:vim25}'

SOAP_CONTENT = """<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:vim="urn:vim25">
<soapenv:Body><vim:RetrieveServiceContent><vim:_this type="ServiceInstance">ServiceInstance</vim:_this></vim:RetrieveServiceContent></soapenv:Body></soapenv:Envelope>"""

SOAP_LOGIN = """<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:vim="urn:vim25">
<soapenv:Body><vim:Login><vim:_this type="SessionManager">SessionManager</vim:_this>
<vim:userName>{u}</vim:userName><vim:password>{p}</vim:password></vim:Login></soapenv:Body></soapenv:Envelope>"""

SOAP_LOGOUT = """<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:vim="urn:vim25">
<soapenv:Body><vim:Logout><vim:_this type="SessionManager">SessionManager</vim:_this></vim:Logout></soapenv:Body></soapenv:Envelope>"""


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


def fault(body):
    m = re.search(r'<faultstring>(.*?)</faultstring>', body, re.S)
    return m.group(1).strip() if m else None


def load_pwd(arg):
    """密码来源：'@/path/file' 读文件（首行，去换行）；否则用字面值。"""
    if arg.startswith('@'):
        with open(arg[1:]) as f:
            return f.readline().rstrip('\r\n')
    return arg


def retrieve_props(url, cookie, moref, mtype, paths):
    """老版 RetrieveProperties 直读单个对象属性。返回 (props dict | None, faultstr)。"""
    ps = ''.join(f'<vim:pathSet>{p}</vim:pathSet>' for p in paths)
    body = f"""<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:vim="urn:vim25">
<soapenv:Body><vim:RetrieveProperties><vim:_this type="PropertyCollector">propertyCollector</vim:_this>
<vim:specSet><vim:propSet><vim:type>{mtype}</vim:type>{ps}</vim:propSet>
<vim:objectSet><vim:obj type="{mtype}">{moref}</vim:obj></vim:objectSet>
</vim:specSet></vim:RetrieveProperties></soapenv:Body></soapenv:Envelope>"""
    b, _ = soap(url, body, cookie)
    f = fault(b)
    if f:
        return None, f
    props = {}
    xsi = '{http://www.w3.org/2001/XMLSchema-instance}type'
    for pr in ET.fromstring(b).iter(NS + 'propSet'):
        n = pr.find(NS + 'name').text
        v = pr.find(NS + 'val')
        if v is None:
            continue
        if v.get(xsi) == 'ArrayOfManagedObjectReference':
            props[n] = [m.text for m in v.findall(NS + 'ManagedObjectReference')]
        elif v.get(xsi) == 'ManagedObjectReference':
            props[n] = v.text  # 单引用（如 Datacenter.hostFolder）
        else:
            props[n] = v       # 复杂结构（如 hardware.systemInfo）保留原始元素
    return props, None


def moref_type(ref):
    return ref.split('-', 1)[0].lower()


def main():
    vc, user = sys.argv[1], sys.argv[2]
    pwd = load_pwd(sys.argv[3])
    out = sys.argv[4] if len(sys.argv) > 4 else '/tmp/vc_hosts.json'
    url = f'https://{vc}/sdk'

    # 1) ServiceContent: apiVersion + rootFolder
    body, _ = soap(url, SOAP_CONTENT)
    root = ET.fromstring(body)
    api_ver = root.iter(NS + 'apiVersion').__next__().text
    root_folder = root.iter(NS + 'rootFolder').__next__().text

    # 2) 登录
    body, cookie = soap(url, SOAP_LOGIN.format(u=user, p=pwd))
    if 'LoginResponse' not in body:
        f = fault(body) or 'unknown'
        print('LOGIN FAIL:', f[:150])
        sys.exit(2 if ('InvalidLogin' in f or 'Permission' in f) else 3)
    print(f'login OK, apiVersion={api_ver}, rootFolder={root_folder}')

    # 3) BFS 枚举: rootFolder -> Datacenter -> hostFolder -> (Folder/ComputeResource)* -> HostSystem
    hosts = []
    props, _ = retrieve_props(url, cookie, root_folder, 'Folder', ['childEntity'])
    folders = list((props or {}).get('childEntity') or [])
    dcs = []
    for ref in folders:
        t = moref_type(ref)
        if t == 'datacenter':
            dcs.append(ref)
        elif t == 'group':  # Folder
            p2, _ = retrieve_props(url, cookie, ref, 'Folder', ['childEntity'])
            dcs += [x for x in ((p2 or {}).get('childEntity') or []) if moref_type(x) == 'datacenter']
    print(f'datacenters: {len(dcs)}')
    for dc in dcs:
        p, _ = retrieve_props(url, cookie, dc, 'Datacenter', ['name', 'hostFolder'])
        if not p:
            print(f'  {dc}: 不可读(权限)，跳过'); continue
        p2, _ = retrieve_props(url, cookie, p['hostFolder'], 'Folder', ['childEntity'])
        # hostFolder 下是 ComputeResource（集群/独立宿主）或 Folder
        stack = list((p2 or {}).get('childEntity') or [])
        while stack:
            ref = stack.pop()
            t = moref_type(ref)
            if t == 'group':  # Folder
                p3, _ = retrieve_props(url, cookie, ref, 'Folder', ['childEntity'])
                stack += list((p3 or {}).get('childEntity') or [])
            elif t == 'domain':  # ComputeResource
                p3, _ = retrieve_props(url, cookie, ref, 'ComputeResource', ['name', 'host'])
                if p3:
                    h = p3.get('host')
                    hosts += (h if isinstance(h, list) else ([h] if h else []))
    print(f'hosts found: {len(hosts)}')

    # 4) 逐台读硬件识别属性（otherIdentifyingInfo 必须用子路径单独取，
    #    hardware.systemInfo 的返回值不含 identification 数组）
    rows = []
    for ref in hosts:
        p, f = retrieve_props(url, cookie, ref, 'HostSystem',
                              ['name', 'hardware.systemInfo', 'hardware.systemInfo.otherIdentifyingInfo',
                               'summary.hardware.vendor', 'summary.hardware.model'])
        if p is None:
            print(f'  {ref}: FAULT {f[:80]}'); continue
        row = {'moref': ref, 'name': '', 'uuid': '', 'vendor': '', 'model': '', 'identifiers': []}
        nm = p.get('name')
        if nm is not None and nm.text:
            row['name'] = nm.text
        row['vendor'] = (p.get('summary.hardware.vendor').text or '') if p.get('summary.hardware.vendor') is not None else ''
        row['model'] = (p.get('summary.hardware.model').text or '') if p.get('summary.hardware.model') is not None else ''
        sysinfo = p.get('hardware.systemInfo')
        if sysinfo is not None:
            u = sysinfo.find(f'{NS}uuid')
            if u is not None and u.text:
                row['uuid'] = u.text.lower()
        oii = p.get('hardware.systemInfo.otherIdentifyingInfo')
        if oii is not None:
            for info in oii.iter(NS + 'HostSystemIdentificationInfo'):
                ti = info.find(NS + 'identifierType')
                val = info.find(NS + 'identifierValue')
                row['identifiers'].append({
                    'type': ti.find(NS + 'key').text if ti is not None and ti.find(NS + 'key') is not None else '?',
                    'label': ti.find(NS + 'label').text if ti is not None and ti.find(NS + 'label') is not None else '',
                    'value': (val.text or '') if val is not None else ''})
        rows.append(row)

    json.dump(rows, open(out, 'w'), ensure_ascii=False, indent=1)
    print(f'hosts: {len(rows)} -> {out}')
    withsn = sum(1 for r in rows if any(i['value'] for i in r['identifiers']))
    print(f'identifiers 有值的宿主: {withsn}/{len(rows)}')
    try:
        soap(url, SOAP_LOGOUT, cookie)
    except Exception:
        pass


if __name__ == '__main__':
    main()
