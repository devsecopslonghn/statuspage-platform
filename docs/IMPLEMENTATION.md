# Thực hiện tách Statuspage Platform — 2026-09-27

## Yêu cầu và quyết định

Người dùng yêu cầu link từ Statuspage về website, repo riêng chứa toàn bộ code /
Argo CD / tài liệu, namespace riêng và web quản trị nội bộ. Đã xác nhận tên repo
private `devsecopslonghn/statuspage-platform`, namespace `statuspage`, hostname
`status-admin.apps.drgdevlab.com`, đăng nhập qua Keycloak hiện có.

## Điều tra và thiết kế

- Trang đích/API key đã được xác minh là page `hsyg2r4ql1pr`.
- Repo ban đầu deploy qua Argo root có `prune: false`; cần cleanup rõ ràng.
- Bốn namespace ứng dụng chỉ nhận ingress-nginx/monitoring/nội bộ namespace:
  thêm policy giới hạn nguồn pod sync trong namespace mới và readiness port.
- StorageClass thực tế là `longhorn-workers`, 2 replica, Retain. Không dùng tên
  giả định `longhorn` vì không tồn tại trong cụm.
- Keycloak realm `kubernetes` có nhóm `/k8s-cluster-admins`. Tạo client riêng,
  không tái sử dụng credential của ứng dụng khác và không sửa membership.
- Metadata hiển thị do admin ghi vào Atlassian; health do sync quản lý. Git giữ
  check và deployment. Không đưa Kubernetes API token vào workload.

## Trình tự triển khai

1. Tạo repo private, sao chép sync, tests, lịch sử kế hoạch/verification từ repo cũ.
2. PATCH `page.url=https://drgdevlab.com/`; HTML public đã chứa anchor thật về website.
3. Thêm backend/UI quản trị, CSRF/allowlist/SSO, audit SQLite với PVC riêng.
4. Bootstrap deploy key chỉ đọc cho repo, API Secret và OIDC client/Secret.
5. Deploy staging: UI chạy ở namespace mới; sync replicas0, chưa có heartbeat
   ingress trùng hostname. Writer cũ tiếp tục chạy trong lúc kiểm tra UI.
6. Dừng writer cũ, chuyển production source, heartbeat, Prometheus và workflow
   về repo/namespace mới. Chỉ một writer cho mỗi nhóm component.
7. Xác minh login, quyền, thay đổi metadata thật, audit, checks và Statuspage;
   cleanup app/tài nguyên/Secret/nguồn code cũ sau khi thay thế chạy ổn định.

## Phân công và review

Codex điều phối và review cấu hình / diff / kết quả live. Antigravity được giao
phần backend/UI và unit tests với model `gemini-3.1-pro-high`, effort high, nhóm
quota Gemini (96% weekly / 97% 5h lúc giao); không dùng G1 credits.
Lượt Gemini đầu exit0, JSON SUCCESS nhưng `denied_actions=RunCommand`, stderr cho biết shell
bị headless từ chối và chưa có file. Không coi lượt đó là hoàn thành; giao lại chỉ
bằng file tools (exit0, JSON SUCCESS, stderr trống). Coordinator review actual implementation, sửa API path, CSRF secret fail-closed, validation, lỗi HTTP, group API và CSP/refresh trước khi deploy.

## Bằng chứng nghiệm thu

- Repo private đã tạo, branch `main`, deploy key Argo chỉ đọc, không chứa credential.
- 30 unit tests đã đạt trước khi người dùng bổ sung chỉ dẫn không chạy test nếu
  chưa được yêu cầu. Sau chỉ dẫn đó không chạy thêm suite; CI tự động chỉ render,
  unit test chuyển thành tùy chọn `workflow_dispatch.run_tests`.
- Browser verifier chạy trước chỉ dẫn mới dừng ở lỗi truy cập public timeout;
  user Keycloak tạm đã được xóa. Không ghi nhận full browser login/save là PASS.
- Điều tra live xác định ingress-nginx hostNetwork trên node master không khớp
  namespaceSelector của Cilium khi admin ở worker. Thêm Cilium policy host/node
  trên đúng port4180/8080 và chỉ tin forwarded headers từ node ingress.
- Public admin sau sửa trả302 tới đúng Keycloak client `statuspage-admin`, callback
  đúng hostname; Keycloak trả200 và form đăng nhập thật.
- Đọc API backend bằng quyền operator trong pod trả200, dữ liệu thật gồm10
  components,3 groups và10 cấu hình monitor; API Secret không nằm trong response.
- Pod admin Ready2/2, audit PVC Bound1Gi. Rollout đã sửa cookie secret thành
  URL-safe base64 và required affinity chỉ worker vì master không có Longhorn CSI.
- Cutover/cleanup cuối cùng và kết quả review được bổ sung bên dưới khi hoàn tất.

## Tài liệu tham khảo

- [Statuspage API](https://developer.statuspage.io/): `page.url` là link website;
  component `showcase` là thuộc tính showcase/uptime, không dùng để hứa ẩn component.
- [OAuth2 Proxy configuration](https://oauth2-proxy.github.io/oauth2-proxy/configuration/overview/)
- [Keycloak OIDC provider](https://github.com/oauth2-proxy/oauth2-proxy/blob/master/docs/docs/configuration/providers/keycloak_oidc.md)

## Cutover hoàn tất

- Root repo PR24: bootstrap staged application; PR25 merge
  `b3db54949f24eaae30d46876dad3189fe0c2361d`: production source path `deploy`,
  Prometheus target mới, bỏ code/workflow/docs trùng lặp ở repo cũ.
- Repo riêng initial `1478ecb`; PR1 sửa cookie encoding/probe timeout, PR2 ràng
  buộc worker CSI, PR3 `f66d8d63d67099bae864f3ab274f0167c11e5a0a` sửa hostNetwork
  ingress và chuyển unit tests sang opt-in.
- Đã xóa Application `statuspage-sync`, dừng và chờ pod sync cũ kết thúc trước
  khi bật sync mới. Chuyển heartbeat host sang ingress namespace `statuspage`.
- Hai Deployment `statuspage-admin` và `statuspage-sync` Ready1/1. Admin pod2/2,
  API/config/audit lưu ở namespace mới. API backend đọc được dữ liệu public thật.
- Sync mới qua các vòng debounce, cả8 component nội bộ Operational; metrics
  cycles_failed=0 ở thời điểm kiểm tra. Public heartbeat HTTP200 và timestamp mới.
- Prometheus target `statuspage-sync.statuspage.svc.cluster.local:8080` UP,
  lastError rỗng. Prometheus đã rollout checksum5, không còn target namespace cũ.
- Watchdog repo mới run [36290650215](https://github.com/devsecopslonghn/statuspage-platform/actions/runs/36290650215)
  completed/success sau cutover. Run schedule36280902091 trước đó cũng success.
- Xóa Service, NetworkPolicy, hai ConfigMap và API Secret Statuspage ở `monitoring`;
  inventory hậu kiểm không còn resource tên `statuspage*` tại đó. Namespace
  `monitoring` và workload giám sát khác được giữ nguyên.
- Workflow cũ đã disable, file workflow đã xóa khỏi default branch; Actions Secret
  cũ đã xóa. Repo mới có Secret riêng; chỉ còn một writer theo từng nhóm component.
- Root, Statuspage và platform-observability Argo applications Synced/Healthy.
- Production manifests render và server-side dry-run thành công sau cutover.
- Bảy thay đổi có sẵn của người dùng trong worktree repo gốc được giữ nguyên.

## Checklist nghiệm thu

| Yêu cầu | Bằng chứng |
|---|---|
| Public Statuspage có link website | Page URL `https://drgdevlab.com/`; HTML public có anchor tới website |
| Repo private chứa toàn bộ code/app Argo/docs | `devsecopslonghn/statuspage-platform`, main; README, argocd/application.yaml, deploy/, admin/, sync/, scripts/, docs/ |
| Namespace riêng | Live `statuspage`: sync, admin, secrets, audit PVC; inventory cũ trống |
| Trang quản trị nội bộ được bảo vệ | Public302 tới Keycloak client/callback riêng; login form200; OAuth group restriction, loopback backend, CSRF/field allowlists qua source review |
| Dữ liệu thật và đồng bộ tiếp tục | Admin backend GET200 đọc10components/3groups; sync logs Operational, Prometheus UP, watchdog success |
| Ghi nhận giới hạn xác minh | Full browser login/save chưa chạy lại sau chỉ dẫn không chạy tests; không ghi nhận E2E là PASS |

Các lượt triển khai/rà soát và xử lý nhận xét chi tiết ở [REVIEW.md](REVIEW.md).

## Khôi phục Statuspage sau khi gỡ monitoring — 2026-10-06

- Người dùng xác nhận namespace `monitoring` đã chủ đích gỡ và yêu cầu đổi nguồn
  kiểm tra. Không khôi phục Prometheus hoặc thay đổi repo cluster root.
- Read-only live: sync Running nhưng0/1, log liên tục `Statuspage HTTPError 401`;
  GET bằng Kubernetes API key và file key cũ đều401. GitHub secret được cập nhật
  02:39UTC và watchdog chạy lại thành công; giá trị đó không đọc lại được từ GitHub.
- Ba Kubernetes node Ready;7volume Longhorn attached/healthy,2storage node Ready.
  Public Statuspage có cluster/storage/monitoring degraded, không có active incident.
- Bản sửa local: control plane GET `/readyz` với cluster CA và không có token;
  storage GET Longhorn volumes/nodes với kiểm tra mọi phần tử, yêu cầu dữ liệu
  không rỗng và ít nhất2storage node. Mount CA và mở egress API server/Longhorn.
  Giữ debounce, incident precedence và writer ownership.
- Kiểm chứng read-only bằng code mới: API readiness200 và TLS hợp lệ qua endpoint
  quản trị; Longhorn volumes/nodes PASS qua Kubernetes service proxy. Port-forward
  service không dùng được vì manager không bind loopback9500; đã dừng session và
  dùng proxy tạm trên localhost, cũng đã dừng sau xác minh.
- Python syntax/config validation, render production/staging và `git diff --check`
  đạt. Không chạy unit/browser tests theo AGENTS.md của repo.
- Chưa deploy/push/đổi Secret. Cần file key mới hợp lệ và quyền cập nhật production.
  NetworkPolicy hiện tại chặn request từ pod tới API server và Longhorn; luồng mới
  phải được xác minh lại sau khi deploy. Chưa ghi nhận public page phục hồi.

### Theo dõi sau khi operator cập nhật Secret

- Operator đã patch `statuspage/statuspage-api-key` từ file key mới. GET incident
  và component bằng Secret mới đạt200; ban đầu key mount trong pod còn401 trong
  lúc chờ Secret volume cập nhật, sau đó sync tự phục hồi, không restart.
- Hai vòng sync liên tiếp thành công: heartbeat public200, `ok=true`,
  `last_success_at` tăng từ1791275261.720834 lên1791275321.2942975; pod sync1/1.
  Log vòng tiếp theo không còn401, các application component Operational.
- Cluster/storage vẫn degraded vì cấu hình live còn dùng Prometheus đã gỡ.
  Status Monitoring trên public page vẫn degraded, chờ watchdog quan sát heartbeat
  mới. Bản sửa direct API vẫn local, chưa có quyền deploy/push và chưa áp dụng.

### Triển khai patch direct API

- Người dùng đã cho phép triển khai patch qua GitOps. Worktree chỉ có bảy file
  của bản sửa này; `main` khớp `origin/main` trước triển khai.
- Render production và server-side dry-run đạt cho toàn bộ manifests, bao gồm
  CA mount và Cilium egress mới. Không chạy unit/browser tests theo AGENTS.md.
- Commit `296572c` được push lên main, Argo refresh và automated sync Succeeded.
  Pod sync mới `statuspage-sync-77d55c9ffc-x9g2n`1/1; admin2/2, trang admin chuyển
  hướng đến Keycloak login hoạt động. Cilium policy mới Valid=True.
- Hai vòng sync mới quan sát cluster/storage Operational; vòng thứ hai tự cập
  nhật Statuspage theo recovery threshold2. Heartbeat public200 với `ok=true`.
- Watchdog thủ công run [37436803845](https://github.com/devsecopslonghn/statuspage-platform/actions/runs/37436803845)
  success; public summary trả `All Systems Operational`, indicator `none`, không
  còn component/group non-operational và không có active incident. CI validation
  run [37436617268](https://github.com/devsecopslonghn/statuspage-platform/actions/runs/37436617268)
  success.
- Argo Healthy, sync operation Succeeded. Tổng sync status còn OutOfSync chỉ vì
  hai ConfigMap hash cũ `statuspage-sync-code-7kbb665cfb` và
  `statuspage-sync-config-gd4hm4h4h6` cần prune nhưng Application có `prune:false`.
  Không xóa resource hoặc đổi chính sách prune; tất cả resource mong muốn đã sync.
- Không khôi phục monitoring, không cấp token/RBAC cho sync, không tạo incident
  hoặc gửi subscriber test notification. Kiểm chứng endpoint/runtime read-only;
  chưa chạy unit/browser tests theo chỉ dẫn của repo.

## Discovery ứng dụng và Kubernetes Node — 2026-10-06 (patch local)

### Thay đổi và phạm vi

- Collector riêng Python stdlib, chỉ get/list/watch Ingress/Node, không Secrets,
  write Kubernetes hoặc Statuspage key. Snapshot version1 nội bộ, inventory SQLite
  retained PVC, source lỗi/stale/missing/retired tách rõ; không xóa component/history.
- Canonical/alias/endpoint phụ theo Ingress host/path/TLS và policy đã review;
  override ngoài Kubernetes, include/exclude và public approval riêng inventory.
  URL GET xác minh TLS, DNS pin, allowlist và hard deadline subprocess; không dò
  endpoint readiness. Exact private Auth ingress exception theo CoreDNS live.
- Sync nhận snapshot; giữ backend readiness thật, một writer, debounce3/2 có
  dedupe cả failure/recovery snapshot lặp, incident/maintenance precedence và
  redirect-denied API. Missing/stale/malformed không thành operational.
- Admin SSO/loopback/CSRF/field allowlists giữ nguyên; thêm inventory URL/source,
  timestamp, check scope, lý do/Node conditions, mapping/public approval và
  sync telemetry nội bộ (readiness/content failure, raw/final/debounce/incident). Metadata
  admin không bị sync hoặc provisioner mới reset. Legacy seed apply có guard.
- Provisioner explicit preview/adopt/create mapping được duyệt; fail closed khi
  capacity chưa biết; no ambiguous POST retry, output IDs atomic. Jenkins được
  người dùng duyệt public trong lượt làm này; logo URL được lưu như tham chiếu
  tĩnh. Ba Node mapping được duyệt theo objective, còn chờ component ID thật.

### Evidence chỉ đọc

Collector code mới chạy local từ live Kubernetes GET lúc `2026-10-06T08:59:47.295795+00:00`
trên context `k8s-admin-public`: **17 Ingress, 23 route**, tất cả17Ingress được
đối chiếu vào16application rows; cộng website external override thành17application
rows và3Node rows, tổng20. Không tạo/mất live resource để thử.

| Ingress thực tế | Mapping | Check URL thực tế | Kết quả | Public / lý do loại |
|---|---|---|---|---|
| `keycloak/keycloak` | `authentication` | `https://auth.apps.drgdevlab.com/realms/kubernetes/.well-known/openid-configuration` | 200 | Đã duyệt; ID hiện hữu |
| `card-credit/card-credit` | `card-credit` | `https://card-credit.apps.drgdevlab.com/` | 200 | Đã duyệt; ID hiện hữu |
| `cr7allgoals/cr7allgoals` | `cr7allgoals` | `https://cr7allgoals.com/` | 200 | Đã duyệt; ID hiện hữu |
| `fund-management/fund-management` | `fund-management` | `https://quybpdev.apps.drgdevlab.com/` | 200 | Đã duyệt; ID hiện hữu |
| `fund-management/fund-management-mcp` | `fund-management` | `https://quybpdev.apps.drgdevlab.com/` | 200 | Đã duyệt; ID hiện hữu |
| `argocd/argocd-server` | `ingress/argocd/argocd-server` | `https://argocd.apps.drgdevlab.com/` | 200 | Chưa được duyệt public; giữ nội bộ |
| `bytebase/bytebase` | `ingress/bytebase/bytebase` | `https://bytebase.apps.drgdevlab.com/` | 200 | Chưa được duyệt public; giữ nội bộ |
| `db-poc/db-poc` | `ingress/db-poc/db-poc` | `https://db-poc.apps.drgdevlab.com/` | 200 | Chưa được duyệt public; giữ nội bộ |
| `identity-console/console` | `ingress/identity-console/console` | `https://console.apps.drgdevlab.com/` | 200 | Chưa được duyệt public; giữ nội bộ |
| `identity-console/headlamp` | `ingress/identity-console/headlamp` | `https://headlamp.apps.drgdevlab.com/` | 200 | Chưa được duyệt public; giữ nội bộ |
| `nebula-control/nebula-control-ui` | `ingress/nebula-control/nebula-control-ui` | `https://nebula-control.apps.drgdevlab.com/` | 200 | Chưa được duyệt public; giữ nội bộ |
| `oceanbase-odc/oceanbase-odc` | `ingress/oceanbase-odc/oceanbase-odc` | `https://oceanbase.apps.drgdevlab.com/` | 200 | Chưa được duyệt public; giữ nội bộ |
| `oracle-management/oracle-management` | `ingress/oracle-management/oracle-management` | `https://oracle-management.apps.drgdevlab.com/` | 302 / redirect host denied | Chưa được duyệt public; giữ nội bộ |
| `sonarqube/sonarqube` | `ingress/sonarqube/sonarqube` | `https://sonarqube.apps.drgdevlab.com/` | 200 | Chưa được duyệt public; giữ nội bộ |
| `statuspage/statuspage-admin` | `ingress/statuspage/statuspage-admin` | `https://status-admin.apps.drgdevlab.com/` | excluded: Admin SSO endpoint; excluded from synthetic application checks and public publication | Không probe/public: Admin SSO endpoint; excluded from synthetic application checks and public publication |
| `statuspage/statuspage-sync-health` | `ingress/statuspage/statuspage-sync-health` | `https://status-sync.apps.drgdevlab.com/healthz` | excluded: Independent watchdog owns monitoring; prevent recursive collector health checks | Không probe/public: Independent watchdog owns monitoring; prevent recursive collector health checks |
| `jenkins/jenkins` | `jenkins` | `https://jenkins.apps.drgdevlab.com/` | 403 | Đã duyệt; pending ID |
| `External override (không Ingress)` | `main-page` | `https://drgdevlab.com/` | 200 | Đã duyệt; ID hiện hữu |

CR7 `www` là alias do policy duyệt; `/api` là endpoint phụ. Card Credit `/docs`,
`/mcp` là phụ. Fund Management MCP được nhóm vào cùng app, không tạo duplicate;
root MCP404 trong audit đầu không thay kết quả canonical app. Auth hostname
`database-manager-auth` là endpoint phụ, không tự public thêm. Chỉ canonical URL
được probe theo rule; bảng inventory admin vẫn hiện23routes qua alias/phụ.
Jenkins root403 TLS hợp lệ là failed URL availability; logo SVG200 với TLS hợp lệ
chỉ chứng minh static asset, không thay bằng Jenkins/backend healthy. Oracle
Management redirect sang host khác bị deny mặc định; cần owner duyệt SSO rule
nếu muốn probe login, không nới redirect toàn cục.

| Kubernetes Node | Ready | Memory/Disk/PIDPressure | Ready heartbeat UTC | Raw mapping |
|---|---|---|---|---|
| k8s-master | True | False/False/False | 2026-10-06T08:55:56Z | operational (ready_no_pressure) |
| k8s-worker1 | True | False/False/False | 2026-10-06T08:59:19Z | operational (ready_no_pressure) |
| k8s-worker2 | True | False/False/False | 2026-10-06T08:56:53Z | operational (ready_no_pressure) |

Cả ba node non-cordoned ở snapshot này. Node observation lấy đúng Node API,
không dùng `/readyz` hay Longhorn. Không kết luận sức khỏe mọi workload/nghiệp vụ.
Public page trước patch có9components +3groups; GET Manage page được sanitize,
không chứa plan/limit fields. Không đoán gói Free/hạn mức25; `component_limit`
đangnull và create bị chặn. Quota tính bảo thủ cả group;9+3+4=16slot cần được
operator xác minh. AppProject live đã allow Statuspage repo và cluster RBAC;
root checkout có drift cũ và thay đổi người dùng, không sửa file của root repo.

### Kiểm chứng

- Local suite66tests PASS, gồm46tests ban đầu/collector rồi bổ sung writer/admin/
  provisioning/deadline/private scope, admin route gate và SQLite restart retention. Bao phủ URL fail, TLS/timeout/redirect/SSRF,
  stale/future/malformed, NotReady/Unknown/pressure/cordon/retirement, new/missing
  resource, history/IDs, dedupe failure/recovery, source unavailable, expiry trong lúc đọc Management API, writer
  ownership, incident/maintenance, capacity và no-POST-retry registry.
- Probe code runtime bounded worker chạy thật trên URL canonical live; Jenkins
  root403/logo200, ba node riêng Ready/no pressure. Các app/admin200 chỉ là URL
  availability; backend checks giữ nguyên nhưng chưa chạy lại live trong pod mới.
- Production/staging Kustomize render PASS, Python syntax/config/policy validation,
  JavaScript `node --check`, `git diff --check` PASS. Review delegated code/manifests
  xong, sửa repeated failure samples, hard DNS deadline, source freshness summary,
  collector error readiness reachability và hash ConfigMap rollout.
- Không chạy browser login/save E2E, server-side apply/dry-run hoặc tạo tài nguyên
  test trong production. Chưa chứng minh network/RBAC/pod runtime sau rollout;
  cần kiểm tra read-only sau khi được phép triển khai.

### Phần còn lại và giao diện ticket

Patch local sẵn để review; chưa commit/push/merge/deploy, thay Secret, tạo component,
incident/ticket hoặc gửi notification. Cần xác minh capacity, preview/provision
Jenkins+3node hoặc adopt IDs có sẵn, giữ registry vào Git, rồi xin duyệt rollout.
Chưa có live component riêng của ba node/Jenkins: không ghi nhận nghiệm thu public
rollout hoàn thành. URL/logo trong component description là tham chiếu; API không
có field per-component URL/icon và custom HTML không hiển thị trên subdomain
statuspage.io. Clickable directory/icon public cần UI/domain decision, không giả
vờ tooltip là portal click-to-open. Admin có clickable URL/logo để vận hành.

Event v1/lifecycle cho bước `health_failure -> ticket/incident -> investigating
-> fix_deployed -> verification -> recovered` được ghi trong OPERATIONS. Chưa
chọn/đoán Jira/GitHub Issues, chưa kết nối external ticket service hoặc đảm bảo
outbox delivery.


## Điều tra Partial System Outage sau rollout discovery — 2026-10-06

- Lượt này chỉ đọc cluster/Statuspage và chuẩn bị bản sửa; không đổi status bằng
  tay, tạo incident hoặc patch workload. User đã merge PR6; Argo main revision
  `33dff3100ef130da18838063b283df49abcada7a`, operation Succeeded/Healthy.
- Public: Website major_outage; CR7/Card Credit/Fund/Auth partial_outage; cluster,
  storage và hai external component operational. Không có incident/maintenance.
  Heartbeat200 `ok=true`: sync ghi được observations nhưng không có nghĩa URL
  checks đều healthy. Ba Kubernetes node Ready.
- Collector Ingress/Node sources fresh; tất cả URL probe có
  `probe_deadline_exceeded`. Sync telemetry xác nhận URLfalse, readiness/content
  checks true cho các ứng dụng/Auth: nguồn outage hiện tại là URL probe collector.
- Collector CPU limit250m,8subprocess đồng thời. Cgroup snapshot:
  `nr_throttled=1335/nr_periods=1474`, `throttled_usec=510950026` (thời gian cộng
  dồn, không suy thành tỷ lệ thời gian wall-clock). Direct GET website trong chính
  pod đạt200~421ms; một worker bounded đạt200~3,09giây. DNS website~135ms.
- Thử chỉ đọc với2worker đồng thời trong pod: Website, CR7, Card Credit, Fund
  đều200, toàn batch4URL~12,37giây; mỗi probe vẫn deadline10giây. Điều này xác định
  CPU contention lúc startup Python/TLS khiến8worker timeout giả. Không khẳng
  định toàn bộ platform/nghiệp vụ healthy chỉ từ tập URL này.
- Bản sửa: giới hạn thread pool collector còn2; giữ250m CPU, TLS/SSRF validation,
  deadline hữu hạn, expected status, debounce và incident precedence. Regression
  test chạy12resource kiểm tra tất cả được probe/giữ inventory và concurrency<=2.
  Jenkins403 vẫn là availability failure thật; không dùng logo200 để làm xanh.
- Local suite67tests PASS; render production/staging và `git diff --check` PASS.
  Bản sửa chưa merge/deploy; cần rollout có phép và ít nhất2fresh observation để
  xác minh sync tự phục hồi public status. Không sửa Secret hoặc component IDs.


### Xác minh main và public phục hồi sau hotfix

- User cho phép push trực tiếp main để Argo triển khai. Khi bắt đầu thực hiện,
  remote main đã có PR7 squash commit `3c9b320de559bbfb2a94ef6772134e72839eafbc`
  chứa đúng hotfix; không tạo bản sao thay đổi. Local main fast-forward theo remote.
- Argo đã chạy đúng revision3c9b320, operation Succeeded; collector/sync rollout
  thành công, admin/collector/sync đều Deployment Ready1/1, admin pod2/2.
- Collector có các snapshot nguồn mới riêng tại1791279508.773623,
  1791279568.7751386 và1791279628.775453. Website/CR7/Card Credit/Fund/Auth đều
  URL HTTP200. Backend/OIDC checks true trong sync. Sau đủ fresh recovery samples,
  sync tự PATCH các component về operational; không sửa status bằng tay.
- Public GET summary lúc1791279717.8073497 trả indicator none,
  `All Systems Operational`, không có component/group non-operational và không
  incident. Heartbeat HTTP200, ok=true, last_success_at1791279711.6251504.
  Đây là trạng thái của tập component đang được monitor, không kiểm chứng mọi
  workload/nghiệp vụ trong cluster.
- Admin URL chuyển tới Keycloak login hoạt động; public `/observations` trả404,
  telemetry nội bộ không bị expose. Không chạy browser login/save E2E.
- Argo health Healthy; tổng sync vẫn OutOfSync chỉ bởi6ConfigMap hash cũ
  requiresPruning=true, tất cả resource mong muốn đã Synced. Không prune/xóa
  ConfigMap hoặc đổi chính sách prune:false trong lượt này.
- Jenkins403 tiếp tục là failed URL availability trong inventory; Jenkins và
  ba node vẫn pending component mapping vì chưa xác minh capacity/provision ID.
  Không ghi nhận bốn component mới đã public. Không đổi Secret, ID, incident,
  ticket hoặc gửi thông báo. Bằng chứng xác minh được push vào main riêng bằng
  commit tài liệu sau rollout; code runtime không thay đổi.

### URL ở tooltip public và rule mặc định — 2026-10-06

- Theo yêu cầu bổ sung URL của user, đã review danh sách 7 component hiện hữu có
  endpoint public, đọc mô tả mới nhất và kiểm tra không đổi kể từ preview. Chỉ
  PATCH `description` bằng adapter admin có audit dưới actor
  `codex-operator: user-requested public URL metadata`; giữ nội dung cũ rồi thêm
  URL. Không thêm URL backend/private, không đổi tên/group/status/showcase/ID.
- Applications: Website `https://drgdevlab.com/`, CR7
  `https://cr7allgoals.com/`, Card Credit
  `https://card-credit.apps.drgdevlab.com/`, Fund Management
  `https://quybpdev.apps.drgdevlab.com/`. Authentication thêm URL OIDC discovery
  đã có trong policy. External Service Access thêm ba URL probe hiện hữu; Status
  Monitoring thêm `https://status-sync.apps.drgdevlab.com/healthz`.
- Kiểm tra public JSON xác nhận 7 description đúng và public HTML có 7 tooltip
  `data-original-title` tương ứng. Audit có 7 SUCCESS, mỗi record chỉ trường
  description. Đây là văn bản URL trong tooltip, không phải link/icon component
  có thể click. Không chạy browser login/save E2E.
- Public summary sau cập nhật: All Systems Operational, indicator none, không
  component/group non-operational và không incident. Heartbeat HTTP 200,
  ok=true, last_success_at1791280545.831327. Argo Healthy; OutOfSync vẫn do các
  ConfigMap cũ pending prune, không thay đổi tài nguyên cluster trong lượt này.
- OPERATIONS bổ sung sự khác nhau giữa rule tổng hợp/history mặc định của
  Statuspage và rule HTTP/readiness/debounce của collector/sync. Uptime 90 ngày vẫn
  phản ánh lịch sử trạng thái đã ghi; không chỉnh sửa lịch sử hoặc start date.
  Metadata hiện hữu thuộc admin, không biến config seed thành bộ đồng bộ mô tả.
- Chỉ thay đổi tài liệu trong Git; `git diff --check` PASS. Không thay code/config
  deployment nên không chạy lại test suite hoặc rollout; main push đã được user
  cho phép. Jenkins và ba node vẫn pending mapping/capacity như ghi nhận trước đó.
