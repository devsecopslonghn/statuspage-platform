#!/usr/bin/env python3
"""Operator-only live SSO smoke test. Creates and deletes one temporary Keycloak user.
Requires playwright+Chromium locally and kubectl read of keycloak-runtime.
Does not create incidents or change health. Re-saves existing display metadata.
"""
import base64
import json
import os
from pathlib import Path
import secrets
import subprocess
import urllib.parse
import urllib.request
from playwright.sync_api import sync_playwright, expect

ORIGIN = 'https://status-admin.apps.drgdevlab.com'
AUTH = 'https://auth.apps.drgdevlab.com'

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise RuntimeError('Credential-bearing redirect denied')

OPENER = urllib.request.build_opener(NoRedirect())

def main():
    secret = json.loads(subprocess.check_output(['kubectl','-n','keycloak','get','secret','keycloak-runtime','-o','json']))['data']
    payload = urllib.parse.urlencode({'client_id':'admin-cli','grant_type':'password',
        'username':base64.b64decode(secret['admin-username']).decode(),
        'password':base64.b64decode(secret['admin-password']).decode()}).encode()
    request = urllib.request.Request(AUTH+'/realms/master/protocol/openid-connect/token',data=payload,
                                     headers={'User-Agent':'DRGDevLab-StatusMonitor/1.0'})
    token = json.load(OPENER.open(request,timeout=20))['access_token']
    def api(method,path,data=None):
        request=urllib.request.Request(AUTH+'/admin/realms/kubernetes'+path,
            data=json.dumps(data).encode() if data is not None else None,
            headers={'Authorization':'Bearer '+token,'Content-Type':'application/json',
                     'User-Agent':'DRGDevLab-StatusMonitor/1.0'},method=method)
        with OPENER.open(request,timeout=20) as res:
            body=res.read();return json.loads(body) if body else None
    username='statuspage-test-'+secrets.token_hex(5)
    password=secrets.token_urlsafe(32)
    uid=None
    try:
        api('POST','/users',{'username':username,'email':username+'@example.com','emailVerified':True,
            'enabled':True,'firstName':'Statuspage','lastName':'Verification',
            'credentials':[{'type':'password','value':password,'temporary':False}]})
        uid=api('GET','/users?username='+username+'&exact=true')[0]['id']
        group=next(g for g in api('GET','/groups') if g['path']=='/k8s-cluster-admins')
        with sync_playwright() as p:
            browser=p.chromium.launch(headless=True)
            context=browser.new_context()
            r=context.request.get(ORIGIN+'/api/overview',headers={'X-Forwarded-User':'root',
                'X-Forwarded-Email':'root@example.com','X-Forwarded-Groups':'/k8s-cluster-admins'},max_redirects=0)
            assert r.status in (302,401,403), 'Unauthenticated header spoof must be denied'
            print('Unauthenticated/spoofed headers denied:',r.status,flush=True)
            page=context.new_page()
            page.goto(ORIGIN)
            page.locator('#username').fill(username);page.locator('#password').fill(password)
            page.locator('#kc-login').click()
            page.wait_for_url(ORIGIN+'/**',timeout=45000)
            assert page.locator('#page-form').count()==0, 'Non-admin must not access editor'
            assert context.request.get(ORIGIN+'/api/overview',max_redirects=0).status != 200
            print('Authenticated non-admin denied',flush=True)
            context.close()
            api('PUT','/users/'+uid+'/groups/'+group['id'])
            context=browser.new_context(viewport={'width':1440,'height':1100})
            page=context.new_page();errors=[]
            page.on('pageerror',lambda e:errors.append(str(e)))
            page.on('dialog',lambda d:d.accept())
            page.goto(ORIGIN)
            page.locator('#username').fill(username);page.locator('#password').fill(password)
            page.locator('#kc-login').click()
            page.wait_for_url(ORIGIN+'/',timeout=45000)
            expect(page.locator('#page-url')).to_have_value('https://drgdevlab.com/',timeout=45000)
            expect(page.locator('#components-table tbody tr')).to_have_count(13,timeout=30000)
            assert not errors, 'Browser JavaScript errors'
            print('Admin SSO and live data loaded: 10 components, 3 groups',flush=True)
            with page.expect_response(lambda r:r.url==ORIGIN+'/api/pages/hsyg2r4ql1pr' and r.request.method=='PATCH',timeout=60000) as response:
                page.locator('#btn-save-page').click()
            assert response.value.status==200, 'Page metadata write failed'
            expect(page.locator('#toast-container')).to_contain_text('Đã lưu cấu hình trang')
            page.locator('#btn-refresh').click()
            # Re-save a component unchanged, exercising group/showcase fields.
            row=page.locator('#components-table tbody tr').filter(has_text='DRG DevLab Website')
            row.get_by_role('button',name='Sửa',exact=True).click()
            with page.expect_response(lambda r:'/api/components/' in r.url and r.request.method=='PATCH',timeout=60000) as response:
                page.locator('#btn-save-comp').click()
            assert response.value.status==200, 'Component metadata write failed'
            expect(page.locator('#toast-container')).to_contain_text('Đã cập nhật')
            # CSRF rejects a write even for an authenticated browser.
            reject=context.request.patch(ORIGIN+'/api/pages/hsyg2r4ql1pr',data={'name':'DO NOT APPLY'},headers={'Origin':ORIGIN})
            assert reject.status==403
            audit=context.request.get(ORIGIN+'/api/audit').json()
            assert sum(a['result']=='SUCCESS' and username in a['user'] for a in audit)>=2
            print('Page/component writes and persistent audit passed; missing CSRF denied',flush=True)
            out=Path(os.environ.get('STATUSPAGE_SCREENSHOT_DIR','/tmp/statuspage-browser-evidence'));out.mkdir(parents=True,exist_ok=True)
            page.screenshot(path=str(out/'admin-desktop.png'),full_page=True)
            page.set_viewport_size({'width':390,'height':844})
            page.screenshot(path=str(out/'admin-mobile.png'),full_page=True)
            assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), 'Mobile page overflow'
            assert not errors
            browser.close()
        print('Browser verification completed',flush=True)
    finally:
        if uid:
            api('DELETE','/users/'+uid)
            print('Temporary verification user removed',flush=True)

if __name__=='__main__': main()
