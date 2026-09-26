import json
import io
import os
import sys
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'statuspage-sync')))
import sync


def response(body):
    resp = MagicMock()
    resp.status = 200
    resp.read.return_value = json.dumps(body).encode()
    resp.__enter__.return_value = resp
    return resp


def component():
    return {'key': 'c1', 'id': 'c1', 'mode': 'internal', 'checks': [
        {'type': 'prometheus', 'url': 'https://prom.example', 'query': 'up',
         'comparison': 'eq', 'threshold': 1}]}


def statuspage(status='operational', incidents=None, maintenance=None):
    sp = MagicMock()
    sp.get_components.return_value = {'c1': {'id': 'c1', 'status': status, 'page_id': 'p1'}}
    sp.get_active_incidents_and_maintenance.return_value = (incidents or [], maintenance or [])
    return sp


class TestStatuspageSync(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {'STATUSPAGE_API_KEY': 'fake-key',
                                   'STATUSPAGE_API_KEY_FILE': ''})
        env.start()
        self.addCleanup(env.stop)

    def test_internal_debounce_starts_unknown(self):
        manager = sync.ComponentManager({})
        self.assertIsNone(manager.process_raw_status('major_outage', 3, 2, True))
        self.assertIsNone(manager.process_raw_status('major_outage', 3, 2, True))
        self.assertEqual(manager.process_raw_status('major_outage', 3, 2, True), 'major_outage')
        self.assertEqual(manager.process_raw_status('operational', 3, 2, True), 'major_outage')
        self.assertEqual(manager.process_raw_status('operational', 3, 2, True), 'operational')

    @patch('sync.check_prometheus', return_value=False)
    def test_restart_never_publishes_false_operational(self, _check):
        comp = component()
        sp = statuspage(status='major_outage')
        managers = {'c1': sync.ComponentManager(comp)}
        config = {'page_id': 'p1', 'components': [comp], 'failure_threshold': 3}
        for _ in range(3):
            self.assertTrue(sync.run_sync_cycle(config, sp, managers, 'internal', False))
        sp.update_component.assert_not_called()

    @patch('sync.opener.open')
    def test_http_json_path_must_exist(self, mock_open):
        mock_open.return_value = response({'status': 'ok'})
        check = {'type': 'http', 'url': 'https://service.example/health',
                 'body_json_key': 'data.ready'}
        self.assertEqual(sync.check_http(check), (False, None))
        check['body_json_key'] = 'status'
        self.assertTrue(sync.check_http(check)[0])

    @patch('sync.urllib.request.urlopen')
    def test_prometheus_fresh_vector_stale_future_and_no_data(self, mock_open):
        check = component()['checks'][0]
        now = time.time()
        def payload(timestamp, result_type='vector', values=None):
            return {'status': 'success', 'data': {'resultType': result_type,
                'result': [{'value': [timestamp, '1']}] if values is None else values}}
        cases = [(payload(now), True), (payload(now - 300), False),
                 (payload(now + 300), False), (payload(now, values=[]), False),
                 (payload(now, result_type='matrix'), False)]
        for data, expected in cases:
            with self.subTest(data=data):
                mock_open.return_value = response(data)
                self.assertEqual(sync.check_prometheus(check), expected)

    @patch('sync.urllib.request.urlopen')
    def test_prometheus_scalar_nan_and_invalid_threshold(self, mock_open):
        check = component()['checks'][0]
        now = time.time()
        mock_open.return_value = response({'status': 'success', 'data': {
            'resultType': 'scalar', 'result': [now, '1']}})
        self.assertTrue(sync.check_prometheus(check))
        mock_open.return_value = response({'status': 'success', 'data': {
            'resultType': 'vector', 'result': [{'value': [now, 'NaN']}]}})
        self.assertFalse(sync.check_prometheus(check))
        check.pop('threshold')
        self.assertFalse(sync.check_prometheus(check))

    @patch('sync.time.sleep')
    @patch('sync.management_opener.open')
    def test_management_retries_429(self, mock_open, mock_sleep):
        error = urllib.error.HTTPError('https://api.statuspage.io', 429,
                                        'Too Many Requests', {}, None)
        mock_open.side_effect = [error, response({})]
        self.assertEqual(sync.StatuspageClient('p1', False)._request('GET', '/components'), {})
        self.assertEqual(mock_open.call_count, 2)
        mock_sleep.assert_called()

    def test_management_redirects_forbidden(self):
        req = urllib.request.Request('https://api.statuspage.io/v1/pages/p1/components',
                                     headers={'Authorization': 'OAuth fake-key'})
        for destination in ['https://elsewhere.example/steal', 'http://elsewhere.example/steal']:
            with self.subTest(destination=destination):
                with self.assertRaises(urllib.error.URLError):
                    sync.NoManagementRedirect().redirect_request(
                        req, None, 302, 'Found', {}, destination)

    @patch('sync.management_opener.open')
    def test_dry_run_mutations_make_no_management_network_calls(self, mock_open):
        client = sync.StatuspageClient('p1', True)
        client.update_component('c1', 'major_outage')
        client.post_metric('m1', 10.0, 123)
        mock_open.assert_not_called()

    @patch('sync.management_opener.open')
    def test_components_must_belong_to_target_page(self, mock_open):
        mock_open.return_value = response([
            {'id': 'c1', 'page_id': 'p1'}, {'id': 'c2', 'page_id': 'wrong_page'}])
        with self.assertRaisesRegex(Exception, 'Page ID mismatch'):
            sync.StatuspageClient('p1', False).get_components()

    @patch('sync.check_prometheus', return_value=False)
    def test_manual_incident_resets_debounce(self, _check):
        comp = component()
        config = {'page_id': 'p1', 'components': [comp], 'failure_threshold': 3}
        sp = statuspage()
        manager = sync.ComponentManager(comp)
        managers = {'c1': manager}
        for _ in range(2):
            self.assertTrue(sync.run_sync_cycle(config, sp, managers, 'internal', False))
        sp.get_active_incidents_and_maintenance.return_value = (['c1'], [])
        self.assertTrue(sync.run_sync_cycle(config, sp, managers, 'internal', False))
        self.assertEqual(manager.consec_counts['major_outage'], 0)
        sp.get_active_incidents_and_maintenance.return_value = ([], [])
        for _ in range(2):
            self.assertTrue(sync.run_sync_cycle(config, sp, managers, 'internal', False))
        sp.update_component.assert_not_called()
        self.assertTrue(sync.run_sync_cycle(config, sp, managers, 'internal', False))
        sp.update_component.assert_called_once_with('c1', 'major_outage')

    @patch('sync.check_prometheus', return_value=False)
    def test_incident_and_maintenance_precede_automatic_status(self, _check):
        comp = component()
        config = {'page_id': 'p1', 'components': [comp]}
        for sp in [statuspage(incidents=['c1']), statuspage(maintenance=['c1']),
                   statuspage(incidents=['*']), statuspage(maintenance=['*']),
                   statuspage(status='under_maintenance')]:
            with self.subTest(sp=sp):
                self.assertTrue(sync.run_sync_cycle(config, sp, {'c1': sync.ComponentManager(comp)},
                                                    'internal', True))
                sp.update_component.assert_not_called()

    @patch('sync.check_http')
    def test_partial_and_total_http_failure(self, mock_check):
        comp = component()
        comp['checks'] = [{'type': 'http', 'url': 'https://one.example'},
                          {'type': 'http', 'url': 'https://two.example'}]
        for values, expected in [([(True, 10), (False, None)], 'partial_outage'),
                                 ([(False, None), (False, None)], 'major_outage')]:
            mock_check.side_effect = values
            sp = statuspage()
            self.assertTrue(sync.run_sync_cycle({'components': [comp]}, sp,
                {'c1': sync.ComponentManager(comp)}, 'internal', True))
            sp.update_component.assert_called_once_with('c1', expected)

    def test_healthz_requires_recent_success_and_is_not_cached(self):
        handler = object.__new__(sync.HealthServerHandler)
        handler.path = '/healthz'
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()
        for last, expected in [(0, 503), (time.time() - 500, 503), (time.time(), 200)]:
            handler.wfile = io.BytesIO()
            with patch.dict(sync.app_state, last_success_at=last, stale_seconds=180):
                handler.do_GET()
            handler.send_response.assert_called_with(expected)
            handler.send_header.assert_any_call('Cache-Control', 'no-store')
            self.assertEqual(json.loads(handler.wfile.getvalue())['ok'], expected == 200)

    @patch('sync.check_prometheus', return_value=False)
    def test_missing_component_fails_cycle_before_write(self, _check):
        comp = component()
        sp = statuspage()
        sp.get_components.return_value = {}
        self.assertFalse(sync.run_sync_cycle({'page_id': 'p1', 'components': [comp]},
                                             sp, {'c1': sync.ComponentManager(comp)}, 'internal', True))
        sp.update_component.assert_not_called()

    @patch('sync.time.sleep')
    @patch('sync.check_http')
    def test_external_once_requires_two_of_three_successes(self, mock_check, _sleep):
        cases = [([(True, 40.0), (False, None), (True, 60.0)], (True, 50.0)),
                 ([(False, None), (True, 40.0), (False, None)], (False, None))]
        for checks, expected in cases:
            with self.subTest(checks=checks):
                mock_check.side_effect = checks
                self.assertEqual(sync.run_http_check_with_retry({'type': 'http'}, True), expected)
                self.assertEqual(mock_check.call_count, 3)
                mock_check.reset_mock()

    def test_metric_without_checks_is_not_posted(self):
        sp = MagicMock()
        sp.get_active_incidents_and_maintenance.return_value = ([], [])
        sp.get_components.return_value = {}
        config = {'page_id': 'p1', 'components': [], 'metrics': [
            {'id': 'm1', 'mode': 'internal', 'kind': 'checks_passing_percent'}]}
        self.assertTrue(sync.run_sync_cycle(config, sp, {}, 'internal', True))
        sp.post_metric.assert_not_called()

    def test_config_validation(self):
        base = {'page_id': 'p1', 'components': [{'key': 'c1', 'id': 'c1',
            'mode': 'internal', 'checks': [{'type': 'http',
            'url': 'http://backend.default.svc.cluster.local/health'}]}],
            'failure_threshold': 3, 'recovery_threshold': 2}
        sync.validate_config(base)
        invalid = [
            dict(base, failure_threshold=0),
            dict(base, recovery_threshold=0),
            dict(base, components=base['components'] * 2),
            dict(base, components=[dict(base['components'][0], checks=[
                {'type': 'htp', 'url': 'https://service.example'}])]),
            dict(base, components=[dict(base['components'][0], mode='external', checks=[
                {'type': 'http', 'url': 'http://service.example/health'}])]),
            dict(base, components=[dict(base['components'][0], checks=[
                {'type': 'http', 'url': 'http://service.example/health'}])]),
        ]
        for case in invalid:
            with self.subTest(case=case), self.assertRaises((ValueError, SystemExit)):
                sync.validate_config(case)


if __name__ == '__main__':
    unittest.main()
