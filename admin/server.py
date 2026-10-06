"""Internal display-metadata editor. OAuth proxy is the only network-facing server."""
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

sys.path.insert(0, os.environ.get('SYNC_MODULE_DIR', str(Path(__file__).resolve().parents[1] / 'statuspage-sync')))
from sync import StatuspageClient
from inventory import effective_observation, fetch_inventory, fetch_sync_observations, fresh, load_policy

PAGE_ID = 'hsyg2r4ql1pr'
ADMIN_GROUP = os.environ.get('ADMIN_GROUP', '/k8s-cluster-admins')
PUBLIC_ORIGIN = os.environ.get('PUBLIC_ORIGIN', 'https://status-admin.apps.drgdevlab.com')
DATA_DIR = os.environ.get('DATA_DIR', '/data')
CONFIG_PATH = os.environ.get('CONFIG_PATH', '')
DISCOVERY_CONFIG_PATH = os.environ.get('DISCOVERY_CONFIG_PATH', '')
api_lock = threading.RLock()
cache = {'overview': None, 'timestamp': 0}
client = StatuspageClient(PAGE_ID, False)

class ValidationError(Exception):
    pass

def get_db():
    os.makedirs(DATA_DIR, mode=0o700, exist_ok=True)
    conn = sqlite3.connect(os.path.join(DATA_DIR, 'audit.db'), timeout=10)
    conn.execute('''CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT DEFAULT CURRENT_TIMESTAMP,
        user TEXT, operation TEXT, target TEXT, changed_fields TEXT, result TEXT)''')
    conn.commit()
    return conn

def log_audit(user, target, fields, result):
    with get_db() as db:
        row = db.execute('INSERT INTO audit_log (user,operation,target,changed_fields,result) VALUES (?,?,?,?,?)',
                         (user, 'PATCH', target, json.dumps(sorted(fields)), result))
        return row.lastrowid

def finish_audit(row_id, result):
    with get_db() as db:
        db.execute('UPDATE audit_log SET result=? WHERE id=?', (result, row_id))

def get_csrf_secret():
    name = os.environ.get('ADMIN_CSRF_SECRET_FILE')
    if not name:
        raise RuntimeError('CSRF secret is required')
    secret = Path(name).read_bytes().strip()
    if len(secret) < 32:
        raise RuntimeError('CSRF secret must have at least 32 bytes')
    return secret

def generate_csrf_token(identity):
    msg = json.dumps([identity, int(time.time()) + 3600], separators=(',', ':')).encode()
    sig = hmac.new(get_csrf_secret(), msg, hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(msg).decode() + '.' + sig

def validate_csrf_token(token, identity):
    try:
        if not isinstance(token, str) or len(token) > 2048:
            return False
        encoded, sig = token.split('.')
        msg = base64.b64decode(encoded, altchars=b'-_', validate=True)
        who, expiry = json.loads(msg)
        now = time.time()
        return (who == identity and type(expiry) is int and now < expiry <= now + 3601
                and hmac.compare_digest(hmac.new(get_csrf_secret(), msg, hashlib.sha256).hexdigest(), sig))
    except (ValueError, TypeError, OSError, RuntimeError):
        return False

def validate_url(value):
    if not isinstance(value, str) or not value or len(value) > 255 or re.search(r'[\s\\\x00-\x1f\x7f]', value):
        return False
    try:
        u = urlparse(value)
        return bool(u.scheme == 'https' and u.hostname and u.username is None and u.password is None
                    and (u.port is None or 0 < u.port < 65536))
    except ValueError:
        return False

def validate_fields(data, kind):
    allowed = {'page': {'name', 'url'}, 'component': {'name', 'description', 'group_id', 'showcase'}, 'group': {'name'}}[kind]
    if not isinstance(data, dict) or not data or not set(data) <= allowed:
        raise ValidationError('Thông tin gửi lên có trường không được phép.')
    if 'name' in data and (not isinstance(data['name'], str) or not data['name'].strip()
                           or len(data['name']) > 255 or re.search(r'[\x00-\x1f\x7f]', data['name'])):
        raise ValidationError('Tên phải là văn bản từ 1 đến 255 ký tự.')
    if 'url' in data and not validate_url(data['url']):
        raise ValidationError('Website phải là URL HTTPS hợp lệ, không chứa thông tin đăng nhập.')
    if 'description' in data and (not isinstance(data['description'], str) or len(data['description']) > 5000
                                  or '\x00' in data['description']):
        raise ValidationError('Mô tả phải là văn bản, tối đa 5000 ký tự.')
    if 'showcase' in data and type(data['showcase']) is not bool:
        raise ValidationError('Lựa chọn uptime phải là true hoặc false.')
    if 'group_id' in data and data['group_id'] is not None and not isinstance(data['group_id'], str):
        raise ValidationError('Nhóm không hợp lệ.')

def fetch_all_components():
    result = []
    for number in range(1, 13):
        batch = client._request('GET', f'/components?page={number}&per_page=100')
        if not isinstance(batch, list) or any(c.get('page_id') != PAGE_ID for c in batch):
            raise RuntimeError('Component page mismatch')
        result.extend(batch)
        if len(batch) < 100:
            return result
    raise RuntimeError('Component pagination limit exceeded')

def fetch_overview():
    page = client._request('GET', '/')
    if page.get('id') != PAGE_ID or page.get('subdomain') != 'drgdevlab':
        raise RuntimeError('Page identity mismatch')
    raw = fetch_all_components()
    incidents = client._request('GET', '/incidents/unresolved')
    groups = [{'id': c['id'], 'name': c['name']} for c in raw if c.get('group')]
    components = [{k: c.get(k) for k in ('id','name','description','status','group_id','showcase')}
                  for c in raw if not c.get('group')]
    meta = []
    if CONFIG_PATH:
        config = json.loads(Path(CONFIG_PATH).read_text())
        if config['page_id'] != PAGE_ID:
            raise RuntimeError('Configuration page mismatch')
        meta = [{'key': c['key'], 'id': c['id'], 'mode': c.get('mode', 'internal'),
                 'check_count': len(c.get('checks', []))} for c in config['components']]
    return {'page': {'name': page.get('name'), 'url': page.get('url')}, 'groups': groups,
            'components': components, 'monitor_config': meta,
            'incidents': [{'name': i.get('name'), 'status': i.get('status'),
                           'shortlink': i.get('shortlink') if validate_url(i.get('shortlink')) else None}
                          for i in incidents], 'observed_at': int(time.time())}

def fetch_discovery_inventory():
    if not DISCOVERY_CONFIG_PATH:
        return {'available': False, 'reason': 'discovery_not_configured', 'rows': []}
    policy = load_policy(DISCOVERY_CONFIG_PATH)
    rules = {r['key']: r for r in policy['applications'] + policy.get('nodes', [])}
    try:
        snapshot = fetch_inventory(os.environ.get('INVENTORY_URL', 'http://statuspage-collector.statuspage.svc.cluster.local:8082/inventory'))
    except Exception:
        snapshot = {'generated_at': 0, 'sources': {}, 'rows': []}
    try:
        sync_observations = fetch_sync_observations()
    except Exception:
        sync_observations = {}
    rows = {r['key']: r for r in snapshot['rows']}
    for key, rule in rules.items():
        rows.setdefault(key, {'key': key, 'name': rule['name'], 'source_observed_at': 0})
    result = []
    fields = ('key', 'name', 'kind', 'source', 'url', 'aliases', 'secondary_endpoints', 'resources',
              'configured_resources', 'source_observed_at', 'last_seen_at', 'present', 'check_type', 'assurance',
              'expected_status', 'redirect', 'probe_allowed')
    for key, row in sorted(rows.items()):
        entry = {field: row.get(field) for field in fields}
        rule = rules.get(key, {})
        entry['logo_url'] = rule.get('logo_url')
        entry.update(component_id=rule.get('component_id'), publish=rule.get('publish', False),
                     retired=rule.get('retired', False), publication='retired' if rule.get('retired') else
                     ('public' if rule.get('publish') and rule.get('component_id') else
                      'approved_pending_mapping' if rule.get('publish') else 'internal_only'))
        obs = effective_observation(row, policy)
        entry['observation'] = {field: obs.get(field) for field in
                                ('status', 'reason', 'observed_at', 'http_status', 'latency_ms', 'redirect_hops', 'cordoned')}
        entry['conditions'] = {kind: {field: value.get(field) for field in
                                      ('status', 'reason', 'lastHeartbeatTime', 'lastTransitionTime')}
                               for kind, value in obs.get('conditions', {}).items()
                               if kind in ('Ready', 'MemoryPressure', 'DiskPressure', 'PIDPressure', 'NetworkUnavailable')}
        sync_value = sync_observations.get(key, {})
        if sync_value.get('component_id') == rule.get('component_id') and rule.get('component_id'):
            entry['sync'] = {field: sync_value.get(field) for field in
                             ('raw_status', 'final_status', 'statuspage_status', 'reason', 'observed_at', 'manual_control', 'check_results', 'write_error')}
            entry['sync']['stale'] = not fresh(sync_value.get('observed_at'), time.time(), policy['stale_seconds'])
        result.append(entry)
    return {'available': fresh(snapshot.get('generated_at'), time.time(), policy['stale_seconds']), 'generated_at': snapshot.get('generated_at', 0),
            'component_limit': policy.get('component_limit'), 'component_limit_evidence': policy.get('component_limit_evidence'),
            'sources': {kind: {'ok': value.get('ok') is True and fresh(value.get('observed_at'), time.time(), policy['stale_seconds']),
                               'observed_at': value.get('observed_at'), 'stale': not fresh(value.get('observed_at'), time.time(), policy['stale_seconds'])}
                        for kind, value in snapshot.get('sources', {}).items() if kind in ('node', 'ingress')}, 'rows': result}


def get_cached_overview(force=False):
    with api_lock:
        if force or cache['overview'] is None or time.monotonic() - cache['timestamp'] >= 15:
            cache['overview'] = fetch_overview()
            cache['timestamp'] = time.monotonic()
        return cache['overview']

def mutate(kind, target, data, user):
    validate_fields(data, kind)
    with api_lock:
        overview = get_cached_overview(force=True)
        if kind == 'page':
            if target != PAGE_ID:
                raise ValidationError('Trang không được phép.')
            path, payload = '/', {'page': data}
        elif kind == 'component':
            if not any(c['id'] == target for c in overview['components']):
                raise ValidationError('Thành phần không thuộc trang này.')
            gid = data.get('group_id')
            if gid is not None and not any(g['id'] == gid for g in overview['groups']):
                raise ValidationError('Nhóm không thuộc trang này.')
            # Statuspage documents the literal string "null" for ungrouping.
            values = dict(data)
            if 'group_id' in values and values['group_id'] is None:
                values['group_id'] = 'null'
            path, payload = '/components/' + target, {'component': values}
        else:
            if not any(g['id'] == target for g in overview['groups']):
                raise ValidationError('Nhóm không thuộc trang này.')
            members = [c['id'] for c in overview['components'] if c['group_id'] == target]
            path = '/component-groups/' + target
            payload = {'component_group': {'name': data['name'], 'components': members}}
        row = log_audit(user, kind + '/' + target, data.keys(), 'PENDING')
        try:
            client._request('PATCH', path, data=payload, is_mutation=True)
        except Exception:
            finish_audit(row, 'FAILED')
            raise
        finally:
            cache['overview'] = None
        finish_audit(row, 'SUCCESS')

class AdminHandler(BaseHTTPRequestHandler):
    server_version = 'StatuspageAdmin'

    def setup(self):
        super().setup()
        self.connection.settimeout(20)

    def log_message(self, *args):
        # Identity and operation are in the structured audit, not request URLs/headers.
        pass

    def respond(self, status, body, mime='application/json; charset=utf-8'):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        for name, value in {'Content-Type': mime, 'Content-Length': str(len(data)),
                            'Cache-Control': 'no-store', 'X-Frame-Options': 'DENY',
                            'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'same-origin',
                            'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"}.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def identity(self):
        user = self.headers.get('X-Forwarded-User', '')
        email = self.headers.get('X-Forwarded-Email', '')
        groups = self.headers.get('X-Forwarded-Groups', '').split(',')
        if not user or not email or len(user) > 255 or len(email) > 255:
            self.respond(401, {'error': 'Bạn cần đăng nhập.'})
            return None
        if ADMIN_GROUP not in [g.strip() for g in groups]:
            self.respond(403, {'error': 'Tài khoản không có quyền quản trị.'})
            return None
        return user + '|' + email

    def do_GET(self):
        if self.path == '/healthz':
            return self.respond(200, {'ok': True})
        identity = self.identity()
        if identity is None:
            return
        try:
            static = {'/': ('index.html','text/html; charset=utf-8'),
                      '/index.html': ('index.html','text/html; charset=utf-8'),
                      '/app.js': ('app.js','application/javascript; charset=utf-8'),
                      '/style.css': ('style.css','text/css; charset=utf-8')}
            if self.path in static:
                name, mime = static[self.path]
                return self.respond(200, Path(__file__).with_name(name).read_bytes(), mime)
            if self.path == '/api/session':
                return self.respond(200, {'csrf_token': generate_csrf_token(identity),
                                          'user': self.headers['X-Forwarded-User']})
            if self.path == '/api/inventory':
                return self.respond(200, fetch_discovery_inventory())
            if self.path == '/api/overview':
                return self.respond(200, get_cached_overview())
            if self.path == '/api/audit':
                with get_db() as db:
                    rows = db.execute('SELECT timestamp,user,operation,target,changed_fields,result FROM audit_log ORDER BY id DESC LIMIT 100').fetchall()
                keys = ['timestamp','user','operation','target','changed_fields','result']
                return self.respond(200, [dict(zip(keys, list(r[:4])+[json.loads(r[4]),r[5]])) for r in rows])
            self.respond(404, {'error': 'Không tìm thấy đường dẫn.'})
        except Exception:
            self.respond(502, {'error': 'Không thể đọc dữ liệu lúc này. Kiểm tra kết nối hoặc cấu hình server.'})

    def do_PATCH(self):
        identity = self.identity()
        if identity is None:
            return
        if self.headers.get('Origin') != PUBLIC_ORIGIN or not validate_csrf_token(self.headers.get('X-CSRF-Token'), identity):
            return self.respond(403, {'error': 'Phiên xác nhận không hợp lệ hoặc đã hết hạn. Hãy tải lại trang.'})
        if self.headers.get('Content-Type','').split(';')[0].strip() != 'application/json':
            return self.respond(415, {'error': 'Yêu cầu JSON.'})
        match = re.fullmatch(r'/api/(pages|components|groups)/([a-zA-Z0-9_-]+)', self.path)
        if not match:
            return self.respond(404, {'error': 'Không tìm thấy đường dẫn.'})
        try:
            if self.headers.get('Transfer-Encoding'):
                raise ValidationError('Transfer-Encoding không được hỗ trợ.')
            length = int(self.headers.get('Content-Length','0'))
            if length <= 0 or length > 32768:
                return self.respond(413, {'error': 'Nội dung phải từ 1 đến 32768 byte.'})
            data = json.loads(self.rfile.read(length))
            kind = {'pages':'page','components':'component','groups':'group'}[match[1]]
            mutate(kind, match[2], data, identity)
        except (ValidationError, ValueError, UnicodeError) as error:
            # Never return malformed request contents or raw upstream responses.
            log_audit(identity, match[1]+'/'+match[2], [], 'REJECTED')
            message = str(error) if isinstance(error, ValidationError) else 'JSON hoặc Content-Length không hợp lệ.'
            return self.respond(400, {'error': message})
        except Exception:
            return self.respond(502, {'error': 'Chưa thể xác nhận cập nhật. Kiểm tra audit và làm mới dữ liệu trước khi thử lại.'})
        self.respond(200, {'status': 'ok'})

def run():
    get_csrf_secret()  # Fail closed rather than boot with a predictable fallback.
    client.get_api_key()
    with get_db() as db:
        db.execute('SELECT 1')
    print('Statuspage admin listening on loopback:8081', flush=True)
    ThreadingHTTPServer(('127.0.0.1',8081), AdminHandler).serve_forever()

if __name__ == '__main__':
    run()
