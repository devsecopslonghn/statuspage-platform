# Vận hành Statuspage Platform

## GitOps và bootstrap

Namespace `statuspage` giữ sync, admin, API Secret, OAuth Secret và audit PVC.
NetworkPolicy `allow-statuspage-probes` ở bốn namespace ứng dụng chỉ cho pod
`app=statuspage-sync` trong namespace `statuspage` gọi TCP readiness port tương ứng.
Sync/admin không có Kubernetes ServiceAccount token hoặc RBAC. Collector riêng
chỉ có get/list/watch Ingress và Node; không có Statuspage key, quyền Secret hay
quyền ghi Kubernetes.

Argo Application chuẩn ở `argocd/application.yaml`. Root Application của cụm vẫn
thuộc `devsecopslonghn/k8s-namepsace-chart`: nó giữ bản bootstrap Application và
AppProject source allowlist. Mọi mã ứng dụng, cấu hình check và manifest workload
đều lấy từ repo này, branch `main`, path `deploy`. Khi đổi source/path của chính
Application, cập nhật cả bootstrap trong root repo để self-heal không ghi đè.

Bootstrap theo thứ tự:

1. Cho phép repo SSH này trong `gitops/projects.yaml` của root repo.
2. Chạy `scripts/bootstrap_gitops.sh` nếu Argo chưa có quyền đọc repo. Script tạo
   deploy key chỉ đọc và Secret `argocd/repo-statuspage-platform`; không in private key.
3. Commit Application vào root GitOps. `CreateNamespace=true` tạo namespace.
4. Provision các secret sau từ máy quản trị:

```bash
kubectl -n statuspage create secret generic statuspage-api-key \
  --from-file=api-key=/private/path/hsyg2r4ql1pr.key --dry-run=client -o json | \
  kubectl apply --server-side --field-manager=statuspage-secret-bootstrap -f -
python3 scripts/bootstrap_identity.py
gh secret set STATUSPAGE_API_KEY --repo devsecopslonghn/statuspage-platform < /private/path/hsyg2r4ql1pr.key
```

Secret không thuộc Git. Script identity đọc Secret `keycloak/keycloak-runtime`
qua quyền operator, cấu hình confidential OIDC client `statuspage-admin`, redirect
URI chính xác, PKCE S256 và mapper groups. Không thay quyền/thành viên nhóm hiện có.
Cookie/CSRF secret được giữ khi chạy lại. Runtime pod không đọc Secret Keycloak.
Khi rotate client/cookie/CSRF secret, restart admin bằng một thay đổi annotation
qua Git; cookie cũ có thể cần đăng nhập lại. API key được đọc lại trên từng request.

## Quyền và bảo vệ trang quản trị

Chỉ nhóm Keycloak `/k8s-cluster-admins` truy cập được. OAuth2 Proxy kiểm tra nhóm;
backend kiểm tra identity/group lần nữa. Service chỉ expose proxy4180, backend
bind loopback8081 trong cùng pod. NetworkPolicy cho ingress-nginx; Cilium policy bổ sung traffic từ host/node trên đúng port4180 vì ingress-nginx chạy hostNetwork. Backend8081 không được expose. OAuth2 Proxy chỉ tin forwarded headers từ node ingress `100.125.138.71/32`.
Đây là trang giới hạn bằng SSO, không phải endpoint chỉ có IP private/VPN.

API chỉ cho sửa metadata được phép của đúng page `hsyg2r4ql1pr`. Không có generic
API proxy. PATCH yêu cầu Origin đúng, JSON và token CSRF gắn với identity có hạn.
Credential chỉ ở phía server. Các request không log cookie/token/API key.
Public ingress heartbeat chỉ expose chính xác `/healthz`; `/metrics` không public.

## Sửa check hoặc thêm dịch vụ

Check definition tại `statuspage-sync/config.json` dùng component ID ổn định.
Ứng dụng/Node được collector khám phá vào inventory nội bộ. Khi công bố thêm,
dùng quy trình discovery bên dưới để duyệt policy và provision/adopt ID ổn định;
backend readiness chỉ thêm khi có endpoint thật đã xác minh.
Không xóa/recreate component đang dùng vì sẽ mất ánh xạ và history.
Tên/mô tả hiện tại trên Atlassian là nguồn metadata; config giữ thông tin seed.
`provision.py --apply` dành cho bootstrap có backup, có thể reset metadata;
không nằm trong workflow hoặc Deployment.

## Kiểm tra hoạt động

```bash
kubectl -n argocd get application statuspage-platform
kubectl -n statuspage get deploy,pod,pvc,ingress
kubectl -n statuspage logs deployment/statuspage-sync --tail=20
curl -fsS https://status-sync.apps.drgdevlab.com/healthz
gh run list --repo devsecopslonghn/statuspage-platform --workflow statuspage-external.yml
```

Namespace monitoring/Prometheus đã chủ đích gỡ. Counters nội bộ tại
`statuspage-sync.statuspage.svc.cluster.local:8080/metrics` vẫn dùng để chẩn đoán:
 `statuspage_sync_cycles_total`, `statuspage_sync_cycles_failed_total`,
`statuspage_sync_last_success_timestamp_seconds`. Nếu heartbeat stale >180 giây,
watchdog ngoài cụm có thể đánh dấu Status Monitoring lỗi. Workflow lịch 30 phút
có thể bị GitHub trì hoãn; theo dõi run history và Actions quota. Nó không phải SLA.
Lỗi API/monitoring không được xem là healthy. Khi cụm down, public page vẫn ở
Atlassian, nhưng reading nội bộ có thể stale. Quản trị Atlassian vẫn dùng được.

### Kiểm tra platform khi không còn Prometheus

Sync kiểm tra control plane bằng HTTPS GET
`https://kubernetes.default.svc.cluster.local/readyz`, xác minh chứng chỉ bằng
ConfigMap `kube-root-ca.crt` mount tại `/trust/ca.crt`. Request không dùng token;
không cấp ServiceAccount token hoặc RBAC cho workload. Check này chỉ xác nhận
readiness của API server (bao gồm etcd), không còn đo readiness của từng worker.
Cilium policy `statuspage-sync-apiserver-egress` cho phép đúng API server.

Storage dùng GET `/v1/volumes` và `/v1/nodes` tại
`longhorn-backend.longhorn-system.svc.cluster.local:9500`. Mọi volume trả về phải
có `robustness=healthy`; phải có ít nhất hai storage node và mọi node trả về có
`conditions.Ready.status=True`. Response rỗng, lỗi, thiếu trường hoặc vượt 2MiB
đều thất bại. NetworkPolicy chỉ mở port9500 tới namespace `longhorn-system`.
Không dùng metric lịch sử, không cần khôi phục namespace `monitoring`.

HTTP check hỗ trợ `body_json_items` (đường dẫn collection),
`body_json_item_values` (các trường phải khớp trên mọi phần tử) và `min_items`
(mặc định1, tối thiểu1). `ca_file` chỉ cho phép `/trust/ca.crt` với HTTPS internal.
Debounce và ưu tiên incident/maintenance giữ nguyên.

### Khôi phục khi API key bị từ chối

Nếu log có `Statuspage HTTPError 401`, kiểm tra key mới bằng Manage API GET
trên page `hsyg2r4ql1pr` trước khi thay key. Không in key hoặc response quản trị.
Key cần quyền đọc incident/maintenance/component và ghi component/metric.
Cập nhật cả Kubernetes Secret `statuspage/statuspage-api-key` và GitHub secret
`STATUSPAGE_API_KEY` theo lệnh bootstrap ở trên; đổi một nơi không đổi nơi kia.
GitHub không cho đọc lại giá trị secret, cần file key hợp lệ từ operator.

Sync đọc lại key trên từng request; Secret volume tự cập nhật nên không cần
restart chỉ để rotate key. Admin dùng cùng Kubernetes Secret. Chờ một vòng sync,
kiểm tra pod Ready và heartbeat HTTP200. Sau hai quan sát thành công liên tiếp,
component nội bộ được phục hồi; watchdog ngoài cụm cập nhật Status Monitoring ở
lần chạy tiếp theo. Khi cần phục hồi ngay, operator chạy watchdog thủ công sau
khi heartbeat đạt200. Chỉ ghi Operational dựa trên check thật.

## Audit, lưu trữ và backup

Audit SQLite nằm trong PVC `statuspage-admin-data` 1Gi, StorageClass
`longhorn-workers` (Retain, 2 replica). Deployment dùng Recreate và chỉ chạy trên hai worker có Longhorn CSI. Volume giữ audit
qua restart; PVC được bảo vệ khỏi prune/delete bằng Argo annotation. Hai replica
không thay cho backup off-cluster. Metadata chính nằm trên Atlassian, không ở SQLite.

Trước khi nâng cấp, backup audit bằng SQLite online backup API trong pod rồi
copy file qua `kubectl cp`; không copy riêng file DB khi WAL đang hoạt động.
Tên file DB thực tế xem `admin/server.py`. Lưu backup riêng và thử restore định kỳ.
Backup cấu hình public bằng Manage API GET page/components/groups/metrics, lọc
hoặc bảo vệ file riêng vì API response có thể chứa automation email. Không commit
raw response/Secret vào Git. Không deploy mock/fake status để thử trong production.

## Khôi phục

- Lỗi UI: rollback commit qua Git/Argo; sync có thể chạy độc lập. Đăng nhập
  `https://manage.statuspage.io/pages/hsyg2r4ql1pr/incidents` để quản trị trực tiếp.
- Lỗi sync: revert check/config; nếu cần dừng writer thì đặt replicas0 qua Git
  và disable external workflow trước khi sửa tay status.
- Mất audit PVC: khôi phục backup vào volume mới, mount lại đúng path và quyền1000.
- Chuyển namespace: chỉ một sync writer được chạy; dừng writer cũ trước khi bật
  writer mới, chuyển heartbeat và scrape target, kiểm tra rồi mới xóa tài nguyên cũ.
- Không xóa namespace/PVC để xử lý lỗi triển khai thông thường.

## Backup audit bằng SQLite online backup

Thực hiện bằng quyền operator; ví dụ này không dừng phục vụ và không đọc Secret:

```bash
kubectl -n statuspage exec deployment/statuspage-admin -c admin -- \
  python3 -c "import sqlite3; source=sqlite3.connect('/data/audit.db'); dest=sqlite3.connect('/data/audit-backup.sqlite'); source.backup(dest); dest.close(); source.close()"
# Lấy tên pod từ kubectl -n statuspage get pods, rồi:
kubectl -n statuspage cp <admin-pod>:/data/audit-backup.sqlite ./audit-backup.sqlite -c admin
```

File backup có identity người thao tác; lưu ngoài Git và giới hạn người đọc.
Các file `scripts/verify_admin_browser.py` và `tests/` là công cụ tùy chọn, chỉ
chạy khi được yêu cầu. Browser verifier tạo một user Keycloak tạm và xóa trong
`finally`; không tạo incident hoặc gửi notification.

Cilium reference: [host/node identity and namespace policy](https://docs.cilium.io/en/stable/network/kubernetes/policy/).

## Discovery URL và từng node

### Nguồn, quyền và phạm vi

`statuspage-collector` singleton trong namespace `statuspage` LIST paginated
Ingress/Node mỗi60giây. ClusterRole chỉ get/list/watch hai resource này, không
Secret, Lease hoặc write. Token projected3600giây được đọc lại từng request;
HTTPS Kubernetes xác minh cluster CA và cấm redirect. Collector không mount
Statuspage credential. Sync/admin không có token Kubernetes; chúng đọc endpoint
cố định `http://statuspage-collector.statuspage.svc.cluster.local:8082/inventory`.
Không có Ingress public cho collector; NetworkPolicy chỉ nhận từ sync/admin.
Service dùng `publishNotReadyAddresses` để dữ liệu lỗi vẫn được đọc và chẩn đoán
khi collector readiness503. Admin inventory vẫn đọc được nếu Manage API lỗi.

Inventory SQLite trên PVC riêng1Gi `longhorn-workers`, Recreate, Retain và
Prune=false/Delete=false, giữ tombstone qua restart. Sao lưu bằng SQLite online
backup API giống audit DB, path `/data/inventory.db`; không xóa PVC để refresh.
Collector restart đánh dấu observation persisted stale cho tới khi list/probe mới.
ConfigMap policy/code có hash để đổi policy rollout tất cả consumer.

Policy `collector/policy.json` là nguồn duyệt, không tin annotation để tự public:

- `applications[].resources` nhóm nhiều Ingress vào một key; `url` chọn canonical
  (host phải còn có trong Ingress), `alias_hosts` khai báo alias; các route khác là
  endpoint phụ và không tự thành component/check riêng. Key không dựa UID để
  recreate tài nguyên cùng tên giữ mapping. Không suy đoán hai Ingress khác ứng
  dụng là cùng service; inventory mới cần owner review grouping.
- `publish:false` mặc định cho tài nguyên mới và endpoint admin/private. Nó vẫn
  được inventory/probe nội bộ nếu URL an toàn. `exclude_reason` giữ entry giải
  thích việc không probe; admin/heartbeat Statuspage là hai exclusion hiện tại.
- `name`, `url`, `expected_status`, `redirect`, `redirect_hosts`, `assurance` là
  override được review; `source:external` hỗ trợ URL ngoài Kubernetes. `logo_url`
  chỉ là tham chiếu tài nguyên tĩnh, không dùng làm backend health.
- `publish:true` + `component_id` mới cho phép sync ghi status. ID hiện hữu được
  giữ; không xóa/recreate component/history. Tài nguyên mới tự xuất hiện nhưng
  không tự public. `retired:true` + `retirement_reason` dừng health writer, giữ ID
  và lịch sử, admin hiện retired; Statuspage không có enum retired nên status cũ
  được giữ và operator cập nhật mô tả retirement bằng admin nếu cần. Missing
  không đồng nghĩa retired.

Canonical URL phải HTTPS, không credential/query/fragment/port lạ, không IP
literal, traversal hoặc path regex. Scope: đúng `drgdevlab.com`, `cr7allgoals.com`,
`www.cr7allgoals.com` và subdomain `apps.drgdevlab.com`. DNS IP public được kiểm
tra rồi pin khi kết nối, TLS vẫn xác minh hostname/SNI. Mọi redirect được kiểm
tra lại host/IP; `deny` mặc định, `same_host` tối đa5hop, `allowlisted` cần duyệt
`redirect_hosts` cho SSO. Query SSO không được xuất/log. CoreDNS live trỏ Auth
về `100.125.138.71`: chỉ cặp `auth.apps.drgdevlab.com`/IP này được ngoại lệ private
trong policy, Cilium cho host/node TCP443 để tới ingress. Không cho loopback,
link-local hoặc IP private bất kỳ. Chuyển ingress cần cập nhật ngoại lệ đúng IP.

Collector chạy tối đa2probe subprocess đồng thời với CPU limit250m. Không tăng
concurrency nếu chưa đo lại startup Python/TLS dưới CPU quota:8worker đã gây
throttle và timeout giả trong probe budget. Deadline tính cả startup worker.

Mỗi probe là HTTPS GET URL thật, default HTTP200, check TLS, deadline10giây;
worker subprocess bị kill khi hết hạn (cả DNS/TLS/headers). Check availability
chỉ xét response status, không chứng minh backend/nghiệp vụ. Redirect tới login
và HTTP200 chỉ chứng minh route/login reachable. Expected403 cần được owner duyệt
và đổi assurance thành access-policy/reachability, không coi là app health.
Backend readiness của CR7/Card Credit/Fund/Auth giữ endpoint thật đã có; không
khôi phục Prometheus/namespace monitoring, không đoán health URL.

### Mapping trạng thái và debounce

| Quan sát | Statuspage raw status | Lý do |
|---|---|---|
| URL đúng expected status; Node Ready=True, pressure=False, heartbeat mới | operational | URL availability hoặc ready_no_pressure, phạm vi check được nêu |
| URL lỗi/TLS/timeout/status sai; Node Ready=False | major_outage | HTTP/TLS/deadline hoặc not_ready |
| Có check thành công và backend/URL check thất bại | partial_outage | Partial check failure |
| Ready Unknown/thiếu; pressure True/Unknown/thiếu; NetworkUnavailable True | degraded_performance | Condition tương ứng |
| Ingress/Node mất, API lỗi, snapshot >180giây, Node heartbeat >600giây hoặc tương lai >30giây | degraded_performance | missing/source_unavailable/stale, không healthy |
| Cordon nhưng Ready/pressure/heartbeat tốt | operational | cordoned được hiện riêng, không phải node hỏng |
| Retired có lý do được duyệt | Không ghi health nữa | Giữ history, không biến mất thành healthy |

Heartbeat của Node lấy `lastHeartbeatTime` từ Ready và ba pressure conditions,
ngưỡng600giây vì Node status có thể cập nhật khoảng5phút; không cấp quyền Lease.
Quan sát LIST Node mới có thể chứa heartbeat cũ và vẫn bị degrade. Controller
Ready Unknown/False được đọc trực tiếp, không thay bằng `/readyz`/Longhorn.
Admin đọc thêm sync `/observations` nội bộ để hiện raw/final status, debounce,
check results, lý do readiness/content failure và manual incident/maintenance.
Endpoint này không có public Ingress; sync Service cũng phục vụ pod chưa Ready
để trạng thái lỗi vẫn có thể đọc; admin egress8080 và sync ingress8080 giới hạn
pod admin. Telemetry quá180giây hiển thị stale, không suy đoán final status mới.

Nguồn Ingress/Node lỗi được tách riêng; phần còn đọc được vẫn có inventory.

Ba quan sát lỗi riêng mới đổi status; hai healthy riêng mới phục hồi. Đọc lại
cùng timestamp không tăng failure/recovery counters. Local cycle phát hiện
source unavailable/stale là một quan sát unknown riêng; debounce vẫn áp dụng.
Startup state unknown, incident/maintenance (kể cả page-wide) thắng và reset
counters; kết thúc incident cần đủ sample mới. Dữ liệu stale/malformed/unavailable
làm heartbeat sync503 để watchdog nhận mất assurance. URL outage có dữ liệu hợp
lệ không tự làm heartbeat fail. GitHub watchdog vẫn chỉ sở hữu Internet/Monitoring.

### Provision/adopt component, capacity và triển khai

Page live có9components +3groups tại snapshot2026-10-06. Management GET page
không có trường plan/limit. Tài liệu [Atlassian component](https://support.atlassian.com/statuspage/docs/create-a-component/)
ghi ceiling1100, [pricing](https://www.atlassian.com/software/statuspage/pricing)
ghi Free public25; không suy đoán page hiện dùng Free. API có pacing1request/giây/
token; client pacing1,5giây/request và backoff420/429/5xx, pagination100/page có
bounded limit. Admin/sync cùng key có thể gặp429; không hứa global pacing giữa pod.

Operator xác minh actual plan trên Billing, ghi `component_limit` và
`component_limit_evidence` (gói, thời điểm, nguồn). Provisioner đếm bảo thủ cả
group và component;9+3hiện tại cùng4record mới=16slot, cần capacity ít nhất16
trừ khi đã xác minh quota không tính group. Không tạo/xóa để tự giải phóng quota.
Jenkins và ba node là4mapping pending ID. Các endpoint private khác không duyệt.

Quy trình dưới đây chỉ chạy sau khi người dùng cho phép external write/deploy:

1. Review policy/capacity, snapshot collector mới (JSON version1), component ID
   có sẵn. Có thể thu snapshot bằng local collector code với Ingress/Node GET
   của operator trước deploy; không cần Statuspage key cho bước discovery.
2. Chạy preview read-only:

   ```bash
   STATUSPAGE_API_KEY_FILE=/private/path/key python3 statuspage-sync/provision_discovery.py \
     --config statuspage-sync/config.json --policy collector/policy.json \
     --snapshot /private/path/fresh-inventory.json
   ```

3. Sau khi duyệt action list, thêm `--apply --output /private/path/mapped-policy.json`.
   Chỉ tạo/adopt record được duyệt, initial degraded để không xanh khi bootstrap;
   không sửa metadata đã có, group, incident, notification hoặc xóa component.
   POST không retry khi kết quả mơ hồ; chạy GET/preview để tìm marker identity
   trước thử lại. Registry output ghi atomic sau từng ID trả về; copy/merge mapping
   về policy Git để giữ identity. Có component cùng tên nhưng thiếu marker/ID:
   dừng để operator gắn ID thật; không recreate. Giữ marker khi mapping chưa vào
   Git. Component có ID vẫn dùng ID dù admin đổi tên/mô tả.
4. Render/review production/staging; staging writer replicas0. Deploy GitOps sau
   khi được phép, kiểm tra collector RBAC/NetworkPolicy/DNS trong pod, inventory,
   Nodeconditions, debounce, admin SSO và heartbeat. Không thử outage production.
   Bootstrap/AppProject live đã allow repo/ClusterRole/ClusterRoleBinding, không
   cần đổi root repo cho collector. Root checkout hiện có drift cũ, không sửa trong
   patch này; review source allowlist trước rollout nếu dùng checkout đó.

Legacy `provision.py --apply` bị chặn khi discovery active vì seed cũ có thể
reset metadata/group. Admin sửa metadata như trước; sync chỉ PATCH status/metrics.
Statuspage component API không có per-component URL/icon field; provision lưu
URL/logo trong description và admin hiện tham chiếu. Mô tả hiện bằng tooltip `?`,
không hứa clickable directory hoặc icon trên public page. Custom HTML trên
`*.statuspage.io` không được hiển thị theo [Atlassian docs](https://support.atlassian.com/statuspage/docs/use-custom-html-on-a-status-page/);
nếu cần click-to-open/icon public cần quyết định UI/domain riêng sau task này.

### URL public và rule hiển thị mặc định

Các component hiện hữu có URL public được bổ sung vào `description`, hiển thị ở
tooltip `?`: Website, CR7 All Goals, Card Credit, Fund Management, Authentication
(URL OIDC discovery), External Service Access (ba URL được kiểm tra) và Status
Monitoring (heartbeat). Đây là metadata do admin quản lý; seed description trong
config không phải desired state và sync không ghi đè mô tả. Khi cập nhật, đọc mô
tả hiện tại, chỉ thêm URL đã duyệt, giữ nguyên nội dung cũ và ghi audit. Không đưa
URL readiness nội bộ của Kubernetes/Longhorn hoặc backend vào trang public.

Statuspage [tự tổng hợp trạng thái chung](https://support.atlassian.com/statuspage/docs/top-level-status-and-incident-impact-calculations/)
từ status component. Với nhiều component: tất cả operational → All Systems
Operational; có major_outage trong khi các component khác operational → Partial
System Outage; có partial_outage trong khi các component khác operational → Minor
Service Outage. Statuspage [không tự ping URL](https://support.atlassian.com/statuspage/docs/know-when-to-automate-your-status-page/).
HTTP 200, TLS, redirect, readiness và debounce 3 mẫu lỗi/2 mẫu phục hồi là rule của
collector/sync; việc sửa description không thay đổi chúng.

[Uptime history](https://support.atlassian.com/statuspage/docs/display-historical-uptime-of-components/)
tính từ thời gian component ở major/partial outage; partial được tính trọng số
0,3. Cột xám là ngày chưa có dữ liệu. Trạng thái hiện tại operational không xóa
lịch sử outage hoặc làm phần trăm 90 ngày lập tức thành100%.

### Đọc tình trạng cụm trên trang public

API/etcd, Longhorn và ba Kubernetes node có component riêng, không nằm trong
group thu gọn, để xem trực tiếp trạng thái từng phần mà không mở một hàng
Platform chung. Tên node giữ hostname và vai trò. Nhóm ứng dụng, SSO/CI và giám
sát ngoài cụm ghi rõ phạm vi; URL/hostname ứng dụng có trong tên và mô tả.
Trang chính ưu tiên danh sách trạng thái hiện tại, tắt `showcase` để biểu đồ
90ngày không chiếm màn hình. Đây chỉ là lựa chọn hiển thị, không xóa component,
start date hoặc lịch sử đã ghi; admin có thể bật lại lịch sử khi cần.

`Operational` của node nghĩa là Ready=True, không pressure/network-unavailable
và nguồn/heartbeat còn mới; không khẳng định workload trên node đều healthy.
API/etcd dùng `/readyz`; Longhorn kiểm tra volume và storage node như phần trên.
Ứng dụng có URL availability và readiness đã cấu hình. Jenkins dùng HTTPS
`/login`, là path readiness/liveness thực tế của StatefulSet đã đọc từ cluster;
HTTP200 không chứng minh login có xác thực, agents hoặc job/build. Root403 được
ghi rõ là anonymous bị hạn chế, không dùng logo làm health.

Nội bộ thu thập khoảng60giây, báo lỗi sau3mẫu mới và phục hồi sau2mẫu mới;
collector quá180giây hoặc Ready heartbeat quá600giây không được xanh giả.
Giám sát từ GitHub chạy khoảng30phút và có thể trễ; không gọi nó là kiểm tra
liên tục. Tên/mô tả/nhóm/headline tiếp tục thuộc admin và sync không ghi đè.
Admin cho sửa `headline`/`page_description` qua cùng SSO, CSRF và audit; không
mở custom HTML, status write hoặc API proxy. API không công bố setting mặc định
expand group/reorder, nên không dựa vào trường API chưa được hỗ trợ để ẩn/hiện
dữ liệu cụm.

Policy đặt `component_limit=25` là trần tạo mới bảo thủ, không phải tên gói:
[Free public25](https://support.atlassian.com/statuspage/docs/what-do-i-get-on-a-free-plan/),
[Atlassian xác nhận trial25/paid1100](https://community.atlassian.com/forums/Statuspage-questions/Are-their-limits-on-the-number-of-components-in-the-non-Free/qaq-p/1555695).
Đếm cả group nên12object hiện hữu +4component được duyệt =16 vẫn nằm trong trần.
GET page không có field billing/tier; chưa xác minh tên gói thực tế. Không nâng
trần lên1100 khi chưa có bằng chứng account-specific. Mọi tạo mới vẫn là operator
preview/apply, không tự tạo từ collector hoặc sync.

### Giao diện cho bước ticket tiếp theo

Chưa kết nối ticket service. Nguồn đề xuất là transition **sau debounce và sau
Statuspage PATCH thành công**, không phải raw HTTP failure. Event contract v1:
`event_id`, `inventory_key`, `component_id`, `source`, `observed_at`,
`previous_status`, `status`, `reason`, `check_type`, `assurance`, `writer=sync`,
`incident_precedence`, `verification_reference`. Consumer sau này cần durable
outbox/idempotency và correlation theo inventory key + episode; stdout hiện tại
chỉ là observation log, chưa là delivery contract đảm bảo.

Luồng: `health_failure` -> adapter ticket/incident của hệ thống do owner chọn
-> `investigating` -> `fix_deployed` (revision/deploy reference) -> `verification`
(check thật, source mới, đủ debounce) -> `recovered`. Ticket không tự resolved
chỉ vì deploy xong hoặc logo/login trả200. Active incident/maintenance giữ quyền
quyết định public status; ticket integration không được thêm writer health thứ hai.
