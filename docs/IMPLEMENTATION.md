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
