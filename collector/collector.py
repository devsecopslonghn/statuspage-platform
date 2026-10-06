"""Singleton, read-only Kubernetes collector with a durable inventory tombstone store."""
import json
import os
from pathlib import Path
import signal
import sqlite3
import ssl
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.environ.get('SYNC_MODULE_DIR', str(Path(__file__).resolve().parents[1] / 'statuspage-sync')))
from inventory import NoRedirect, collect_inventory, load_policy, fresh

TOKEN_DIR = Path('/var/run/secrets/kubernetes.io/serviceaccount')
CONFIG = os.environ.get('DISCOVERY_CONFIG_PATH', '/discovery/policy.json')
DB = os.environ.get('INVENTORY_DB', '/data/inventory.db')
lock = threading.Lock()
stop = threading.Event()
snapshot = {'version': 1, 'generated_at': 0, 'sources': {}, 'rows': []}


def list_resources(path):
    """Only two hard-coded resource paths; token reread on every paginated request."""
    if path not in ('/api/v1/nodes', '/apis/networking.k8s.io/v1/ingresses'):
        raise ValueError('Resource forbidden')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(),
        urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=str(TOKEN_DIR / 'ca.crt'))))
    items, continuation = [], ''
    for _ in range(100):
        query = urllib.parse.urlencode({'limit': 200, 'continue': continuation})
        req = urllib.request.Request('https://kubernetes.default.svc' + path + '?' + query,
            headers={'Authorization': 'Bearer ' + (TOKEN_DIR / 'token').read_text().strip()})
        with opener.open(req, timeout=10) as response:
            body = response.read(4 * 1024 * 1024 + 1)
        if len(body) > 4 * 1024 * 1024:
            raise ValueError('Resource list too large')
        data = json.loads(body)
        if not isinstance(data.get('items'), list):
            raise ValueError('Invalid resource list')
        items.extend(data['items'])
        continuation = data.get('metadata', {}).get('continue', '')
        if not continuation:
            return items
    raise ValueError('Pagination limit exceeded')


def read_previous():
    Path(DB).parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB) as db:
        db.execute('CREATE TABLE IF NOT EXISTS snapshot (id INTEGER PRIMARY KEY, payload TEXT NOT NULL)')
        row = db.execute('SELECT payload FROM snapshot WHERE id=1').fetchone()
        return json.loads(row[0]) if row else None


def write_snapshot(value):
    with sqlite3.connect(DB) as db:
        db.execute('INSERT OR REPLACE INTO snapshot VALUES (1, ?)', (json.dumps(value),))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        with lock:
            value = snapshot
        if self.path == '/inventory':
            status, body = 200, json.dumps(value).encode()
        elif self.path == '/healthz':
            ok = fresh(value['generated_at'], time.time(), 180) and all(s.get('ok') for s in value['sources'].values()) and len(value['sources']) == 2
            status, body = (200 if ok else 503), json.dumps({'ok': ok}).encode()
        else:
            status, body = 404, b'{}'
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def run():
    global snapshot
    policy = load_policy(CONFIG)
    snapshot = read_previous() or snapshot
    # A saved healthy payload must not appear fresh after restart before a new list/probe.
    snapshot['generated_at'] = 0
    for row in snapshot['rows']:
        row['source_observed_at'] = 0
    server = ThreadingHTTPServer(('0.0.0.0', 8082), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    while not stop.is_set():
        start = time.monotonic()
        sources = []
        for path in ('/apis/networking.k8s.io/v1/ingresses', '/api/v1/nodes'):
            try:
                sources.append(list_resources(path))
            except Exception:
                sources.append(None)  # Never print bearer tokens, responses or endpoint queries.
        try:
            value = collect_inventory(*sources, policy, previous=snapshot)
            write_snapshot(value)
            with lock:
                snapshot = value
            print(json.dumps({'event': 'inventory_observed', 'rows': len(value['rows']), 'sources': value['sources']}), flush=True)
        except Exception:
            print(json.dumps({'event': 'collector_cycle_failed'}), flush=True)
        stop.wait(max(0, policy['interval_seconds'] - (time.monotonic() - start)))
    server.shutdown()


if __name__ == '__main__':
    run()
