#!/usr/bin/env python3
import argparse
import concurrent.futures
import json
import logging
import math
import os
import re
import signal
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple

logging.basicConfig(level=logging.INFO, format='%(message)s', stream=sys.stdout)

app_state = {
    'last_success_at': 0.0,
    'cycles_total': 0,
    'cycles_failed': 0,
    'stale_seconds': 180,
    'running': True
}
app_state_lock = threading.Lock()
shutdown_event = threading.Event()

def extract_json_path(data: Any, path: str) -> Any:
    parts = path.split('.')
    current = data
    for part in parts:
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list):
            try:
                idx = int(part)
                current = current[idx]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return current

class StrictHttpsRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.startswith('https://'):
            raise urllib.error.URLError("Redirect to non-HTTPS forbidden")
        return super().redirect_request(req, fp, code, msg, headers, newurl)

opener = urllib.request.build_opener(StrictHttpsRedirectHandler())

def check_http(check: dict) -> Tuple[bool, Optional[float]]:
    url = check.get('url')
    if not url: return False, None
    expected_status = check.get('expected_status', [200])

    req = urllib.request.Request(url, headers={'User-Agent': 'DRGDevLab-StatusMonitor/1.0'})
    start = time.time()
    try:
        with opener.open(req, timeout=10) as resp:
            status = resp.status
            body = resp.read(65536)
    except urllib.error.HTTPError as e:
        status = e.code
        try:
            body = e.read(65536)
        except Exception:
            body = b""
    except Exception as e:
        return False, None

    lat = (time.time() - start) * 1000

    if status not in expected_status:
        return False, None

    try:
        body_str = body.decode('utf-8')
    except Exception:
        body_str = ""

    body_contains = check.get('body_contains')
    if body_contains and body_contains not in body_str:
        return False, None

    json_key = check.get('body_json_key')
    json_val = check.get('body_json_value')
    if json_key:
        try:
            data = json.loads(body_str)
            val = extract_json_path(data, json_key)
            if val is None or ('body_json_value' in check and val != json_val):
                return False, None
        except Exception:
            return False, None

    return True, lat

def check_prometheus(check: dict) -> bool:
    url = check.get('url')
    query = check.get('query')
    comp = check.get('comparison')
    try:
        thresh = float(check.get('threshold'))
    except (TypeError, ValueError):
        return False

    if not url or not query: return False

    params = urllib.parse.urlencode({'query': query})
    req_url = f"{url}/api/v1/query?{params}"
    req = urllib.request.Request(req_url)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read(65536).decode('utf-8')
            data = json.loads(body)
    except Exception:
        return False

    if data.get('status') != 'success':
        return False

    result_type = data.get('data', {}).get('resultType')
    result = data.get('data', {}).get('result', [])
    if result_type == 'scalar':
        result = [{'value': result}]
    elif result_type != 'vector':
        return False
    if not result:
        return False

    for item in result:
        try:
            timestamp, val_str = item['value']
            age = time.time() - float(timestamp)
            if not math.isfinite(age) or age < -30 or age > check.get('max_age_seconds', 90):
                return False
            val = float(val_str)
            if not math.isfinite(val):
                return False
        except Exception:
            return False

        if comp == 'eq':
            if val != thresh: return False
        elif comp == 'gte':
            if val < thresh: return False
        elif comp == 'lte':
            if val > thresh: return False
        else:
            return False
    return True

def run_http_check_with_retry(check: dict, is_external_once: bool) -> Tuple[bool, Optional[float]]:
    attempts = 3 if is_external_once else 1
    successes = []
    for i in range(attempts):
        success, latency = check_http(check)
        if success:
            successes.append(latency)
        if i < attempts - 1:
            time.sleep(1)
    if len(successes) >= (2 if is_external_once else 1):
        values = [x for x in successes if x is not None]
        return True, sum(values) / len(values) if values else None
    return False, None

class NoManagementRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise urllib.error.URLError('Management redirect forbidden')

management_opener = urllib.request.build_opener(NoManagementRedirect())

class StatuspageClient:
    def __init__(self, page_id: str, dry_run: bool):
        if not re.fullmatch(r'[a-zA-Z0-9_-]+', page_id):
            raise ValueError('Invalid page ID')
        self.page_id = page_id
        self.base_url = f"https://api.statuspage.io/v1/pages/{page_id}"
        self.dry_run = dry_run
        self.last_mutation_time = 0.0
        self.mutation_lock = threading.Lock()

    def get_api_key(self):
        kf = os.environ.get('STATUSPAGE_API_KEY_FILE')
        if kf and os.path.exists(kf):
            with open(kf, 'r') as f:
                return f.read().strip()
        k = os.environ.get('STATUSPAGE_API_KEY')
        if k:
            return k.strip()
        raise ValueError("Missing STATUSPAGE_API_KEY")

    def _request(self, method: str, path: str, data: dict = None, is_mutation: bool = False):
        if method != 'GET':
            is_mutation = True
        if is_mutation:
            if self.dry_run:
                logging.info(json.dumps({"msg": "dry-run mutation skipped", "method": method, "path": path}))
                return {}
        if not path.startswith('/') or '..' in path or '#' in path:
            raise ValueError('Invalid management path')
        url = self.base_url + path
        headers = {
            "Authorization": f"OAuth {self.get_api_key()}",
            "Content-Type": "application/json"
        }
        req_data = json.dumps(data).encode('utf-8') if data else None
        req = urllib.request.Request(url, data=req_data, headers=headers, method=method)

        backoff = 1.0
        for attempt in range(4):
            with self.mutation_lock:
                elapsed = time.monotonic() - self.last_mutation_time
                if elapsed < 1.5:
                    time.sleep(1.5 - elapsed)
                self.last_mutation_time = time.monotonic()
            try:
                with management_opener.open(req, timeout=10) as resp:
                    resp_body = resp.read(2 * 1024 * 1024)
                    if not resp_body:
                        return {}
                    return json.loads(resp_body.decode('utf-8'))
            except urllib.error.HTTPError as e:
                if e.code in (420, 429, 500, 502, 503, 504):
                    if attempt == 3:
                        raise Exception(f"Statuspage HTTPError {e.code}")
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 16.0)
                else:
                    raise Exception(f"Statuspage HTTPError {e.code}")
            except Exception as e:
                if attempt == 3:
                    raise Exception("Statuspage API request failed")
                time.sleep(backoff)
                backoff = min(backoff * 2, 16.0)

    def get_active_incidents_and_maintenance(self) -> Tuple[List[str], List[str]]:
        incidents = self._request("GET", "/incidents/unresolved")
        maintenances = self._request("GET", "/incidents/active_maintenance")

        incident_comp_ids = set()
        for inc in incidents:
            if not inc.get('components'):
                incident_comp_ids.add('*')
            for comp in inc.get('components', []):
                incident_comp_ids.add(comp['id'])

        maint_comp_ids = set()
        for m in maintenances:
            if not m.get('components'):
                maint_comp_ids.add('*')
            for comp in m.get('components', []):
                maint_comp_ids.add(comp['id'])

        return list(incident_comp_ids), list(maint_comp_ids)

    def get_components(self) -> dict:
        comps = self._request("GET", "/components?per_page=100")
        result = {}
        for c in comps:
            if c.get('page_id') != self.page_id:
                raise Exception("Page ID mismatch in component")
            result[c['id']] = c
        return result

    def update_component(self, comp_id: str, status: str):
        self._request("PATCH", f"/components/{comp_id}", data={"component": {"status": status}}, is_mutation=True)

    def post_metric(self, metric_id: str, value: float, ts: int):
        self._request("POST", f"/metrics/{metric_id}/data", data={"data": {"timestamp": ts, "value": value}}, is_mutation=True)


class ComponentManager:
    def __init__(self, comp_config: dict):
        self.config = comp_config
        self.state = None
        self.consec_counts = {"operational": 0, "partial_outage": 0, "major_outage": 0, "degraded_performance": 0}

    def process_raw_status(self, raw_status: str, failure_threshold: int, recovery_threshold: int, is_daemon_internal: bool) -> str:
        if not is_daemon_internal:
            self.state = raw_status
            return self.state

        if raw_status == self.state:
            for k in self.consec_counts:
                self.consec_counts[k] = 0
            return self.state

        self.consec_counts[raw_status] += 1
        for k in self.consec_counts:
            if k != raw_status:
                self.consec_counts[k] = 0

        threshold = recovery_threshold if raw_status == "operational" else failure_threshold
        if self.consec_counts[raw_status] >= threshold:
            self.state = raw_status
            for k in self.consec_counts:
                self.consec_counts[k] = 0

        return self.state

class HealthServerHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        if self.path == '/healthz':
            with app_state_lock:
                ok = app_state['last_success_at'] > 0 and (time.time() - app_state['last_success_at']) < app_state['stale_seconds']
                data = {
                    "ok": ok,
                    "last_success_at": app_state['last_success_at']
                }
            self.send_response(200 if ok else 503)
            self.send_header('Content-type', 'application/json')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(json.dumps(data).encode('utf-8'))
        elif self.path == '/metrics':
            with app_state_lock:
                data = (f"statuspage_sync_cycles_total {app_state['cycles_total']}\n"
                        f"statuspage_sync_cycles_failed_total {app_state['cycles_failed']}\n"
                        f"statuspage_sync_last_success_timestamp_seconds {app_state['last_success_at']}\n")
            self.send_response(200)
            self.send_header('Content-type', 'text/plain; version=0.0.4')
            self.end_headers()
            self.wfile.write(data.encode('utf-8'))
        else:
            self.send_response(404)
            self.end_headers()

def run_sync_cycle(config: dict, sp: StatuspageClient, comp_managers: dict, mode: str, is_once: bool) -> bool:
    is_external_once = mode == 'external' and is_once

    def run_checks(comp):
        if comp.get('mode', 'internal') != mode:
            return comp['key'], None, []

        results = []
        latencies = []
        for chk in comp.get('checks', []):
            if chk.get('type') == 'http':
                success, lat = run_http_check_with_retry(chk, is_external_once)
                results.append(success)
                if lat is not None:
                    latencies.append(lat)
            elif chk.get('type') == 'prometheus':
                success = check_prometheus(chk)
                results.append(success)
            else:
                results.append(False)
        return comp['key'], results, latencies

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(run_checks, comp): comp for comp in config.get('components', [])}
        check_outputs = {}
        for f in concurrent.futures.as_completed(futures):
            try:
                key, results, latencies = f.result()
            except Exception:
                comp = futures[f]
                key, results, latencies = comp['key'], [False] * len(comp['checks']), []
            if results is not None:
                check_outputs[key] = (results, latencies)

    try:
        incident_ids, maint_ids = sp.get_active_incidents_and_maintenance()
        sp_comps = sp.get_components()
        if any(c['id'] not in sp_comps for c in config['components'] if c.get('mode', 'internal') == mode):
            raise ValueError('Configured component missing from target page')
    except Exception as e:
        logging.error(json.dumps({"msg": "Failed to fetch Statuspage state", "error": str(e)}))
        return False

    all_executed_checks = 0
    all_passed_checks = 0
    all_latencies = []

    for comp in config.get('components', []):
        key = comp['key']
        if key not in check_outputs:
            continue

        results, latencies = check_outputs[key]
        all_executed_checks += len(results)
        all_passed_checks += sum(1 for r in results if r)
        all_latencies.extend(latencies)

        if not results:
            raw_status = comp.get('failure_status', 'major_outage')
        elif all(results):
            raw_status = 'operational'
        elif any(results):
            raw_status = 'partial_outage'
        else:
            raw_status = comp.get('failure_status', 'major_outage')

        is_daemon_internal = (not is_once) and (mode == 'internal')
        mgr = comp_managers[key]
        comp_id = comp['id']
        sp_status = sp_comps[comp_id]['status']
        if '*' in incident_ids or '*' in maint_ids or comp_id in incident_ids or comp_id in maint_ids or sp_status == 'under_maintenance':
            mgr.state = None
            mgr.consec_counts = dict.fromkeys(mgr.consec_counts, 0)
            logging.info(json.dumps({'component': key, 'manual_control': True}))
            continue
        final_status = mgr.process_raw_status(raw_status, config.get('failure_threshold', 3), config.get('recovery_threshold', 2), is_daemon_internal)

        logging.info(json.dumps({
            "component": key,
            "raw_status": raw_status,
            "final_status": final_status,
            "sp_status": sp_status
        }))

        if final_status is not None and sp_status != final_status:
            try:
                sp.update_component(comp_id, final_status)
            except Exception as e:
                logging.error(json.dumps({"msg": f"Failed to update component {key}", "error": str(e)}))
                return False

    now_ts = int(time.time())
    try:
        for metric in config.get('metrics', []):
            if metric.get('mode', 'internal') != mode:
                continue
            m_id = metric['id']
            if metric.get('kind') == 'checks_passing_percent':
                if all_executed_checks > 0:
                    val = (all_passed_checks / all_executed_checks) * 100.0
                    sp.post_metric(m_id, val, now_ts)
            elif metric.get('kind') == 'http_latency_ms':
                if all_latencies:
                    val = sum(all_latencies) / len(all_latencies)
                    sp.post_metric(m_id, val, now_ts)
    except Exception as e:
        logging.error(json.dumps({"msg": "Failed to update metrics", "error": str(e)}))
        return False

    return True

def validate_config(config):
    if not re.fullmatch(r'[a-zA-Z0-9_-]+', config.get('page_id', '')):
        raise ValueError('Invalid page ID')
    if not config.get('components'):
        raise ValueError('Components required')
    for setting in ('failure_threshold', 'recovery_threshold', 'interval_seconds', 'stale_seconds'):
        if setting in config and (not isinstance(config[setting], int) or config[setting] < 1):
            raise ValueError('Invalid timing configuration')
    keys, ids = set(), set()
    for comp in config['components']:
        key, cid = comp.get('key'), comp.get('id')
        if not key or key in keys or not re.fullmatch(r'[a-zA-Z0-9_-]+', cid or '') or cid in ids:
            raise ValueError('Missing or duplicate component identifier')
        keys.add(key)
        ids.add(cid)
        mode = comp.get('mode', 'internal')
        if mode not in ('internal', 'external') or not comp.get('checks'):
            raise ValueError('Invalid component mode or checks')
        if comp.get('failure_status', 'major_outage') not in ('major_outage', 'degraded_performance'):
            raise ValueError('Invalid component failure status')
        for check in comp['checks']:
            kind = check.get('type')
            if kind not in ('http', 'prometheus'):
                raise ValueError('Unknown check type')
            parsed = urllib.parse.urlparse(check.get('url', ''))
            internal = (parsed.hostname or '').endswith('.svc.cluster.local')
            if not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
                raise ValueError('Invalid check URL')
            if parsed.scheme != 'https' and not (parsed.scheme == 'http' and internal and mode == 'internal'):
                raise ValueError('Plain HTTP allowed only for internal cluster services')
            if kind == 'http':
                statuses = check.get('expected_status', [200])
                if not statuses or any(not isinstance(s, int) or s < 200 or s > 599 for s in statuses):
                    raise ValueError('Invalid expected HTTP status')
            else:
                if mode != 'internal' or check.get('comparison') not in ('eq', 'gte', 'lte') or not check.get('query'):
                    raise ValueError('Invalid Prometheus check')
                try:
                    valid = math.isfinite(float(check['threshold']))
                except (KeyError, TypeError, ValueError):
                    valid = False
                if not valid or check.get('max_age_seconds', 90) <= 0:
                    raise ValueError('Invalid Prometheus threshold or freshness')
    metric_ids = set()
    for metric in config.get('metrics', []):
        mid = metric.get('id', '')
        if not re.fullmatch(r'[a-zA-Z0-9_-]+', mid) or mid in metric_ids:
            raise ValueError('Invalid metric identifier')
        metric_ids.add(mid)
        if metric.get('kind') not in ('checks_passing_percent', 'http_latency_ms') or metric.get('mode', 'internal') not in ('internal', 'external'):
            raise ValueError('Invalid metric configuration')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    parser.add_argument('--mode', choices=['internal', 'external'], required=True)
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()

    kf = os.environ.get('STATUSPAGE_API_KEY_FILE')
    k = os.environ.get('STATUSPAGE_API_KEY')
    if not (kf and os.path.exists(kf)) and not k:
        logging.error(json.dumps({"msg": "API key missing"}))
        sys.exit(1)

    with open(args.config, 'r') as f:
        config = json.load(f)

    if not config.get('page_id'):
        logging.error(json.dumps({"msg": "page_id missing"}))
        sys.exit(1)

    validate_config(config)
    # Validate empty check list
    keys = set()
    ids = set()
    for comp in config.get('components', []):
        if not comp.get('id'):
            logging.error(json.dumps({"msg": "Component id missing"}))
            sys.exit(1)
        if comp['id'] in ids:
            logging.error(json.dumps({"msg": f"Duplicate component id {comp['id']}"}))
            sys.exit(1)
        ids.add(comp['id'])

        if not comp.get('key'):
            logging.error(json.dumps({"msg": "Component key missing"}))
            sys.exit(1)
        if comp['key'] in keys:
            logging.error(json.dumps({"msg": f"Duplicate component key {comp['key']}"}))
            sys.exit(1)
        keys.add(comp['key'])

        if not comp.get('checks'):
            logging.error(json.dumps({"msg": f"Component {comp['key']} checks empty"}))
            sys.exit(1)

    app_state['stale_seconds'] = config.get('stale_seconds', 180)

    sp = StatuspageClient(config['page_id'], args.dry_run)
    if not sp.get_api_key():
        raise ValueError('Empty API key')
    comp_managers = {c['key']: ComponentManager(c) for c in config.get('components', [])}

    def sigterm_handler(signum, frame):
        app_state['running'] = False
        shutdown_event.set()

    signal.signal(signal.SIGTERM, sigterm_handler)
    signal.signal(signal.SIGINT, sigterm_handler)

    if args.once:
        success = run_sync_cycle(config, sp, comp_managers, args.mode, args.once)
        if success:
            sys.exit(0)
        else:
            sys.exit(1)
    else:
        server = ThreadingHTTPServer(('0.0.0.0', config.get('listen_port', 8080)), HealthServerHandler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()

        while app_state['running']:
            cycle_start = time.time()
            try:
                success = run_sync_cycle(config, sp, comp_managers, args.mode, args.once)
            except Exception:
                logging.error(json.dumps({'msg': 'Sync cycle failed'}))
                success = False

            with app_state_lock:
                app_state['cycles_total'] += 1
                if success:
                    app_state['last_success_at'] = time.time()
                else:
                    app_state['cycles_failed'] += 1

            elapsed = time.time() - cycle_start
            sleep_time = max(0, config.get('interval_seconds', 60) - elapsed)

            if app_state['running'] and sleep_time > 0:
                shutdown_event.wait(sleep_time)

        server.shutdown()

if __name__ == '__main__':
    main()
