"""Exercise actual HTTP routing/serialization with only the upstream API mocked."""
import http.client
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'admin'))
import server

class AdminHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.secret = Path(self.tmp.name) / 'csrf'
        self.secret.write_text('a' * 64)
        self.env = patch.dict(os.environ, {'ADMIN_CSRF_SECRET_FILE': str(self.secret)})
        self.env.start()
        self.attrs = patch.multiple(server, DATA_DIR=self.tmp.name, CONFIG_PATH='',
                                    PUBLIC_ORIGIN='https://status-admin.apps.drgdevlab.com',
                                    cache={'overview':None,'timestamp':0})
        self.attrs.start()
        self.api = MagicMock()
        self.api._request.side_effect = self.upstream
        self.client_patch = patch.object(server, 'client', self.api)
        self.client_patch.start()
        self.httpd = server.ThreadingHTTPServer(('127.0.0.1',0), server.AdminHandler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.headers = {'X-Forwarded-User':'operator', 'X-Forwarded-Email':'admin@example.com',
                        'X-Forwarded-Groups':'/k8s-cluster-admins', 'Origin':server.PUBLIC_ORIGIN,
                        'Content-Type':'application/json',
                        'X-CSRF-Token':server.generate_csrf_token('operator|admin@example.com')}

    def tearDown(self):
        self.httpd.shutdown();self.httpd.server_close();self.thread.join()
        self.client_patch.stop();self.attrs.stop();self.env.stop();self.tmp.cleanup()

    def upstream(self, method, path, **kwargs):
        if method == 'PATCH': return {}
        if path == '/':
            return {'id':server.PAGE_ID,'subdomain':'drgdevlab','name':'DRG DevLab','url':'https://drgdevlab.com/', 'automation_email':'SECRET'}
        if path.startswith('/components?'):
            return [{'id':'c1','page_id':server.PAGE_ID,'name':'Website','status':'operational','group_id':'g1','showcase':True,'automation_email':'SECRET'},
                    {'id':'g1','page_id':server.PAGE_ID,'group':True,'name':'Applications'}]
        if path == '/incidents/unresolved':
            return [{'name':'Example','status':'investigating','shortlink':'javascript:alert(1)','internal':'SECRET'}]
        raise AssertionError(path)

    def request(self, method, path, data=None, headers=None):
        conn = http.client.HTTPConnection(*self.httpd.server_address,timeout=5)
        conn.request(method,path,body=json.dumps(data) if data is not None else None,
                     headers=self.headers if headers is None else headers)
        r=conn.getresponse();body=r.read();result=(r.status,dict(r.getheaders()),body);conn.close();return result

    def test_anonymous_and_non_admin_denied(self):
        for headers,code in [({},401),({**self.headers,'X-Forwarded-Groups':'/k8s-viewers'},403)]:
            with self.subTest(code=code):
                self.assertEqual(self.request('GET','/api/overview',headers=headers)[0],code)
        self.assertEqual(self.request('GET','/healthz',headers={})[0],200)
        self.api._request.assert_not_called()

    def test_inventory_route_keeps_group_gate_and_does_not_need_management_api(self):
        with patch('server.fetch_discovery_inventory', return_value={'available':False,'rows':[]}):
            for headers, code in [({},401), ({**self.headers,'X-Forwarded-Groups':'/k8s-viewers'},403)]:
                self.assertEqual(self.request('GET','/api/inventory',headers=headers)[0],code)
            code, headers, body = self.request('GET','/api/inventory')
            self.assertEqual(code,200)
            self.assertEqual(json.loads(body)['available'],False)
            self.assertEqual(headers['Cache-Control'],'no-store')
        self.api._request.assert_not_called()

    def test_csrf_identity_expiry_and_secret_required(self):
        t=server.generate_csrf_token('operator|admin@example.com')
        self.assertTrue(server.validate_csrf_token(t,'operator|admin@example.com'))
        self.assertFalse(server.validate_csrf_token(t,'attacker|admin@example.com'))
        with patch('server.time.time', return_value=time.time()+3602):
            self.assertFalse(server.validate_csrf_token(t,'operator|admin@example.com'))
        with patch.dict(os.environ, {'ADMIN_CSRF_SECRET_FILE':''}):
            with self.assertRaises(RuntimeError): server.generate_csrf_token('x')

    def test_cross_origin_and_bad_csrf_never_write(self):
        for delta in [{'Origin':'https://evil.example'}, {'X-CSRF-Token':'bad'}, {'Origin':''}]:
            self.assertEqual(self.request('PATCH','/api/pages/'+server.PAGE_ID,{'name':'X'}, {**self.headers,**delta})[0],403)
        self.api._request.assert_not_called()

    def test_page_patch_exact_api_path_and_persistent_audit(self):
        code,headers,_=self.request('PATCH','/api/pages/'+server.PAGE_ID,{'name':'DRG DevLab','url':'https://drgdevlab.com/'})
        self.assertEqual(code,200);self.assertEqual(headers['Cache-Control'],'no-store')
        self.api._request.assert_any_call('PATCH','/',data={'page':{'name':'DRG DevLab','url':'https://drgdevlab.com/'}},is_mutation=True)
        rows=json.loads(self.request('GET','/api/audit')[2]);self.assertEqual(rows[0]['result'],'SUCCESS')
        self.assertEqual(rows[0]['user'],'operator|admin@example.com')

    def test_public_page_context_keeps_csrf_and_field_boundaries(self):
        content = {'headline': 'Trạng thái cụm Kubernetes',
                   'page_description': 'Node Ready và pressure.\nKhông kiểm chứng job/build.'}
        self.assertEqual(self.request('PATCH','/api/pages/'+server.PAGE_ID,content,
                                      {**self.headers,'X-CSRF-Token':'bad'})[0],403)
        self.api._request.assert_not_called()
        self.assertEqual(self.request('PATCH','/api/pages/'+server.PAGE_ID,content)[0],200)
        self.api._request.assert_any_call('PATCH','/',data={'page':content},is_mutation=True)
        writes = sum(c.args[0] == 'PATCH' for c in self.api._request.call_args_list)
        for bad in ({'headline': False}, {'headline': 'x'*256}, {'page_description': []},
                    {'page_description': 'x'*5001}, {'page_description': 'bad\x00'},
                    {'headline': 'x', 'custom_html': '<script>'}, {'status': 'operational'}):
            with self.subTest(data=str(bad)[:50]):
                self.assertEqual(self.request('PATCH','/api/pages/'+server.PAGE_ID,bad)[0],400)
        self.assertEqual(sum(c.args[0] == 'PATCH' for c in self.api._request.call_args_list),writes)

    def test_public_context_is_sanitized_and_not_allowed_on_components(self):
        original = self.upstream
        def context(method,path,**kw):
            result = original(method,path,**kw)
            if method == 'GET' and path == '/':
                result.update(headline='Cụm',page_description='Phạm vi kiểm tra')
            return result
        self.api._request.side_effect = context
        code,_,body = self.request('GET','/api/overview')
        self.assertEqual(code,200)
        self.assertEqual(json.loads(body)['page']['headline'],'Cụm')
        self.assertEqual(json.loads(body)['page']['page_description'],'Phạm vi kiểm tra')
        self.assertNotIn(b'SECRET',body)
        self.assertEqual(self.request('PATCH','/api/components/c1',{'headline':'X'})[0],400)

    def test_sanitized_overview_and_cache(self):
        code,headers,body=self.request('GET','/api/overview');self.assertEqual(code,200)
        self.assertNotIn(b'SECRET',body);self.assertNotIn(b'javascript:',body)
        self.assertEqual(headers['X-Frame-Options'],'DENY')
        self.request('GET','/api/overview');self.assertEqual(self.api._request.call_count,3)

    def test_page_mismatch_fails_closed(self):
        self.api._request.side_effect=lambda *a,**k: {'id':'wrong','subdomain':'other'}
        code,_,body=self.request('GET','/api/overview');self.assertEqual(code,502)
        self.assertNotIn(b'wrong',body)

    def test_component_input_types_and_fields(self):
        for bad in [{'status':'major_outage'},{'showcase':'false'},{'name':3},{'description':['x']},
                    {'group_id':{}},[],None,{'name':''},{'name':'x'*256},{'description':'x'*5001}]:
            with self.subTest(data=str(bad)[:40]):
                code=self.request('PATCH','/api/components/c1',bad)[0]
                self.assertIn(code,[400,413])
        self.api._request.assert_not_called()

    def test_component_unknown_and_wrong_group(self):
        self.assertEqual(self.request('PATCH','/api/components/foreign',{'name':'X'})[0],400)
        self.assertEqual(self.request('PATCH','/api/components/c1',{'group_id':'foreign'})[0],400)
        self.assertFalse(any(c.args[0]=='PATCH' for c in self.api._request.call_args_list))

    def test_component_ungroup_and_showcase(self):
        self.assertEqual(self.request('PATCH','/api/components/c1',{'group_id':None,'showcase':False})[0],200)
        self.api._request.assert_any_call('PATCH','/components/c1',data={'component':{'group_id':'null','showcase':False}},is_mutation=True)

    def test_group_rename_preserves_members(self):
        self.assertEqual(self.request('PATCH','/api/groups/g1',{'name':'Services'})[0],200)
        self.api._request.assert_any_call('PATCH','/component-groups/g1',data={'component_group':{'name':'Services','components':['c1']}},is_mutation=True)

    def test_api_failure_not_exposed_and_audited(self):
        original=self.upstream
        def fail(method,path,**kw):
            if method=='PATCH': raise RuntimeError('SECRET_TOKEN')
            return original(method,path,**kw)
        self.api._request.side_effect=fail
        code,_,body=self.request('PATCH','/api/components/c1',{'name':'X'})
        self.assertEqual(code,502);self.assertNotIn(b'SECRET_TOKEN',body)
        self.assertEqual(json.loads(self.request('GET','/api/audit')[2])[0]['result'],'FAILED')

    def test_unsupported_routes_and_large_bodies(self):
        self.assertEqual(self.request('GET','/does-not-exist')[0],404)
        self.assertEqual(self.request('PATCH','/api/pages/'+server.PAGE_ID,{'name':'a'*33000})[0],413)
        self.assertEqual(self.request('PATCH','/api/pages/x/extra',{'name':'X'})[0],404)

    def test_url_rules(self):
        for valid in ['https://drgdevlab.com/','https://example.com/a?b=1']:
            self.assertTrue(server.validate_url(valid))
        for bad in [None,123,'http://example.com','https://user:pass@example.com','javascript:alert(1)',
                    'https://a b.com','https://example.com\\evil','https://example.com:invalid']:
            self.assertFalse(server.validate_url(bad))

if __name__=='__main__': unittest.main()
