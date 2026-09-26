#!/usr/bin/env python3
"""Create/update only statuspage-admin OIDC client and bootstrap its K8s Secret.
Requires operator kubectl access to keycloak-runtime; never prints credentials.
Namespace statuspage must already exist (Argo CD CreateNamespace).
"""
import base64
import json
import secrets
import subprocess
import urllib.parse
import urllib.request

BASE = 'https://auth.apps.drgdevlab.com'
CLIENT = 'statuspage-admin'
REDIRECT = 'https://status-admin.apps.drgdevlab.com/oauth2/callback'

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise RuntimeError('Credential-bearing redirect denied')

def main():
    raw = subprocess.check_output(['kubectl', '-n', 'keycloak', 'get', 'secret', 'keycloak-runtime', '-o', 'json'])
    source = json.loads(raw)['data']
    decode = lambda key: base64.b64decode(source[key]).decode()
    opener = urllib.request.build_opener(NoRedirect())
    data = urllib.parse.urlencode({'client_id': 'admin-cli', 'grant_type': 'password',
                                  'username': decode('admin-username'), 'password': decode('admin-password')}).encode()
    req = urllib.request.Request(BASE + '/realms/master/protocol/openid-connect/token', data=data,
                                 headers={'User-Agent': 'DRGDevLab-StatusMonitor/1.0'})
    token = json.load(opener.open(req, timeout=20))['access_token']
    def api(method, path, payload=None):
        req = urllib.request.Request(BASE + '/admin/realms/kubernetes' + path,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json',
                     'User-Agent': 'DRGDevLab-StatusMonitor/1.0'}, method=method)
        with opener.open(req, timeout=20) as res:
            body = res.read()
            return json.loads(body) if body else None
    groups = api('GET', '/groups')
    if not any(g.get('path') == '/k8s-cluster-admins' for g in groups):
        raise SystemExit('Expected existing admin group is absent; no privileges changed.')
    clients = api('GET', '/clients?clientId=' + CLIENT)
    payload = {'clientId': CLIENT, 'name': 'Statuspage Administration', 'enabled': True,
        'protocol': 'openid-connect', 'publicClient': False, 'standardFlowEnabled': True,
        'directAccessGrantsEnabled': False, 'serviceAccountsEnabled': False,
        'redirectUris': [REDIRECT], 'webOrigins': ['https://status-admin.apps.drgdevlab.com'],
        'attributes': {'pkce.code.challenge.method': 'S256'},
        'defaultClientScopes': ['web-origins', 'acr', 'profile', 'roles', 'email'],
        'protocolMappers': [{'name': 'statuspage-groups', 'protocol': 'openid-connect',
            'protocolMapper': 'oidc-group-membership-mapper', 'consentRequired': False,
            'config': {'claim.name': 'groups', 'full.path': 'true', 'id.token.claim': 'true',
                       'access.token.claim': 'true', 'userinfo.token.claim': 'true'}}]}
    if clients:
        cid = clients[0]['id']
        api('PUT', '/clients/' + cid, payload)
    else:
        api('POST', '/clients', payload)
        cid = api('GET', '/clients?clientId=' + CLIENT)[0]['id']
    client_secret = api('GET', '/clients/' + cid + '/client-secret')['value']
    old = subprocess.run(['kubectl', '-n', 'statuspage', 'get', 'secret', 'statuspage-admin-oauth',
                          '-o', 'json', '--ignore-not-found'], capture_output=True, check=True).stdout
    previous = json.loads(old)['data'] if old.strip() else {}
    keep = lambda key, default: base64.b64decode(previous[key]).decode() if key in previous else default
    secret = {'apiVersion': 'v1', 'kind': 'Secret',
        'metadata': {'name': 'statuspage-admin-oauth', 'namespace': 'statuspage'}, 'type': 'Opaque',
        'stringData': {'client-secret': client_secret,
            'cookie-secret': base64.urlsafe_b64encode(base64.b64decode(
                keep('cookie-secret', base64.b64encode(secrets.token_bytes(32)).decode()), altchars=b'-_')).decode(),
            'csrf-secret': keep('csrf-secret', secrets.token_hex(32))}}
    subprocess.run(['kubectl', 'apply', '--server-side', '--field-manager=statuspage-identity-bootstrap', '-f', '-'],
                   input=json.dumps(secret).encode(), check=True, stdout=subprocess.DEVNULL)
    print('OIDC client configured; statuspage/statuspage-admin-oauth provisioned. Existing group membership unchanged.')

if __name__ == '__main__':
    main()
