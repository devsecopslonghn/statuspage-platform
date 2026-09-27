# DRG DevLab Statuspage implementation

> Historical first rollout from the cluster repository. For the dedicated repository, namespace and admin console, see [current operations](../OPERATIONS.md) and [implementation record](../IMPLEMENTATION.md).

Target: https://drgdevlab.statuspage.io, page `hsyg2r4ql1pr`.
The former Argo CD `portal` domain directory has been retired. Atlassian hosts
the public status page outside Kubernetes. See [rollout evidence](VERIFICATION.md).

## Scope and ownership

Replace the domain directory with a curated service status page. The initial
page has four application components (website, CR7, Card Credit, Fund Management),
Authentication, Kubernetes Control Plane, Persistent
Storage, External Service Access, and Status Monitoring. Component IDs and check
definitions live in `statuspage-sync/config.json` and are reviewed through Git.
New Ingress resources do not automatically become public components.

An Argo CD-managed singleton Deployment in `monitoring` checks public HTTPS
routes, internal backend readiness and existing Prometheus data every 60 seconds.
It uses no Kubernetes API credentials. Three consecutive unhealthy observations
change a component, and two healthy observations recover it. Partial failures
produce partial outage; loss of monitoring data never becomes a healthy check.
On startup no state change is published until observations satisfy the threshold.

An independent GitHub-hosted workflow checks public application routes and the
sync heartbeat at minutes 17 and 47. It owns only External Service Access and
Status Monitoring, so writers do not race. GitHub schedules may be delayed or
disabled, and a private repository consumes Actions quota. This is a fallback,
not a 30-minute detection SLA. A continuously running off-cluster monitor can use
the same external command later. If Status Monitoring is degraded, application
statuses may be stale. The watchdog can still flag public access loss when all
Kubernetes nodes are unavailable. A delayed/missing watchdog run itself requires
checking Actions run history; Statuspage cannot independently detect that absence.

Manual active incidents and active maintenance take precedence. The integration
never creates or resolves incidents. Incident email/SMS notification is operator
controlled; Statuspage may emit component-change webhooks to existing subscribers.
Incident and maintenance templates start with notifications unselected. Operators
use the Atlassian management UI for incident communication and maintenance.

Two custom metrics show the percentage of current service checks passing and
mean successful HTTP probe time. These are observation metrics, not uptime/SLA
claims. HTTP timing is measured from the cluster. Failed requests are excluded
from latency, and an empty sample set is not converted to zero latency.

## Rollout

1. Verify the correct page and key, inspect live Argo and health endpoints.
2. Back up existing page configuration, repurpose sample components and create
   the curated groups, metrics and templates. Remove only the known example
   resolved incident, retaining a local private backup.
3. Review and test the sync implementation, provision runtime secrets, merge
   the application into `master`, let Argo reconcile the manifests, and run
   a GitHub-hosted external check.
4. Verify several internal cycles, all checks, public JSON status, custom metric
   samples, exact health endpoint, manual override tests and external workflow.
5. Remove the portal GitOps Application and scrape target, then explicitly retire
   its stateless resources because the root Application uses `prune: false`.
   Verify no portal Deployment, Service, Ingress, ConfigMap or RBAC remains.

## Acceptance

- Target page has real grouped components and observations, not sample services.
- Backend health paths are tested directly; a SPA HTTP 200 cannot fake API health.
- API redirects cannot leak credentials; missing/stale/malformed metrics are failures.
- Daemon restart cannot reset an existing outage to green.
- Manual incident/maintenance and external writer ownership are preserved.
- Runtime key is not present in Git, ConfigMaps, image, logs, or public endpoints.
- Internal and external runs are verified and monitoring cadence is documented.
- Argo is Synced/Healthy and the old portal is removed after replacement verification.

## Rollback and operations

Stop sync writers before changing ownership: scale the Deployment to zero through
Git and disable the external workflow. Operators retain access to Atlassian even
if the cluster fails. Revert the portal retirement commit and recreate its Argo
Application from Git if rollback is needed. Do not delete Statuspage history or
change component identities during an infrastructure rollback.

Rotate the API key by replacing Kubernetes Secret `monitoring/statuspage-api-key`
and GitHub Actions secret `STATUSPAGE_API_KEY`. The mounted key is reread on API
requests. Keys have broad Statuspage access; only the explicit page/ID allowlist is
used. Never commit key material. Renew before expiry and test both writers.

Sources: [Statuspage API](https://developer.statuspage.io/),
[Statuspage purpose](https://support.atlassian.com/statuspage/docs/what-is-statuspage/),
[API limits](https://support.atlassian.com/statuspage/docs/what-are-the-different-apis-under-statuspage/),
[GitHub schedule limitations](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).
