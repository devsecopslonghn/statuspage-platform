"""Read-only discovery contract and safe URL probes; no management credentials."""
import concurrent.futures
import copy
from datetime import datetime
import http.client
import ipaddress
import json
import math
from pathlib import Path
import re
import socket
import ssl
import subprocess
import sys
import time
import urllib.parse
import urllib.request

STATUSES = {'operational', 'major_outage', 'degraded_performance', 'partial_outage'}
INVENTORY_URL = 'http://statuspage-collector.statuspage.svc.cluster.local:8082/inventory'


def timestamp(value):
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    except (ValueError, TypeError, AttributeError):
        return 0


def fresh(value, now, maximum):
    return type(value) in (float, int) and math.isfinite(value) and -30 <= now - value <= maximum and value > 0


def validate_url(url, policy):
    if not isinstance(url, str) or len(url) > 2048 or re.search(r'[\s\\\x00-\x1f\x7f]', url):
        raise ValueError('invalid_url')
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname or ''
    # Literal addresses, credentials, non-standard ports, query and fragments are not discovered targets.
    if (parsed.scheme != 'https' or parsed.username is not None or parsed.password is not None
            or parsed.port not in (None, 443) or parsed.fragment or parsed.query
            or not re.fullmatch(r'[a-z0-9]+(?:[a-z0-9.-]*[a-z0-9])?', host)):
        raise ValueError('unsafe_url')
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError('literal_address_denied')
    if not (host in policy['allowed_hosts'] or any(host.endswith('.' + suffix)
                for suffix in policy.get('allowed_host_suffixes', []))):
        raise ValueError('host_not_allowed')
    path = urllib.parse.unquote(parsed.path or '/')
    if (not path.startswith('/') or re.search(r'[\s\\\x00-\x1f\x7f*{}()^$|\[\]]', path)
            or any(part in ('.', '..') for part in path.split('/')) or '//' in path):
        raise ValueError('unsafe_path')
    return parsed


def validate_policy(policy):
    if policy.get('version') != 1 or not isinstance(policy.get('applications'), list):
        raise ValueError('Invalid discovery policy')
    for field in ('allowed_hosts', 'allowed_host_suffixes'):
        if not isinstance(policy.get(field, []), list) or any(not re.fullmatch(r'[a-z0-9.-]+', h) for h in policy.get(field, [])):
            raise ValueError('Invalid host scope')
    if not policy.get('allowed_hosts'):
        raise ValueError('Host scope required')
    for field in ('interval_seconds', 'stale_seconds', 'node_heartbeat_seconds', 'timeout_seconds'):
        if type(policy.get(field)) is not int or not 1 <= policy[field] <= 3600:
            raise ValueError('Invalid discovery timing')
    if policy['timeout_seconds'] > 30:
        raise ValueError('Probe timeout too large')
    for host, addresses in policy.get('allowed_private_addresses', {}).items():
        validate_url('https://' + host + '/', policy)
        if not isinstance(addresses, list) or not addresses:
            raise ValueError('Invalid private ingress scope')
        for value in addresses:
            address = ipaddress.ip_address(value)
            if address.is_global or address.is_loopback or address.is_link_local or address.is_multicast or address.is_unspecified or address.is_reserved:
                raise ValueError('Unsafe private ingress scope')
    keys, resources, ids = set(), set(), set()
    for rule in policy['applications'] + policy.get('nodes', []):
        key = rule.get('key', '')
        if not re.fullmatch(r'[a-z0-9][a-z0-9_/-]*', key) or key in keys:
            raise ValueError('Invalid or duplicate inventory key')
        keys.add(key)
        if type(rule.get('publish', False)) is not bool or type(rule.get('retired', False)) is not bool:
            raise ValueError('Invalid publication/retirement')
        if rule.get('retired') and not rule.get('retirement_reason'):
            raise ValueError('Retirement requires a reason')
        cid = rule.get('component_id')
        if cid is not None:
            if not rule.get('publish') or not re.fullmatch(r'[a-zA-Z0-9_-]+', cid) or cid in ids:
                raise ValueError('Invalid component mapping')
            ids.add(cid)
        for resource in rule.get('resources', []):
            if not re.fullmatch(r'[a-z0-9-]+/[a-z0-9.-]+', resource) or resource in resources:
                raise ValueError('Ingress mapped to multiple applications')
            resources.add(resource)
        if rule.get('url'):
            validate_url(rule['url'], policy)
        if rule.get('logo_url'):
            validate_url(rule['logo_url'], policy)
        statuses = rule.get('expected_status', [200])
        if not statuses or any(type(s) is not int or not 200 <= s <= 599 for s in statuses):
            raise ValueError('Invalid expected status')
        if rule.get('redirect', 'deny') not in ('deny', 'same_host', 'allowlisted'):
            raise ValueError('Invalid redirect rule')
        for host in rule.get('redirect_hosts', []):
            validate_url('https://' + host + '/', policy)
    cap = policy.get('component_limit')
    if cap is not None and (type(cap) is not int or cap < 1 or not policy.get('component_limit_evidence')):
        raise ValueError('Verified component limit evidence required')


def load_policy(path):
    policy = json.loads(Path(path).read_text())
    validate_policy(policy)
    return policy


class PinnedHTTPS(http.client.HTTPSConnection):
    def connect(self):
        addresses = sorted({item[4][0] for item in socket.getaddrinfo(self.host, 443, type=socket.SOCK_STREAM)})
        allowed = getattr(self, 'allowed_addresses', ())
        if not addresses or any(not ipaddress.ip_address(address).is_global and address not in allowed for address in addresses):
            raise ValueError('non_public_address_denied')
        # Connect to a checked IP directly. TLS still validates the original hostname/SNI.
        self.sock = socket.create_connection((addresses[0], 443), self.timeout)
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)


def probe_url(url, rule, policy):
    start = time.monotonic()
    original = url
    status, hops = None, 0
    try:
        origin_host = validate_url(original, policy).hostname
        while True:
            parsed = validate_url(url, policy)
            remaining = policy['timeout_seconds'] - (time.monotonic() - start)
            if remaining <= 0:
                raise TimeoutError()
            conn = PinnedHTTPS(parsed.hostname, timeout=remaining, context=ssl.create_default_context())
            conn.allowed_addresses = policy.get('allowed_private_addresses', {}).get(parsed.hostname, [])
            try:
                conn.request('GET', parsed.path or '/', headers={'User-Agent': 'DRGDevLab-URLAvailability/1.0'})
                response = conn.getresponse()
                status = response.status
                location = response.getheader('Location')
            finally:
                conn.close()
            if status in (301, 302, 303, 307, 308) and rule.get('redirect', 'deny') != 'deny':
                if not location or hops >= 5:
                    raise ValueError('redirect_limit_or_missing_location')
                destination = urllib.parse.urljoin(url, location)
                # Redirects may legitimately carry SSO query parameters. Validate the target scope/path
                # without logging or persisting query values, then send the original query to that host.
                target = urllib.parse.urlsplit(destination)
                validate_url(urllib.parse.urlunsplit((target.scheme, target.netloc, target.path, '', '')), policy)
                allowed = [origin_host] if rule['redirect'] == 'same_host' else [origin_host] + rule.get('redirect_hosts', [])
                if target.hostname not in allowed or target.username is not None or target.password is not None or target.fragment:
                    raise ValueError('redirect_host_denied')
                # Query-bearing redirects use the same safe transport and validated host.
                if target.query:
                    return _probe_redirect_query(destination, rule, policy, start, hops + 1, allowed)
                url, hops = destination, hops + 1
                continue
            healthy = status in rule.get('expected_status', [200])
            return {'status': 'operational' if healthy else 'major_outage',
                    'reason': 'http_expected_status' if healthy else 'http_unexpected_status',
                    'http_status': status, 'latency_ms': round((time.monotonic() - start) * 1000, 2),
                    'redirect_hops': hops, 'observed_at': time.time()}
    except Exception as error:
        reason = str(error) if isinstance(error, ValueError) else type(error).__name__
        return {'status': 'major_outage', 'reason': reason, 'http_status': status,
                'latency_ms': None, 'redirect_hops': hops, 'observed_at': time.time()}


def _probe_redirect_query(url, rule, policy, start, hops, allowed):
    """Follow SSO redirects with a shared deadline; never export query/token values."""
    while True:
        target = urllib.parse.urlsplit(url)
        validate_url(urllib.parse.urlunsplit((target.scheme, target.netloc, target.path, '', '')), policy)
        if target.hostname not in allowed or target.fragment or hops > 5:
            raise ValueError('redirect_host_or_limit_denied')
        remaining = policy['timeout_seconds'] - (time.monotonic() - start)
        if remaining <= 0:
            raise TimeoutError()
        conn = PinnedHTTPS(target.hostname, timeout=remaining, context=ssl.create_default_context())
        conn.allowed_addresses = policy.get('allowed_private_addresses', {}).get(target.hostname, [])
        try:
            conn.request('GET', target.path + ('?' + target.query if target.query else ''),
                         headers={'User-Agent': 'DRGDevLab-URLAvailability/1.0'})
            response = conn.getresponse()
            status, location = response.status, response.getheader('Location')
        finally:
            conn.close()
        if status in (301, 302, 303, 307, 308):
            if not location:
                raise ValueError('redirect_missing_location')
            url, hops = urllib.parse.urljoin(url, location), hops + 1
            continue
        healthy = status in rule.get('expected_status', [200])
        return {'status': 'operational' if healthy else 'major_outage',
                'reason': 'http_expected_status' if healthy else 'http_unexpected_status',
                'http_status': status, 'redirect_hops': hops,
                'latency_ms': round((time.monotonic() - start) * 1000, 2), 'observed_at': time.time()}


def node_observation(node, policy, now):
    conditions = {c['type']: {k: c.get(k) for k in ('status', 'reason', 'lastHeartbeatTime', 'lastTransitionTime')}
                  for c in node.get('status', {}).get('conditions', []) if 'type' in c}
    ready = conditions.get('Ready', {})
    state, reason = 'operational', 'ready_no_pressure'
    if ready.get('status') == 'False':
        state, reason = 'major_outage', 'not_ready'
    elif ready.get('status') != 'True':
        state, reason = 'degraded_performance', 'ready_unknown'
    elif not fresh(timestamp(ready.get('lastHeartbeatTime')), now, policy['node_heartbeat_seconds']):
        state, reason = 'degraded_performance', 'node_heartbeat_stale'
    else:
        for kind in ('MemoryPressure', 'DiskPressure', 'PIDPressure'):
            condition = conditions.get(kind, {})
            if condition.get('status') == 'True':
                state, reason = 'degraded_performance', kind
                break
            if not fresh(timestamp(condition.get('lastHeartbeatTime')), now, policy['node_heartbeat_seconds']):
                state, reason = 'degraded_performance', 'pressure_heartbeat_stale_' + kind
                break
            if condition.get('status') != 'False':
                state, reason = 'degraded_performance', 'pressure_unknown_' + kind
                break
    if state == 'operational' and conditions.get('NetworkUnavailable', {}).get('status') == 'True':
        state, reason = 'degraded_performance', 'NetworkUnavailable'
    return {'status': state, 'reason': reason, 'conditions': conditions,
            'cordoned': bool(node.get('spec', {}).get('unschedulable')), 'observed_at': now}


def ingress_routes(ingress):
    spec = ingress.get('spec', {})
    tls = {h for item in spec.get('tls', []) for h in item.get('hosts', [])}
    routes = []
    for rule in spec.get('rules', []):
        host = rule.get('host', '')
        for path in rule.get('http', {}).get('paths', []):
            routes.append({'url': ('https' if host in tls else 'http') + '://' + host + (path.get('path') or '/'),
                           'host': host, 'path': path.get('path') or '/', 'tls': host in tls,
                           'path_type': path.get('pathType'), 'resource': ingress['metadata']['namespace'] + '/' + ingress['metadata']['name']})
    return routes


def bounded_probe_url(url, rule, policy):
    """A killable worker enforces a wall-clock limit, including DNS and slow TLS/headers."""
    try:
        scope = {field: policy.get(field, {} if field == 'allowed_private_addresses' else [])
                 for field in ('allowed_hosts', 'allowed_host_suffixes', 'allowed_private_addresses')}
        scope['timeout_seconds'] = policy['timeout_seconds']
        result = subprocess.run([sys.executable, '-B', str(Path(__file__).resolve()), '--probe-one'],
            input=json.dumps({'url': url, 'rule': rule, 'policy': scope}), text=True,
            capture_output=True, timeout=policy['timeout_seconds'] + 0.25, check=True)
        return json.loads(result.stdout)
    except Exception as error:
        return {'status': 'major_outage', 'reason': 'probe_deadline_exceeded' if isinstance(error, subprocess.TimeoutExpired) else 'probe_worker_failed',
                'http_status': None, 'latency_ms': None, 'observed_at': time.time()}


def collect_inventory(ingresses, nodes, policy, previous=None, now=None, probe=bounded_probe_url):
    now = now if now is not None else time.time()
    previous = previous or {'rows': []}
    rows = {r['key']: copy.deepcopy(r) for r in previous.get('rows', [])}
    app_rules = policy['applications']
    by_resource = {name: rule for rule in app_rules for name in rule.get('resources', [])}
    grouped = {}
    if ingresses is not None:
        for item in ingresses:
            resource = item['metadata']['namespace'] + '/' + item['metadata']['name']
            rule = by_resource.get(resource, {'key': 'ingress/' + resource, 'name': resource})
            entry = grouped.setdefault(rule['key'], {'rule': rule, 'routes': [], 'resources': []})
            entry['routes'].extend(ingress_routes(item))
            entry['resources'].append(resource)
    # Include configured resources even when absent, and external overrides independently.
    for rule in app_rules:
        grouped.setdefault(rule['key'], {'rule': rule, 'routes': [], 'resources': []})
    for key, group in grouped.items():
        rule, routes = group['rule'], sorted(group['routes'], key=lambda r: (r['path'] != '/', r['url']))
        present = bool(group['resources']) or rule.get('source') == 'external'
        canonical = rule.get('url') or (routes[0]['url'] if routes else None)
        row = rows.setdefault(key, {})
        previous_url = row.get('url')
        observed = now if (ingresses is not None or rule.get('source') == 'external') else row.get('source_observed_at', 0)
        row.update(key=key, kind='application', name=rule.get('name', key), source='external_override' if rule.get('source') == 'external' else 'kubernetes_ingress',
                   resources=group['resources'] if ingresses is not None else row.get('resources', []),
                   url=canonical if (ingresses is not None or rule.get('source') == 'external') else row.get('url'),
                   routes=routes if ingresses is not None else row.get('routes', []),
                   present=present, source_observed_at=observed, publish=rule.get('publish', False),
                   component_id=rule.get('component_id'), retired=rule.get('retired', False),
                   check_type='https_get_url_availability', assurance=rule.get('assurance', 'URL availability only; login/SSO does not prove backend or business readiness'),
                   expected_status=rule.get('expected_status', [200]), redirect=rule.get('redirect', 'deny'))
        row['configured_resources'] = rule.get('resources', [])
        row['aliases'] = sorted({r['url'] for r in row['routes'] if r['path'] == '/' and r['host'] in rule.get('alias_hosts', []) and r['url'] != row['url']})
        row['secondary_endpoints'] = sorted({r['url'] for r in row['routes'] if r['url'] != row['url'] and r['url'] not in row['aliases']})
        if present:
            row['last_seen_at'] = now
        if not present and ingresses is not None:
            row['url'] = canonical or previous_url

        if rule.get('retired'):
            reason = 'retired: ' + rule['retirement_reason']
        elif ingresses is None and rule.get('source') != 'external':
            reason = 'ingress_source_unavailable'
        elif not present:
            reason = 'ingress_missing'
        elif rule.get('exclude_reason'):
            reason = 'excluded: ' + rule['exclude_reason']
        elif not canonical:
            reason = 'no_route'
        elif rule.get('url') and routes and not any(r['host'] == urllib.parse.urlsplit(canonical).hostname for r in routes):
            reason = 'canonical_host_not_in_ingress'
        else:
            try:
                validate_url(canonical, policy)
                if routes and any(r['path_type'] == 'ImplementationSpecific' for r in routes if r['url'] == canonical):
                    raise ValueError('implementation_specific_path_requires_override')
                reason = None
            except ValueError as error:
                reason = str(error)
        row['probe_allowed'] = reason is None
        if reason:
            row['observation'] = {'status': 'degraded_performance', 'reason': reason, 'observed_at': now}
        else:
            row['last_seen_at'] = now
    if ingresses is not None:
        for key, row in rows.items():
            if row['kind'] == 'application' and key not in grouped:
                row.update(present=False, source_observed_at=now, probe_allowed=False,
                           observation={'status': 'degraded_performance', 'reason': 'ingress_missing', 'observed_at': now})
    else:
        for row in rows.values():
            if row.get('source') == 'kubernetes_ingress':
                row.update(probe_allowed=False, observation={'status': 'degraded_performance', 'reason': 'ingress_source_unavailable', 'observed_at': now})
    node_rules = {r['name']: r for r in policy.get('nodes', [])}
    seen = set()
    for node in nodes or []:
        name = node['metadata']['name']
        rule = node_rules.get(name, {'key': 'node/' + name, 'name': name})
        key = rule['key']
        seen.add(key)
        row = rows.setdefault(key, {})
        row.update(key=key, kind='node', name=name, source='kubernetes_node', resources=[name], present=True,
                   publish=rule.get('publish', False), component_id=rule.get('component_id'), retired=rule.get('retired', False),
                   last_seen_at=now, source_observed_at=now, check_type='kubernetes_node_conditions',
                   assurance='Kubernetes Ready, pressure conditions and Node heartbeat; no workload/business guarantee',
                   observation=node_observation(node, policy, now))
    for rule in policy.get('nodes', []):
        rows.setdefault(rule['key'], {'key': rule['key'], 'kind': 'node', 'name': rule['name'],
                                      'source': 'kubernetes_node', 'source_observed_at': 0})
    for key, row in rows.items():
        if row['kind'] != 'node':
            continue
        rule = node_rules.get(row['name'], {})
        row.update(publish=rule.get('publish', False), component_id=rule.get('component_id'), retired=rule.get('retired', False))
        if key not in seen:
            row.update(present=False, observation={'status': 'degraded_performance', 'reason': 'node_source_unavailable' if nodes is None else 'node_missing', 'observed_at': now})
            if nodes is not None:
                row['source_observed_at'] = now
        if rule.get('retired'):
            row['observation'] = {'status': 'degraded_performance', 'reason': 'retired: ' + rule['retirement_reason'], 'observed_at': now}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(probe, r['url'], grouped[r['key']]['rule'], policy): r
                   for r in rows.values() if r.get('probe_allowed')}
        for future in concurrent.futures.as_completed(futures):
            futures[future]['observation'] = future.result()
    return {'version': 1, 'generated_at': now, 'sources': {
        'ingress': {'ok': ingresses is not None, 'observed_at': now if ingresses is not None else 0},
        'node': {'ok': nodes is not None, 'observed_at': now if nodes is not None else 0}},
        'rows': sorted(rows.values(), key=lambda r: r['key'])}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError('internal_redirect_denied')


def fetch_inventory(url=INVENTORY_URL):
    if url != INVENTORY_URL:
        raise ValueError('Inventory endpoint is fixed')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(url, timeout=5) as response:
        body = response.read(4 * 1024 * 1024 + 1)
        if len(body) > 4 * 1024 * 1024:
            raise ValueError('Inventory too large')
        data = json.loads(body)
    if data.get('version') != 1 or not isinstance(data.get('rows'), list):
        raise ValueError('Invalid inventory contract')
    if len({r['key'] for r in data['rows']}) != len(data['rows']):
        raise ValueError('Duplicate inventory key')
    return data


def effective_observation(row, policy, now=None):
    now = time.time() if now is None else now
    observation = row.get('observation', {})
    if not fresh(row.get('source_observed_at'), now, policy['stale_seconds']):
        return {'status': 'degraded_performance', 'reason': 'source_stale', 'observed_at': observation.get('observed_at', 0)}
    if not fresh(observation.get('observed_at'), now, policy['stale_seconds']):
        return {'status': 'degraded_performance', 'reason': 'observation_stale', 'observed_at': observation.get('observed_at', 0)}
    if observation.get('status') not in STATUSES:
        return {'status': 'degraded_performance', 'reason': 'invalid_observation', 'observed_at': observation.get('observed_at', 0)}
    return observation


def fetch_sync_observations():
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open('http://statuspage-sync.statuspage.svc.cluster.local:8080/observations', timeout=5) as response:
        body = response.read(4 * 1024 * 1024 + 1)
        if len(body) > 4 * 1024 * 1024:
            raise ValueError('Observations too large')
        data = json.loads(body)
    if data.get('version') != 1 or not isinstance(data.get('components'), dict):
        raise ValueError('Invalid sync observation contract')
    return data['components']


if __name__ == '__main__' and sys.argv[1:] == ['--probe-one']:
    value = json.loads(sys.stdin.read(4 * 1024 * 1024))
    print(json.dumps(probe_url(value['url'], value['rule'], value['policy'])))
