# Statuspage Platform maintenance

This repository owns DRG DevLab Statuspage sync and its private admin console.
Target page: hsyg2r4ql1pr, drgdevlab.statuspage.io. Namespace: statuspage.

- Read README.md and docs/OPERATIONS.md before deployment changes.
- Preserve unrelated work. Keep credentials outside Git and never print Secret values.
- Use GitOps for persistent cluster changes. Application bootstrap remains in the
  cluster root repository; argocd/application.yaml is the matching reference here.
- Admin may edit display metadata; sync writes only health statuses and metrics.
  Preserve active incident/maintenance precedence, independent writer ownership,
  unknown-on-startup debounce, fresh metric checks and redirect-denied API requests.
- Admin backend remains loopback-only behind OAuth2 Proxy. Require the admin group,
  CSRF/Origin, field allowlists and sanitized API responses. Do not expose a generic
  Statuspage proxy or let clients choose management API hosts/page IDs.
- Keep dependencies minimal; current application uses Python standard library.
- Run python3 -m unittest discover -s tests and render kubectl kustomize deploy.
  Check meaningful changed behavior, not only mocks. Verify live Argo and endpoints
  after deployment; no intentional public outage or subscriber test incidents.
- Record changes, verification and limitations in docs/IMPLEMENTATION.md.
