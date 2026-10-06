# DRG DevLab Statuspage Platform

- Public status: https://drgdevlab.statuspage.io
- Website: https://drgdevlab.com/
- Internal administration: https://status-admin.apps.drgdevlab.com
- Kubernetes namespace: `statuspage`
- GitOps Application: `argocd/statuspage-platform`

Repo chứa toàn bộ mã đồng bộ, web quản trị, Kubernetes/Argo CD manifests, CI,
external watchdog và hướng dẫn vận hành. Không chứa API key hay mật khẩu.

## Kiến trúc

```mermaid
flowchart LR
  Operator[Quản trị viên] --> OAuth[Keycloak + OAuth2 Proxy]
  OAuth --> Admin[Web quản trị nội bộ]
  Admin --> API[Atlassian Statuspage API]
  Probes[HTTP readiness + Prometheus] --> Sync[Bộ đồng bộ trong namespace statuspage]
  Sync --> API
  GH[GitHub Actions ngoài cụm] --> API
  API --> Public[drgdevlab.statuspage.io]
  Admin --> Audit[(SQLite audit / Longhorn PVC)]
  Git[Repo này] --> Argo[Argo CD] --> Sync
  Argo --> Admin
```

| Phần | Nguồn quản lý | Cách cập nhật |
|---|---|---|
| Tên trang, link website, tên/mô tả/nhóm component, biểu đồ uptime component | Statuspage | Web quản trị nội bộ |
| Trạng thái sức khỏe và hai metric tổng hợp | Check thực tế | Sync mỗi 60 giây; lỗi 3 vòng, phục hồi 2 vòng |
| Trạng thái truy cập Internet và độ mới của sync | GitHub-hosted runner | Phút 17 và 47; có thể trễ |
| Định nghĩa check, ID component, deployment | Git | Pull request và Argo CD |
| Incident/maintenance, subscriber và notification | Atlassian | Link trực tiếp từ web quản trị |

Kubernetes Control Plane kiểm tra trực tiếp API `/readyz` với TLS CA của cluster;
Persistent Storage đọc health của volume và storage node từ Longhorn API.
Hai check này không phụ thuộc Prometheus. Check control plane không đo readiness
của từng worker; xem [runbook](docs/OPERATIONS.md) để biết phạm vi kiểm tra.

`showcase` điều khiển lịch sử uptime của component; không ẩn component khỏi public
page. Web nội bộ không tự ý xóa component đang được monitor, sửa check URL hoặc
thay trạng thái sức khỏe. Các thay đổi giao diện không bị sync ghi đè.

## Sử dụng

1. Mở trang quản trị, đăng nhập Keycloak realm `kubernetes` bằng tài khoản thuộc
   nhóm `/k8s-cluster-admins`.
2. Chỉnh thông tin trang hoặc component, kiểm tra trước khi bấm xuất bản.
3. Mở public page để xem kết quả. Lịch sử thao tác nằm trong trang quản trị.
4. Tạo incident và maintenance trong Atlassian theo link trong giao diện;
   quyết định notification tại đó. Sync tôn trọng incident/maintenance đang mở.

## Cấu trúc

- `statuspage-sync/`: Python stdlib check engine, cấu hình check, explicit provisioner.
- `admin/`: Python stdlib API, HTML/CSS/JS, OAuth2 Proxy sidecar và PVC.
- `deploy/`: production Kustomize; `deploy/staging/` tắt writer và heartbeat để chuyển cụm an toàn.
- `argocd/`: Application bootstrap chuẩn.
- `scripts/`: bootstrap Keycloak client/Secret và deploy key Argo.
- `.github/workflows/`: tests và watchdog ngoài cụm.
- `docs/OPERATIONS.md`: thao tác vận hành, backup, quyền, khôi phục.
- `docs/IMPLEMENTATION.md`: trình tự thực hiện và bằng chứng nghiệm thu.
- `docs/statuspage/`: kế hoạch và bằng chứng giai đoạn thay portal ban đầu.

## Kiểm tra local

```bash
kubectl kustomize deploy > /tmp/statuspage.yaml
kubectl apply --dry-run=server -f /tmp/statuspage.yaml
```

Bộ test có sẵn trong `tests/`; chỉ chạy khi người dùng yêu cầu. CI tự động render manifests; unit tests là tùy chọn workflow_dispatch.

Xem [runbook](docs/OPERATIONS.md) để bootstrap môi trường. Không chạy lại
`provision.py --apply` như một bước deploy thường xuyên: đây là công cụ seed ban
đầu, có thể ghi đè tên/mô tả/nhóm đã sửa trong web quản trị.
