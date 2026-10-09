# ADR-0008: 显式行归属与默认拒绝的 RLS

- Status: Accepted
- Date: 2026-10-09
- Affects: app/search RLS，迁移 0009，request session 事务范围

## 原因

S0 真实 PostgreSQL 审查发现旧 `tenant_filter_check()` 不接收行 tenant_id，函数内未绑定变量触发异常后返回 true；缺少 session 范围同样返回 true。只设置 session 参数或运行 superuser 测试不能证明 RLS 生效。

## 决策

新函数显式接收行 tenant UUID；缺失、空或非法 session 范围返回 false。各 tenant-scoped app/search policy 用 `tenant_filter_check(tenant_id)`，搜索表同样开启 RLS。函数使用 invoker 权限与固定 pg_catalog search_path，不通过异常放宽权限。

请求 session 在事务开始事件里 SET LOCAL。业务 service 保持唯一 begin/commit owner，范围不能通过依赖的提前查询导致 autobegin 冲突；连接复用/回滚后每个新事务重设范围。

平台/worker 的跨租户能力必须用显式特权流程与数据库角色。不能缺 tenant 就获得全局权限；本轮本地迁移角色为专用临时库 postgres，不能充当生产角色验收。

## 代价与回滚

已有依赖缺失范围仍可读的消费者将被拒绝，必须修复可信身份链，不能保留 unsafe fallback。迁移可回到前序 schema，但 downgrade 保留等价的直接 tenant 比较策略，绝不恢复旧 fail-open policy。这意味着安全修复的回滚保留默认拒绝语义。原数据不删除。

非 owner、非 superuser 角色的跨租户/缺范围/错范围验证是最低证据；应用检查仍负责 KB ACL 和同租户 user owner，RLS 不代替这两项。
