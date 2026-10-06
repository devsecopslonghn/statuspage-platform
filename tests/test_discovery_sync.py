"""Exercise inventory observations through the actual sync writer and admin boundary."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'statuspage-sync'))
sys.path.insert(0, str(ROOT / 'admin'))
import sync
import server
import provision_discovery


class DiscoveryWriterTests(unittest.TestCase):
    def setUp(self):
        self.now = time.time()
        self.policy = {'version': 1, 'interval_seconds': 60, 'stale_seconds': 180,
            'node_heartbeat_seconds': 600, 'timeout_seconds': 10,
            'allowed_hosts': ['app.example.com'], 'applications': [
                {'key': 'app', 'name': 'App', 'publish': True, 'component_id': 'a',
                 'resources': ['team/app']}], 'nodes': [
                {'key': 'node/worker', 'name': 'worker', 'publish': True, 'component_id': 'n'}]}
        self.config = {'page_id': 'hsyg2r4ql1pr', 'discovery': {'enabled': True},
            'failure_threshold': 3, 'recovery_threshold': 2, 'components': [
                {'key': 'app', 'id': 'a', 'inventory_key': 'app', 'mode': 'internal',
                 'checks': [{'type': 'inventory', 'key': 'app'}]},
                {'key': 'external', 'id': 'e', 'mode': 'external', 'checks': [
                    {'type': 'http', 'url': 'https://app.example.com/'}]}]}
        self.snapshot = {'version': 1, 'generated_at': self.now, 'sources': {
            k: {'ok': True, 'observed_at': self.now} for k in ('node', 'ingress')},
            'rows': [self.row('app'), self.row('node/worker')]}
        self.sp = MagicMock()
        self.sp.get_active_incidents_and_maintenance.return_value = ([], [])
        self.sp.get_components.return_value = {
            i: {'id': i, 'page_id': 'hsyg2r4ql1pr', 'status': 'operational'} for i in ('a', 'n', 'e')}
        self.managers = {'app': sync.ComponentManager(self.config['components'][0])}
        self.policy_patch = patch('sync.load_policy', return_value=self.policy)
        self.inventory_patch = patch('sync.fetch_inventory', side_effect=lambda *_: self.snapshot)
        self.policy_patch.start(); self.inventory_patch.start()
        self.addCleanup(self.policy_patch.stop); self.addCleanup(self.inventory_patch.stop)

    def row(self, key, status='operational', reason='observed', age=0):
        return {'key': key, 'source_observed_at': self.now - age, 'present': True,
                'observation': {'status': status, 'reason': reason, 'observed_at': self.now - age}}

    def cycle(self, advance=True):
        if advance:
            for row in self.snapshot['rows']:
                if row['observation'].get('observed_at', 0) >= self.now - 180:
                    row['observation']['observed_at'] += 1
        return sync.run_sync_cycle(self.config, self.sp, self.managers, 'internal', False)

    def test_individual_node_notready_failure_debounce_and_writer_ownership(self):
        self.snapshot['rows'][1] = self.row('node/worker', 'major_outage', 'not_ready')
        for _ in range(2):
            self.assertTrue(self.cycle())
        self.sp.update_component.assert_not_called()
        self.assertTrue(self.cycle())
        self.sp.update_component.assert_called_once_with('n', 'major_outage')

    def test_repeated_failure_snapshot_is_one_failure_sample(self):
        self.snapshot['rows'][1] = self.row('node/worker', 'major_outage', 'not_ready')
        for _ in range(4): self.assertTrue(self.cycle(advance=False))
        self.sp.update_component.assert_not_called()
        self.assertEqual(self.managers['node/worker'].consec_counts['major_outage'], 1)
        self.cycle(); self.cycle()
        self.sp.update_component.assert_called_once_with('n', 'major_outage')

    def test_stale_and_source_loss_are_degraded_even_with_old_healthy_payload(self):
        for stale in (True, False):
            self.sp.update_component.reset_mock()
            self.managers.clear()
            self.snapshot['rows'][1] = self.row('node/worker', age=181 if stale else 0)
            self.snapshot['sources']['node']['ok'] = stale
            for _ in range(3):
                result = self.cycle()
            self.assertFalse(result)  # Stale observations or source loss also fail sync heartbeat.
            self.sp.update_component.assert_called_once_with('n', 'degraded_performance')

    def test_missing_ingress_with_backend_success_still_degraded(self):
        self.config['components'][0]['checks'].append({'type': 'http', 'url': 'https://app.example.com/ready'})
        self.snapshot['rows'][0] = self.row('app', 'degraded_performance', 'ingress_missing')
        with patch('sync.check_http', return_value=(True, 20)):
            for _ in range(3):
                self.assertTrue(self.cycle())
        self.sp.update_component.assert_called_once_with('a', 'degraded_performance')

    def test_repeated_healthy_snapshot_is_one_recovery_sample(self):
        for value in self.sp.get_components.return_value.values():
            value['status'] = 'major_outage'
        self.assertTrue(self.cycle(advance=False)); self.assertTrue(self.cycle(advance=False))
        self.sp.update_component.assert_not_called()
        for row in self.snapshot['rows']:
            row['observation']['observed_at'] += 1
        self.assertTrue(self.cycle())
        self.assertEqual({call.args for call in self.sp.update_component.call_args_list},
                         {('a', 'operational'), ('n', 'operational')})

    def test_incident_resets_discovery_debounce_and_pagewide_maintenance_blocks(self):
        self.snapshot['rows'][1] = self.row('node/worker', 'major_outage', 'not_ready')
        self.cycle(); self.cycle()
        self.sp.get_active_incidents_and_maintenance.return_value = (['n'], [])
        self.cycle()
        self.assertEqual(self.managers['node/worker'].consec_counts['major_outage'], 0)
        self.sp.get_active_incidents_and_maintenance.return_value = ([], ['*'])
        self.cycle()
        self.sp.update_component.assert_not_called()
        self.sp.get_active_incidents_and_maintenance.return_value = ([], [])
        self.cycle(); self.cycle()
        self.sp.update_component.assert_not_called()
        self.cycle()
        self.sp.update_component.assert_called_once_with('n', 'major_outage')

    def test_missing_component_never_creates_or_deletes_and_retired_freezes(self):
        del self.sp.get_components.return_value['n']
        self.assertFalse(self.cycle())
        self.sp.update_component.assert_not_called()
        self.policy['nodes'][0]['retired'] = True
        self.policy['nodes'][0]['retirement_reason'] = 'decommissioned'
        self.sp.get_components.return_value['n'] = {'status': 'major_outage'}
        for _ in range(3): self.assertTrue(self.cycle())
        self.sp.update_component.assert_not_called()
        self.sp._request.assert_not_called()

    def test_observation_expired_during_management_fetch_cannot_publish_green(self):
        configured = copy.deepcopy(self.config)
        configured['discovery']['enabled'] = False
        app = configured['components'][0]
        app['_inventory_expiry'] = time.time()-1
        app['_inventory_observation'] = {'status':'operational','reason':'http_expected_status','observed_at':time.time()}
        for _ in range(3):
            self.assertFalse(sync.run_sync_cycle(configured,self.sp,self.managers,'internal',False))
        self.sp.update_component.assert_called_once_with('a','degraded_performance')

    def test_sync_reports_backend_failure_reason_and_manual_precedence_to_admin(self):
        self.config['components'][0]['checks'].append({'type':'http','url':'https://app.example.com/ready'})
        with patch('sync.check_http', return_value=(False,None)):
            for _ in range(3): self.cycle()
        observed = sync.app_state['observations']['app']
        self.assertEqual(observed['raw_status'],'partial_outage')
        self.assertEqual(observed['reason'],'additional_readiness_or_content_check_failed')
        self.assertEqual(observed['statuspage_status'],'partial_outage')
        self.sp.get_active_incidents_and_maintenance.return_value = (['a'], [])
        with patch('sync.check_http', return_value=(False,None)): self.cycle()
        self.assertTrue(sync.app_state['observations']['app']['manual_control'])
        self.assertEqual(sync.app_state['observations']['app']['reason'],'active_incident_or_maintenance')

    def test_discovery_cannot_claim_external_or_duplicate_component_mapping(self):
        self.policy['nodes'][0]['component_id'] = 'e'
        self.assertFalse(self.cycle())
        self.sp.update_component.assert_not_called()

    def test_collector_failure_updates_unknown_with_debounce_and_fails_heartbeat(self):
        with patch('sync.fetch_inventory', side_effect=RuntimeError('upstream failed')):
            for _ in range(3): self.assertFalse(self.cycle())
        self.assertEqual({call.args for call in self.sp.update_component.call_args_list},
                         {('a', 'degraded_performance'), ('n', 'degraded_performance')})

    def test_admin_inventory_uses_trusted_mapping_sanitizes_and_reports_stale(self):
        payload = copy.deepcopy(self.snapshot)
        payload['rows'][0].update(component_id='attacker', publish=True, automation_email='SECRET')
        payload['rows'][1]['observation']['observed_at'] -= 181
        with patch.object(server, 'DISCOVERY_CONFIG_PATH', '/policy'), patch('server.load_policy', return_value=self.policy), patch('server.fetch_inventory', return_value=payload), patch('server.fetch_sync_observations', return_value={'app': {'component_id':'a','reason':'additional_readiness_or_content_check_failed','final_status':'partial_outage','observed_at':time.time()}}):
            output = server.fetch_discovery_inventory()
        encoded = json.dumps(output)
        self.assertNotIn('SECRET', encoded)
        self.assertNotIn('attacker', encoded)
        app = next(row for row in output['rows'] if row['key'] == 'app')
        node = next(row for row in output['rows'] if row['key'] == 'node/worker')
        self.assertEqual(app['component_id'], 'a')
        self.assertEqual(app['sync']['final_status'], 'partial_outage')
        self.assertFalse(app['sync']['stale'])
        self.assertEqual(node['observation']['reason'], 'observation_stale')


class ComponentPaginationTests(unittest.TestCase):
    def test_management_list_covers_the_documented_1100_component_ceiling(self):
        client = sync.StatuspageClient('page', True)
        records = [{'id':str(i),'page_id':'page'} for i in range(1100)]
        batches = [records[offset:offset+100] for offset in range(0,1100,100)] + [[]]
        with patch.object(client, '_request', side_effect=batches):
            self.assertEqual(len(client.get_components()),1100)


class ProvisioningTests(unittest.TestCase):
    def setUp(self):
        self.policy = {'version': 1, 'interval_seconds': 60, 'stale_seconds': 180,
            'node_heartbeat_seconds': 600, 'timeout_seconds': 10,
            'allowed_hosts': ['app.example.com'], 'applications': [
                {'key': 'jenkins', 'name': 'Jenkins', 'resources': ['jenkins/jenkins'],
                 'publish': True, 'component_id': None, 'url': 'https://app.example.com/'}], 'nodes': []}
        self.config = {'page_id': 'hsyg2r4ql1pr', 'components': []}
        now = time.time()
        self.snapshot = {'rows': [{'key': 'jenkins', 'present': True, 'source_observed_at': now,
            'url': 'https://app.example.com/', 'observation': {'status': 'major_outage',
            'reason': 'http_unexpected_status', 'http_status': 403, 'observed_at': now}}]}

    def test_unknown_capacity_and_full_page_block_creation(self):
        plan = provision_discovery.provisioning_plan(self.policy, self.config, {}, self.snapshot)
        self.assertFalse(plan['capacity_verified'])
        client = MagicMock()
        with self.assertRaises(ValueError):
            provision_discovery.apply_plan(plan, self.policy, client, '/unused')
        client._request.assert_not_called()
        self.policy.update(component_limit=1, component_limit_evidence='Operator billing verification')
        plan = provision_discovery.provisioning_plan(self.policy, self.config, {'x': {'id':'x'}}, self.snapshot)
        self.assertFalse(plan['capacity_verified'])

    def test_adoption_preserves_id_and_metadata_and_creations_start_unknown(self):
        components = {'j': {'id': 'j', 'description': 'Custom metadata [inventory-key:jenkins]', 'name': 'Renamed Jenkins'}}
        plan = provision_discovery.provisioning_plan(self.policy, self.config, components, self.snapshot)
        self.assertEqual(plan['actions'], [{'key':'jenkins', 'action':'adopt', 'id':'j'}])
        client = MagicMock()
        with tempfile.TemporaryDirectory() as directory:
            result = provision_discovery.apply_plan(plan, self.policy, client, Path(directory)/'policy.json')
            self.assertEqual(result['applications'][0]['component_id'], 'j')
            client._request.assert_not_called()
        self.policy['applications'][0]['component_id'] = None
        self.policy.update(component_limit=25, component_limit_evidence='Operator verified plan')
        plan = provision_discovery.provisioning_plan(self.policy, self.config, {}, self.snapshot)
        self.assertEqual(plan['actions'][0]['component']['status'], 'degraded_performance')
        client._request.return_value = {'id':'new', 'page_id':'hsyg2r4ql1pr'}
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)/'policy.json'
            provision_discovery.apply_plan(plan, self.policy, client, output)
            self.assertEqual(json.loads(output.read_text())['applications'][0]['component_id'], 'new')
            self.assertFalse(client._request.call_args.kwargs['retry'])

    def test_unregistered_name_and_stale_source_refuse_duplicate_or_false_inventory(self):
        with self.assertRaises(ValueError):
            provision_discovery.provisioning_plan(self.policy, self.config, {'j': {'id':'j','name':'Jenkins'}}, self.snapshot)
        self.snapshot['rows'][0]['source_observed_at'] -= 200
        with self.assertRaises(ValueError):
            provision_discovery.provisioning_plan(self.policy, self.config, {}, self.snapshot)


if __name__ == '__main__':
    unittest.main()
