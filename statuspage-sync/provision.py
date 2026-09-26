#!/usr/bin/env python3
"""Review or apply the explicit Statuspage configuration. Never creates incidents."""
import argparse
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise urllib.error.URLError('Management redirects forbidden')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default='statuspage-sync/config.json')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--backup', default='/tmp/drg-statuspage-before.json')
    args = parser.parse_args()
    config_path = Path(args.config)
    config = json.loads(config_path.read_text())
    if config['page_id'] != 'hsyg2r4ql1pr':
        raise SystemExit('Unexpected target page')
    key_path = os.environ['STATUSPAGE_API_KEY_FILE']
    opener = urllib.request.build_opener(NoRedirect())

    def api(method, path, body=None):
        time.sleep(1.2)
        key = Path(key_path).read_text().strip()
        req = urllib.request.Request(
            'https://api.statuspage.io/v1/pages/hsyg2r4ql1pr' + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={'Authorization': 'OAuth ' + key, 'Content-Type': 'application/json'},
            method=method)
        try:
            with opener.open(req, timeout=20) as response:
                data = response.read()
                return json.loads(data) if data else {}
        except urllib.error.HTTPError as error:
            # Validation error bodies may help the operator, but never log headers or tokens.
            raise SystemExit(f'Statuspage {method} {path}: HTTP {error.code}') from None

    page = api('GET', '')
    if page['id'] != config['page_id'] or page['subdomain'] != 'drgdevlab':
        raise SystemExit('Page identity mismatch')
    components = api('GET', '/components?per_page=100')
    groups = api('GET', '/component-groups?per_page=100')
    metrics = api('GET', '/metrics?per_page=100')
    templates = api('GET', '/incident_templates')
    incidents = api('GET', '/incidents?per_page=100')
    print(json.dumps({'page': page['id'], 'apply': args.apply,
                      'components': [c['name'] for c in config['components']],
                      'metrics': [m['name'] for m in config['metrics']]}))
    if not args.apply:
        return

    backup = Path(args.backup)
    if not backup.exists():
        fd = os.open(str(backup), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as handle:
            json.dump({'page': page, 'components': components, 'groups': groups,
                       'metrics': metrics, 'templates': templates, 'incidents': incidents}, handle, indent=2)

    for component in config['components']:
        existing = next((c for c in components if c['id'] == component['id']), None)
        if not existing:
            existing = next((c for c in components if c['name'] == component['name'] and not c.get('group')), None)
        payload = {'component': {'name': component['name'], 'description': component['description'], 'showcase': True}}
        if existing:
            api('PATCH', '/components/' + existing['id'], payload)
        else:
            existing = api('POST', '/components', payload)
            components.append(existing)
        component['id'] = existing['id']
        config_path.write_text(json.dumps(config, indent=2) + '\n')

    for name in dict.fromkeys(c['group'] for c in config['components']):
        ids = [c['id'] for c in config['components'] if c['group'] == name]
        group = next((g for g in groups if g['name'] == name), None)
        if name == 'Applications' and not group:
            group = next((g for g in groups if g['id'] == 'p08q7zl7ksvr'), None)
        body = {'component_group': {'name': name, 'components': ids}}
        if group:
            api('PATCH', '/component-groups/' + group['id'], body)
        else:
            group = api('POST', '/component-groups', body)
            groups.append(group)

    providers = api('GET', '/metrics_providers')
    provider = next((p for p in providers if p['type'] == 'Self'), None)
    if not provider:
        provider = api('POST', '/metrics_providers', {'metrics_provider': {'type': 'Self'}})
    for metric in config['metrics']:
        existing = next((m for m in metrics if m['id'] == metric['id'] or m['name'] == metric['name']), None)
        body = {'metric': {k: metric[k] for k in ('name', 'suffix', 'tooltip_description')}}
        body['metric'].update(display=True, decimal_places=1, y_axis_min=0)
        if metric['kind'] == 'checks_passing_percent':
            body['metric']['y_axis_max'] = 100
        if existing:
            api('PATCH', '/metrics/' + existing['id'], body)
        else:
            existing = api('POST', '/metrics_providers/' + provider['id'] + '/metrics', body)
            metrics.append(existing)
        metric['id'] = existing['id']
        config_path.write_text(json.dumps(config, indent=2) + '\n')

    for name, title, status, body in [
        ('Service disruption', 'Service availability issue', 'investigating',
         'We are investigating an issue affecting the selected services. We will provide an update when more information is available.'),
        ('Scheduled platform maintenance', 'Scheduled platform maintenance', 'scheduled',
         'We will perform maintenance during the scheduled window. The selected services may be temporarily unavailable.')]:
        if not any(t['name'] == name for t in templates):
            api('POST', '/incident_templates', {'template': {
                'name': name, 'title': title, 'update_status': status, 'body': body,
                'should_tweet': False, 'should_send_notifications': False}})

    api('PATCH', '', {'page': {
        'name': 'DRG DevLab', 'url': 'https://drgdevlab.com/', 'headline': 'DRG DevLab Service Status',
        'page_description': 'Current service availability, incidents, and planned maintenance for DRG DevLab.',
        'time_zone': 'Hanoi'}})
    for incident in incidents:
        if incident['id'] == 'p843j6spjs3h' and incident['name'] == 'This is an example incident' and incident['status'] == 'resolved':
            api('DELETE', '/incidents/' + incident['id'])
    print(json.dumps({'provisioned': len(config['components']), 'metrics': len(config['metrics'])}))


if __name__ == '__main__':
    main()
