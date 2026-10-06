"""Contract tests for discovery, node observations, and bounded URL probes."""
import importlib.util
from pathlib import Path
import socket
import tempfile
import time
import sys
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'statuspage-sync'))
import inventory

spec = importlib.util.spec_from_file_location('statuspage_collector', ROOT / 'collector' / 'collector.py')
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)


NOW = 1_800_000_000.0


def policy():
    return {
        'version': 1, 'interval_seconds': 60, 'stale_seconds': 180,
        'node_heartbeat_seconds': 600, 'timeout_seconds': 4,
        'allowed_hosts': ['example.com', 'www.example.com', 'alias.example.com',
                          'api.example.com', 'login.example.net'],
        'allowed_host_suffixes': ['apps.example.com'],
        'applications': [], 'nodes': [],
    }


def ingress(namespace, name, host, paths=('/',), tls=True):
    return {'metadata': {'namespace': namespace, 'name': name}, 'spec': {
        'tls': [{'hosts': [host]}] if tls else [],
        'rules': [{'host': host, 'http': {'paths': [
            {'path': path, 'pathType': 'Prefix'} for path in paths]}}]}}


def node(name='worker-a', ready='True', heartbeat=None, extra=(), unschedulable=False):
    heartbeat = heartbeat or '2027-01-15T08:00:00Z'
    conditions = [{'type': 'Ready', 'status': ready,
                   'lastHeartbeatTime': heartbeat, 'reason': 'KubeletReady'}]
    conditions.extend({'type': kind, 'status': 'False'} for kind in
                       ('MemoryPressure', 'DiskPressure', 'PIDPressure'))
    for condition in conditions[1:]:
        condition['lastHeartbeatTime'] = heartbeat
    conditions.extend({'type': kind, 'status': status,
                       'lastHeartbeatTime': heartbeat} for kind, status in extra)
    return {'metadata': {'name': name}, 'spec': {'unschedulable': unschedulable},
            'status': {'conditions': conditions}}


class TestDiscoveryInventory(unittest.TestCase):
    def test_configured_include_exclude_alias_secondary_and_duplicate_resources_group(self):
        p = policy()
        p['applications'] = [
            {'key': 'app', 'name': 'Application', 'resources': ['team/web', 'team/alias', 'team/api'],
             'publish': True, 'component_id': 'stable_id',
             'url': 'https://www.example.com/', 'alias_hosts': ['alias.example.com']},
            {'key': 'private', 'name': 'Private Admin', 'resources': ['admin/console'],
             'publish': False, 'exclude_reason': 'private administration'},
        ]
        calls = []
        def probe(url, rule, _policy):
            calls.append((url, rule['key']))
            return {'status': 'operational', 'reason': 'mocked', 'observed_at': NOW}

        snapshot = inventory.collect_inventory([
            ingress('team', 'web', 'www.example.com', ['/', '/health']),
            ingress('team', 'alias', 'alias.example.com'),
            ingress('team', 'api', 'api.example.com', ['/v1']),
            ingress('admin', 'console', 'admin.example.com'),
        ], [], p, now=NOW, probe=probe)
        rows = {row['key']: row for row in snapshot['rows']}
        app = rows['app']
        self.assertEqual(app['component_id'], 'stable_id')
        self.assertEqual(app['resources'], ['team/web', 'team/alias', 'team/api'])
        self.assertEqual(app['url'], 'https://www.example.com/')
        self.assertEqual(app['aliases'], ['https://alias.example.com/'])
        self.assertEqual(app['secondary_endpoints'], ['https://api.example.com/v1',
                                                       'https://www.example.com/health'])
        self.assertTrue(app['publish'])
        self.assertFalse(rows['private']['publish'])
        self.assertEqual(rows['private']['observation']['reason'], 'excluded: private administration')
        self.assertEqual([key for _url, key in calls], ['app'])
        self.assertEqual(len(rows), 2, 'multiple Ingresses assigned to one app must not duplicate it')

    def test_auto_discovered_unapproved_ingress_is_internal_and_unpublished(self):
        p = policy()
        p['applications'] = [{'key': 'known', 'resources': ['team/known'], 'publish': False}]
        snapshot = inventory.collect_inventory(
            [ingress('new-team', 'new-app', 'new.apps.example.com')], [], p,
            now=NOW, probe=lambda *_: {'status': 'operational', 'reason': 'mocked',
                                       'observed_at': NOW})
        row = snapshot['rows'][0]
        self.assertEqual(row['key'], 'ingress/new-team/new-app')
        self.assertFalse(row['publish'])
        self.assertIsNone(row['component_id'])
        self.assertEqual(row['source'], 'kubernetes_ingress')

    def test_external_override_probes_without_ingress_and_keeps_stable_identity(self):
        p = policy()
        p['applications'] = [{'key': 'external', 'name': 'External', 'source': 'external',
            'url': 'https://example.com/status', 'publish': True, 'component_id': 'cmp-1',
            'expected_status': [204]}]
        seen = []
        row = inventory.collect_inventory([], [], p, now=NOW,
            probe=lambda url, rule, _p: (seen.append((url, rule['expected_status'])) or
                {'status': 'operational', 'reason': 'mocked', 'observed_at': NOW}))['rows'][0]
        self.assertEqual(row['source'], 'external_override')
        self.assertTrue(row['present'])
        self.assertEqual(row['component_id'], 'cmp-1')
        self.assertEqual(seen, [('https://example.com/status', [204])])

    def test_missing_and_failed_ingress_sources_preserve_identity_history_and_never_green(self):
        p = policy()
        p['applications'] = [{'key': 'app', 'resources': ['team/app'], 'publish': True,
                              'component_id': 'cmp'}]
        old = {'version': 1, 'generated_at': NOW - 1000, 'sources': {}, 'rows': [{
            'key': 'app', 'kind': 'application', 'name': 'App', 'component_id': 'cmp',
            'last_seen_at': NOW - 500, 'source_observed_at': NOW - 500, 'present': True,
            'url': 'https://team.example.com/',
            'observation': {'status': 'operational', 'reason': 'prior', 'observed_at': NOW - 500},
        }]}
        missing = inventory.collect_inventory([], [], p, previous=old, now=NOW)
        missing_row = missing['rows'][0]
        self.assertEqual(missing_row['component_id'], 'cmp')
        self.assertEqual(missing_row['last_seen_at'], NOW - 500)
        self.assertEqual(missing_row['url'], 'https://team.example.com/')
        self.assertFalse(missing_row['present'])
        self.assertEqual(missing_row['observation']['reason'], 'ingress_missing')
        self.assertNotEqual(inventory.effective_observation(missing_row, p, NOW)['status'], 'operational')

        failed = inventory.collect_inventory(None, None, p, previous=old, now=NOW)
        failed_row = failed['rows'][0]
        self.assertEqual(failed_row['last_seen_at'], NOW - 500)
        self.assertEqual(failed_row['url'], 'https://team.example.com/')
        self.assertEqual(failed_row['component_id'], 'cmp')
        self.assertEqual(failed_row['observation']['reason'], 'ingress_source_unavailable')
        self.assertNotEqual(inventory.effective_observation(failed_row, p, NOW)['status'], 'operational')
        self.assertFalse(failed['sources']['ingress']['ok'])

    def test_stale_future_and_malformed_observations_are_degraded(self):
        p = policy()
        cases = [
            ({'status': 'operational', 'observed_at': NOW - 181}, NOW, 'observation_stale'),
            ({'status': 'operational', 'observed_at': NOW + 31}, NOW, 'observation_stale'),
            ({'status': 'green', 'observed_at': NOW}, NOW, 'invalid_observation'),
            ({'status': 'operational', 'observed_at': 'bad'}, NOW, 'observation_stale'),
        ]
        for observation, timestamp, reason in cases:
            with self.subTest(observation=observation):
                row = {'source_observed_at': timestamp, 'observation': observation}
                result = inventory.effective_observation(row, p, NOW)
                self.assertEqual(result['status'], 'degraded_performance')
                self.assertEqual(result['reason'], reason)
        row = {'source_observed_at': NOW - 181,
               'observation': {'status': 'operational', 'observed_at': NOW}}
        self.assertEqual(inventory.effective_observation(row, p, NOW)['reason'], 'source_stale')
        future_source = {'source_observed_at': NOW + 31,
                         'observation': {'status': 'operational', 'observed_at': NOW}}
        self.assertEqual(inventory.effective_observation(future_source, p, NOW)['reason'], 'source_stale')

    def test_node_source_failure_keeps_prior_identity_and_cannot_become_green(self):
        p = policy()
        p['nodes'] = [{'key': 'node/worker-a', 'name': 'worker-a', 'publish': True,
                       'component_id': 'node-component'}]
        previous = {'rows': [{'key': 'node/worker-a', 'kind': 'node', 'name': 'worker-a',
            'component_id': 'node-component', 'last_seen_at': NOW - 500,
            'source_observed_at': NOW - 500,
            'observation': {'status': 'operational', 'reason': 'ready_no_pressure',
                            'observed_at': NOW - 500}}]}
        result = inventory.collect_inventory([], None, p, previous=previous, now=NOW)
        row = result['rows'][0]
        self.assertEqual(row['component_id'], 'node-component')
        self.assertEqual(row['last_seen_at'], NOW - 500)
        self.assertEqual(row['observation']['reason'], 'node_source_unavailable')
        self.assertEqual(inventory.effective_observation(row, p, NOW)['reason'], 'source_stale')

    def test_new_and_retired_nodes_are_discovered_without_losing_history(self):
        p = policy()
        p['nodes'] = [{'key': 'node/old', 'name': 'old', 'publish': True,
                       'component_id': 'stable-node-id',
                       'retired': True, 'retirement_reason': 'decommissioned'}]
        previous = {'rows': [{'key': 'node/old', 'kind': 'node', 'name': 'old',
                              'last_seen_at': NOW - 20, 'source_observed_at': NOW - 20,
                              'component_id': 'stable-node-id'}]}
        result = inventory.collect_inventory([], [node('new-node')], p,
            previous=previous, now=NOW, probe=lambda *_: self.fail('node collection does not probe'))
        rows = {row['key']: row for row in result['rows']}
        self.assertIn('node/new-node', rows)
        self.assertEqual(rows['node/new-node']['observation']['status'], 'operational')
        self.assertEqual(rows['node/old']['component_id'], 'stable-node-id')
        self.assertFalse(rows['node/old']['present'])
        self.assertEqual(rows['node/old']['observation']['reason'], 'retired: decommissioned')


class TestInventoryPersistence(unittest.TestCase):
    def test_sqlite_restart_retains_missing_resource_identity_and_history(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(collector, 'DB', str(Path(directory)/'inventory.db')):
            self.assertIsNone(collector.read_previous())
            snapshot = {'rows':[{'key':'node/removed','kind':'node','name':'removed',
                'component_id':'stable','source_observed_at':NOW-10,'last_seen_at':NOW-10,
                'observation':{'status':'major_outage','reason':'not_ready','observed_at':NOW-10}}]}
            collector.write_snapshot(snapshot)
            restored = collector.read_previous()
            p = policy() | {'nodes':[{'key':'node/removed','name':'removed','publish':True,'component_id':'stable'}]}
            missing = inventory.collect_inventory([], [], p, previous=restored, now=NOW)
            collector.write_snapshot(missing)
            restarted = collector.read_previous()['rows'][0]
            self.assertEqual(restarted['component_id'],'stable')
            self.assertEqual(restarted['last_seen_at'],NOW-10)
            self.assertEqual(restarted['observation']['reason'],'node_missing')


class TestProbeDeadline(unittest.TestCase):
    def test_private_ingress_exception_is_exact_and_forbids_loopback(self):
        p = policy() | {'allowed_private_addresses': {'example.com': ['100.125.138.71']}}
        inventory.validate_policy(p)
        p['allowed_private_addresses']['example.com'] = ['127.0.0.1']
        with self.assertRaises(ValueError): inventory.validate_policy(p)
        with patch('inventory.socket.getaddrinfo', return_value=[(None, None, None, None, ('100.125.138.71',443))]), patch('inventory.socket.create_connection') as connect:
            conn = inventory.PinnedHTTPS('example.com')
            conn._context = MagicMock()
            conn.allowed_addresses = ['100.125.138.71']
            conn.connect()
            connect.assert_called_once()
            other = inventory.PinnedHTTPS('example.com')
            with self.assertRaises(ValueError): other.connect()

    def test_stuck_worker_is_killed_with_a_finite_wall_clock_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            worker = Path(directory) / 'stuck.py'
            worker.write_text('import time; time.sleep(20)')
            p = policy() | {'timeout_seconds': 1}
            start = time.monotonic()
            with patch('inventory.Path') as path:
                path.return_value.resolve.return_value = worker
                observed = inventory.bounded_probe_url('https://example.com/', {}, p)
            self.assertLess(time.monotonic() - start, 5)
            self.assertEqual(observed['status'], 'major_outage')
            self.assertEqual(observed['reason'], 'probe_deadline_exceeded')


class TestNodeObservations(unittest.TestCase):
    def setUp(self):
        self.policy = policy()

    def test_ready_false_unknown_and_stale_heartbeat(self):
        cases = [
            (node(ready='False'), 'major_outage', 'not_ready'),
            (node(ready='Unknown'), 'degraded_performance', 'ready_unknown'),
            (node(heartbeat='2027-01-15T07:49:59Z'), 'degraded_performance', 'node_heartbeat_stale'),
            (node(heartbeat='malformed'), 'degraded_performance', 'node_heartbeat_stale'),
        ]
        for value, state, reason in cases:
            with self.subTest(state=state, reason=reason):
                observed = inventory.node_observation(value, self.policy, NOW)
                self.assertEqual((observed['status'], observed['reason']), (state, reason))

    def test_pressure_conditions_and_cordon_mapping(self):
        for pressure in ('MemoryPressure', 'DiskPressure', 'PIDPressure'):
            with self.subTest(pressure=pressure):
                observed = inventory.node_observation(node(extra=[(pressure, 'True')]), self.policy, NOW)
                self.assertEqual(observed['status'], 'degraded_performance')
                self.assertEqual(observed['reason'], pressure)
        unknown = inventory.node_observation(node(extra=[('MemoryPressure', 'Unknown')]), self.policy, NOW)
        self.assertEqual(unknown['reason'], 'pressure_unknown_MemoryPressure')
        cordoned = inventory.node_observation(node(unschedulable=True), self.policy, NOW)
        self.assertTrue(cordoned['cordoned'])
        self.assertEqual(cordoned['status'], 'operational')

    def test_retired_and_missing_node_reasons_are_distinct(self):
        p = self.policy | {'nodes': [{'key': 'node/gone', 'name': 'gone', 'publish': False,
                                     'retired': True, 'retirement_reason': 'planned'}]}
        result = inventory.collect_inventory([], [], p, now=NOW)
        row = next(row for row in result['rows'] if row['key'] == 'node/gone')
        self.assertEqual(row['observation']['reason'], 'retired: planned')
        p['nodes'][0]['retired'] = False
        result = inventory.collect_inventory([], [], p, now=NOW)
        self.assertEqual(result['rows'][0]['observation']['reason'], 'node_missing')


class TestURLSafetyAndProbe(unittest.TestCase):
    def setUp(self):
        self.policy = policy()
        self.rule = {'expected_status': [200], 'redirect': 'deny'}

    def test_url_validation_rejects_unscoped_and_unsafe_targets(self):
        for url in ('http://example.com/', 'https://user@example.com/',
                    'https://example.com:8443/', 'https://127.0.0.1/',
                    'https://example.com/?token=x', 'https://example.com/#frag',
                    'https://example.com/a/../b', 'https://evil.test/'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                inventory.validate_url(url, self.policy)
        self.assertEqual(inventory.validate_url('https://svc.apps.example.com/health',
                                                 self.policy).hostname, 'svc.apps.example.com')

    def test_pinned_https_denies_private_dns_and_connects_only_to_public_ip(self):
        private = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('10.1.2.3', 443))]
        conn = inventory.PinnedHTTPS('example.com', timeout=1)
        with patch('inventory.socket.getaddrinfo', return_value=private), \
             patch('inventory.socket.create_connection') as create:
            with self.assertRaisesRegex(ValueError, 'non_public_address_denied'):
                conn.connect()
        create.assert_not_called()

        public = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443))]
        raw = MagicMock()
        tls = MagicMock()
        with patch('inventory.socket.getaddrinfo', return_value=public), \
             patch('inventory.socket.create_connection', return_value=raw) as create, \
             patch.object(conn._context, 'wrap_socket', return_value=tls) as wrap:
            conn.connect()
        create.assert_called_once_with(('93.184.216.34', 443), 1)
        wrap.assert_called_once_with(raw, server_hostname='example.com')
        self.assertIs(conn.sock, tls)

    def test_probe_passes_finite_timeout_tls_context_and_expected_status(self):
        response = MagicMock(status=204)
        response.getheader.return_value = None
        response.read.return_value = b''
        conn = MagicMock()
        conn.getresponse.return_value = response
        with patch('inventory.PinnedHTTPS', return_value=conn) as transport:
            result = inventory.probe_url('https://example.com/health',
                {'expected_status': [204], 'redirect': 'deny'}, self.policy)
        self.assertEqual(result['status'], 'operational')
        self.assertEqual(result['reason'], 'http_expected_status')
        kwargs = transport.call_args.kwargs
        self.assertGreater(kwargs['timeout'], 0)
        self.assertLessEqual(kwargs['timeout'], self.policy['timeout_seconds'])
        self.assertIsNotNone(kwargs['context'])
        conn.request.assert_called_once_with('GET', '/health', headers=unittest.mock.ANY)
        conn.close.assert_called_once()

    def test_probe_timeout_and_unexpected_status_fail_closed(self):
        conn = MagicMock()
        conn.request.side_effect = TimeoutError()
        with patch('inventory.PinnedHTTPS', return_value=conn):
            timed_out = inventory.probe_url('https://example.com/', self.rule, self.policy)
        self.assertEqual(timed_out['status'], 'major_outage')
        self.assertEqual(timed_out['reason'], 'TimeoutError')

        response = MagicMock(status=503)
        response.getheader.return_value = None
        response.read.return_value = b''
        conn = MagicMock()
        conn.getresponse.return_value = response
        with patch('inventory.PinnedHTTPS', return_value=conn):
            failed = inventory.probe_url('https://example.com/', self.rule, self.policy)
        self.assertEqual((failed['status'], failed['reason']), ('major_outage', 'http_unexpected_status'))

    def test_redirect_deny_same_host_allowlist_and_private_host(self):
        def connection(status, location=None):
            response = MagicMock(status=status)
            response.getheader.return_value = location
            response.read.return_value = b''
            conn = MagicMock()
            conn.getresponse.return_value = response
            return conn

        first = connection(302, 'https://elsewhere.example.net/login')
        with patch('inventory.PinnedHTTPS', return_value=first):
            denied = inventory.probe_url('https://example.com/', self.rule, self.policy)
        self.assertEqual(denied['status'], 'major_outage')

        chain = [connection(302, '/login'), connection(200)]
        with patch('inventory.PinnedHTTPS', side_effect=chain):
            same_host = inventory.probe_url('https://example.com/',
                {'expected_status': [200], 'redirect': 'same_host'}, self.policy)
        self.assertEqual(same_host['status'], 'operational')
        self.assertEqual(same_host['redirect_hops'], 1)

        self.policy['allowed_hosts'].append('sso.example.net')
        chain = [connection(302, 'https://sso.example.net/login?state=abc'), connection(200)]
        with patch('inventory.PinnedHTTPS', side_effect=chain) as transport:
            allowed = inventory.probe_url('https://example.com/',
                {'expected_status': [200], 'redirect': 'allowlisted',
                 'redirect_hosts': ['sso.example.net']}, self.policy)
        self.assertEqual(allowed['status'], 'operational')
        self.assertEqual(transport.call_args_list[1].args[0], 'sso.example.net')
        self.assertIn('/login?state=abc', chain[1].request.call_args.args[1])
        loopback_redirect = connection(302, 'https://127.0.0.1/login')
        with patch('inventory.PinnedHTTPS', return_value=loopback_redirect):
            private = inventory.probe_url('https://example.com/',
                {'expected_status': [200], 'redirect': 'allowlisted',
                 'redirect_hosts': ['127.0.0.1']}, self.policy)
        self.assertEqual(private['status'], 'major_outage')


class TestCollectorResourceBoundary(unittest.TestCase):
    def test_collector_only_accepts_ingress_and_node_api_paths(self):
        with self.assertRaisesRegex(ValueError, 'Resource forbidden'):
            collector.list_resources('/api/v1/secrets')
        response = MagicMock()
        response.read.return_value = b'{"items":[{"metadata":{"name":"n1"}}],"metadata":{}}'
        response.__enter__.return_value = response
        opener = MagicMock()
        opener.open.return_value = response
        with patch.object(collector, 'TOKEN_DIR', Path('/unused')), \
             patch.object(Path, 'read_text', return_value='test-token'), \
             patch.object(collector.urllib.request, 'build_opener', return_value=opener), \
             patch.object(collector.ssl, 'create_default_context', return_value=MagicMock()):
            items = collector.list_resources('/api/v1/nodes')
        self.assertEqual(items, [{'metadata': {'name': 'n1'}}])
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, 'https://kubernetes.default.svc/api/v1/nodes?limit=200&continue=')
        self.assertEqual(request.get_header('Authorization'), 'Bearer test-token')


if __name__ == '__main__':
    unittest.main()
