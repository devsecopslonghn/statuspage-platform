#!/usr/bin/env python3
"""Explicit operator provisioning; never called by a Deployment or regular sync."""
import argparse
import copy
import json
import os
from pathlib import Path
import re
import tempfile

from inventory import effective_observation, load_policy, validate_policy
from sync import StatuspageClient, validate_config

PAGE_ID = 'hsyg2r4ql1pr'


def marker(key):
    return '[inventory-key:' + key + ']'


def provisioning_plan(policy, config, components, snapshot):
    """Only approved unmapped records; preserve existing IDs/metadata and all history."""
    if config.get('page_id') != PAGE_ID:
        raise ValueError('Wrong page')
    validate_policy(policy)
    existing_ids = {c['id'] for c in config['components']}
    owned_external = {c['id'] for c in config['components'] if c.get('mode') == 'external'}
    rows = {r['key']: r for r in snapshot['rows']}
    actions = []
    for rule in policy['applications'] + policy.get('nodes', []):
        if not rule.get('publish') or rule.get('retired'):
            continue
        cid = rule.get('component_id')
        if cid:
            if cid not in components or components[cid].get('group') or cid in owned_external:
                raise ValueError('Mapped component missing, grouped or owned by external watchdog')
            owner = next((c for c in config['components'] if c['id'] == cid), None)
            if owner and owner.get('inventory_key') != rule['key']:
                raise ValueError('Mapped component belongs to a different check')
            continue
        key = rule['key']
        tagged = [c for c in components.values() if marker(key) in (c.get('description') or '') and not c.get('group')]
        if len(tagged) > 1:
            raise ValueError('Duplicate inventory identity: operator must reconcile')
        if tagged:
            cid = tagged[0]['id']
            if cid in existing_ids or cid in owned_external:
                raise ValueError('Cannot adopt component owned by a different check')
            actions.append({'key': key, 'action': 'adopt', 'id': cid})
            continue
        if any(c.get('name') == rule['name'] for c in components.values()):
            raise ValueError('Existing unregistered component name: set its ID explicitly instead of recreating')
        row = rows.get(key, {})
        observation = effective_observation(row, policy)
        if not row.get('present') or observation.get('reason') in ('source_stale', 'observation_stale', 'invalid_observation'):
            raise ValueError('Fresh inventory required for creation: ' + key)
        url = rule.get('url') or row.get('url')
        description = '\n'.join(v for v in [url, row.get('assurance'),
            'Logo: ' + rule['logo_url'] if rule.get('logo_url') else None, marker(key)] if v)
        actions.append({'key': key, 'action': 'create', 'component': {
            'name': rule['name'], 'description': description, 'status': 'degraded_performance', 'showcase': True}})
    # Conservatively count groups too until the actual plan's group quota semantics are confirmed.
    count = len(components)
    required = sum(a['action'] == 'create' for a in actions)
    cap = policy.get('component_limit')
    allowed = not required or cap is not None and count + required <= min(cap, 1100)
    return {'existing_components': count, 'required_new_components': required, 'component_limit': cap,
            'capacity_verified': allowed, 'actions': actions}


def save_policy(path, policy):
    path = Path(path)
    validate_policy(policy)
    with tempfile.NamedTemporaryFile('w', dir=path.parent, delete=False) as output:
        json.dump(policy, output, indent=2)
        output.write('\n')
        temporary = output.name
    os.replace(temporary, path)


def apply_plan(plan, policy, client, output):
    if not plan['capacity_verified']:
        raise ValueError('Verify actual plan capacity and set component_limit/evidence before creating components')
    rules = {r['key']: r for r in policy['applications'] + policy.get('nodes', [])}
    # Persist the initial registry and every returned mapping. Do not retry ambiguous POSTs.
    save_policy(output, policy)
    for action in plan['actions']:
        if action['action'] == 'adopt':
            cid = action['id']
        else:
            response = client._request('POST', '/components', {'component': action['component']},
                                       is_mutation=True, retry=False)
            cid = response.get('id')
            if response.get('page_id') != PAGE_ID or not isinstance(cid, str) or not re.fullmatch(r'[a-zA-Z0-9_-]+', cid):
                raise ValueError('Unexpected creation response: inspect page before retrying')
        rules[action['key']]['component_id'] = cid
        save_policy(output, policy)
    return policy


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--policy', required=True)
    parser.add_argument('--config', required=True)
    parser.add_argument('--snapshot', required=True, help='fresh collector inventory JSON, obtained read-only by operator')
    parser.add_argument('--output', help='durable policy registry with component IDs; required with --apply')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    policy = load_policy(args.policy)
    config = json.loads(Path(args.config).read_text())
    validate_config(config)
    snapshot = json.loads(Path(args.snapshot).read_text())
    client = StatuspageClient(PAGE_ID, False)
    components = client.get_components()
    plan = provisioning_plan(policy, config, components, snapshot)
    print(json.dumps(plan, indent=2, ensure_ascii=False))
    if args.apply:
        if not args.output:
            parser.error('--output is required for durable mappings')
        apply_plan(plan, copy.deepcopy(policy), client, args.output)


if __name__ == '__main__':
    main()
