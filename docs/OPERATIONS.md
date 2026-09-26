# Vận hành Statuspage Platform

## GitOps và bootstrap

Namespace `statuspage` giữ sync, admin, API Secret, OAuth Secret và audit PVC.
NetworkPolicy `allow-statuspage-probes` ở bốn namespace ứng dụng chỉ cho pod
`app=statuspage-sync` trong namespace `statuspage` gọi TCP readiness port tương ứng.
Không cấp Kubernetes ServiceAccount token hoặc RBAC cho workload.

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
bind loopback8081 trong cùng pod. NetworkPolicy chỉ nhận từ ingress-nginx.
Đây là trang giới hạn bằng SSO, không phải endpoint chỉ có IP private/VPN.

API chỉ cho sửa metadata được phép của đúng page `hsyg2r4ql1pr`. Không có generic
API proxy. PATCH yêu cầu Origin đúng, JSON và token CSRF gắn với identity có hạn.
Credential chỉ ở phía server. Các request không log cookie/token/API key.
Public ingress heartbeat chỉ expose chính xác `/healthz`; `/metrics` không public.

## Sửa check hoặc thêm dịch vụ

Check definition tại `statuspage-sync/config.json` dùng component ID ổn định.
Khi thêm dịch vụ: tạo component ở Atlassian, lấy ID; thêm check và group metadata
vào config; bổ sung egress/probe policy nếu cần; PR + test + review rồi merge.
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

Prometheus scrape `statuspage-sync.statuspage.svc.cluster.local:8080/metrics`.
Counters: `statuspage_sync_cycles_total`, `statuspage_sync_cycles_failed_total`,
`statuspage_sync_last_success_timestamp_seconds`. Nếu heartbeat stale >180 giây,
watchdog ngoài cụm có thể đánh dấu Status Monitoring lỗi. Workflow lịch 30 phút
có thể bị GitHub trì hoãn; theo dõi run history và Actions quota. Nó không phải SLA.
Lỗi API/monitoring không được xem là healthy. Khi cụm down, public page vẫn ở
Atlassian, nhưng reading nội bộ có thể stale. Quản trị Atlassian vẫn dùng được.

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
