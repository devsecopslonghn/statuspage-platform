# Deployment verification — 2026-09-27

> Historical first rollout from the cluster repository. For the dedicated repository, namespace and admin console, see [current operations](../OPERATIONS.md) and [implementation record](../IMPLEMENTATION.md).

## Working replacement

- PR #20 merged as `5f409f51c10e0a7340231b959b93d1aacfc1588b`.
- Argo CD `statuspage-sync`: Synced / Healthy, operation Succeeded.
- Deployment `monitoring/statuspage-sync`: 1/1 Ready; singleton Recreate strategy.
- Two observed cycles completed successfully with all eight internal components Operational.
- Public `/healthz` returned HTTP 200 and a recent last-success timestamp.
- Public `/metrics` returned HTTP 404; the same endpoint inside the pod returned
  sync cycle counters (2 total, 0 failed at initial verification).
- Public page contains ten non-group components in three groups; no example incident remains.
- Custom metric display APIs returned real data: service checks 100%, successful
  HTTP response time approximately 419.5 ms at initial verification. These values vary.
- GitHub-hosted external workflow run `36265619859`: success; both external-access
  and monitoring-freshness checks Operational.
- 17 focused unit tests passed locally and the first rollout CI passed. Server
  dry-run passed for the new resources. Credential scan found no key in task files.

## Portal retirement and final checks

- PR #21 merged as `31be17dd46cef13a2f00a2d21446baa5d6cee39b` after the replacement checks passed.
- Argo root observed that revision before explicit cleanup, required because its
  automated policy has `prune: false`.
- Removed Application `portal`, its Deployment, Service, Ingress, ConfigMap and
  ServiceAccount, then namespace `portal`. Namespace inventory contained no PVCs,
  Secrets or unrelated workloads. ClusterRole and ClusterRoleBinding
  `ingress-portal-reader` were also removed. Follow-up GETs confirmed absence.
- `https://portal.apps.drgdevlab.com/` returned HTTP 404 after removal.
- Live verification caught an existing Kustomize patch that replaced the whole
  Prometheus annotation map, suppressing the config rollout checksum. PR #22
  (`9cdf6565a6a25745bbb075ed002e3a240384660c`) preserves both annotations.
  Prometheus then completed its Recreate rollout and reached 1/1 Ready.
- The live Prometheus targets API reported `statuspage-sync` UP, with no error
  and no remaining `portal` target.
- Post-retirement sync counters showed 10 completed cycles and 0 failed cycles.
- Argo root, `statuspage-sync` and `platform-observability` were Synced / Healthy.
- Public summary identified page `hsyg2r4ql1pr`, all ten service components
  Operational and no active incidents; public heartbeat remained HTTP 200.
- All 17 focused unit tests passed again on the merged implementation.
- The original development worktree's seven pre-existing changes were preserved;
  implementation and review used a separate worktree.

## Expected limitations

The external GitHub schedule is normally 30 minutes and can be delayed. Statuspage
is available independently from the cluster; the internal component readings can
be stale when the cluster is unreachable. External Service Access and Status
Monitoring communicate that condition. Neither probe proves a complete business
transaction or interactive login. No live customer outage or subscriber-notifying
incident was deliberately created as part of verification.
