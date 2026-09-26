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
Lượt đầu CLI trả SUCCESS nhưng `denied_actions=RunCommand`, stderr cho biết shell
bị headless từ chối và chưa có file. Không coi lượt đó là hoàn thành; giao lại chỉ
bằng file tools rồi coordinator kiểm thử actual implementation.

## Bằng chứng nghiệm thu

Được cập nhật sau khi rollout và kiểm tra live hoàn tất. Không dùng trạng thái CI
hoặc manifest đơn lẻ để thay cho kiểm tra SSO, API write và runtime.

## Tài liệu tham khảo

- [Statuspage API](https://developer.statuspage.io/): `page.url` là link website;
  component `showcase` là thuộc tính showcase/uptime, không dùng để hứa ẩn component.
- [OAuth2 Proxy configuration](https://oauth2-proxy.github.io/oauth2-proxy/configuration/overview/)
- [Keycloak OIDC provider](https://github.com/oauth2-proxy/oauth2-proxy/blob/master/docs/docs/configuration/providers/keycloak_oidc.md)
