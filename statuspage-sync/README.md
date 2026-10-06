# DRG DevLab Statuspage synchronization

Public page: https://drgdevlab.statuspage.io

See [current operations](../docs/OPERATIONS.md) and the historical [implementation plan](../docs/statuspage/PLAN.md) for ownership, cadence,
limitations, acceptance and rollback. `sync.py` uses only the Python standard
library; it exposes a minimal heartbeat, not a portal.

```bash
python3 -m unittest discover -s tests
kubectl kustomize deploy
STATUSPAGE_API_KEY_FILE=/private/path/key python3 statuspage-sync/provision.py
STATUSPAGE_API_KEY_FILE=/private/path/key python3 statuspage-sync/sync.py --config statuspage-sync/config.json --mode external --once --dry-run
```

Provisioning is an explicit operator command, never a background daemon action.
It locks the target page identity and stores a private backup outside Git before
applying changes. Configuration contains public component/metric IDs only.

Argo CD owns Deployment, Service, ConfigMaps, NetworkPolicy and heartbeat Ingress.
The runtime Secret is provisioned out of band and not owned by Argo. Secret setup:

```bash
kubectl -n statuspage create secret generic statuspage-api-key --from-file=api-key=/private/path/key --dry-run=client -o json | kubectl apply --server-side --field-manager=statuspage-secret-bootstrap -f -
gh secret set STATUSPAGE_API_KEY --repo devsecopslonghn/statuspage-platform < /private/path/key
```

Only `/healthz` is routed by public Ingress. `/metrics` remains available inside
the cluster for Prometheus. Health contains only success and last-success time;
it does not reveal URLs, component states, query text, or credentials.

Change health checks through Git and verify them before merge. Monitor API errors
and both execution histories. A completed sync means observations reached
Statuspage, even when the observed application itself is down.

Discovery uses `collector/policy.json` and the separate read-only collector.
`inventory.py` defines the version1 snapshot contract, URL safety and freshness.
Existing IDs remain in config; additional approved mappings live in policy.
`provision_discovery.py` previews/adopts/creates only approved pending mappings
with verified component capacity. It runs only as an explicitly authorized operator
command, never in sync/collector. Legacy seed apply is blocked with discovery on.
See the operations runbook for fresh snapshots, registry persistence and rollout.
