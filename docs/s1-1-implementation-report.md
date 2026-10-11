# S1.1 Gateway Hardening 实现报告

状态：实现与本地验证完成，待 Codex Reviewer 审阅。未 commit、未 push、未切换分支、未启动服务、未调用真实模型、未读取凭据、未迁移数据。

## 1. 目标与结论

目标：租户与 use_case 授权通过后，若 provider 不可用，必须在 key acquisition 之前拒绝；错误不得泄露 provider 或 key 身份、秘密、内部 URL。

结论：

- `ModelRouter.resolve` 的顺序为：ProfileResolver 授权 → `KeyPoolManager.admit_provider`（只读 Provider 配置）→ `acquire_key` → `build_provider`。
- 缺失、inactive、未接通 provider 类型、未显式 opt-in 的 stub，均返回同一个通用 `ProviderError`（502，`message="Model provider unavailable"`，`details={}`）。
- `acquire_key` 的错误不再携带 `provider_id`，内部的 Provider 读取失败也不再回显 UUID。
- 显式 test opt-in 的 stub 可进入 acquire 并构造真实 `StubProvider`，但这只证明网关路径可达，不代表真实模型调用成功。
- 正式 provider（openai / anthropic 等）仍未接通，继续拒绝。

## 2. Changed Files

本任务新增的改动（叠加在工作区既有未提交 WIP 之上）：

| 文件 | 改动 |
|---|---|
| `packages/model-gateway/src/ragos/model_gateway/key_pool.py` | 新增 `admit_provider`、`_is_connected`、`_require_provider`；`acquire_key` 先取 Provider 再查 key；去掉 `provider_id` details；`build_provider` 复用 `_is_connected` 并使用通用文案 |
| `packages/model-gateway/src/ragos/model_gateway/router.py` | `ModelRouter.__init__` 增加 keyword-only `allow_test_stub: bool = False` 并透传；`resolve` 在 acquire 前调用 `admit_provider`；docstring 更新顺序 |
| `tests/test_provider_admission.py` | 新增 13 个用例（真实链路 + 本地 ASGI 错误映射） |
| `docs/s1-1-implementation-report.md` | 本报告 |
| `docs/validation/s1-1-gateway-hardening/` | 基线与改后测试输出、行为计数脚本与输出、静态检查、源码摘要 |

未修改：`profiles.py`、`docs/architecture.md`、`retrieval`、`ingestion`、`embedding`、`database`、前端、`uv.lock`、`pyproject.toml`、模型配置、API 认证。已有测试 `tests/test_model_gateway.py`、`tests/test_provider_capability.py` 未改动，其既有断言在改后仍通过。

注意：`router.py`、`key_pool.py`、`profiles.py` 在本任务开始前已有未提交改动（`allow_test_stub`、`build_provider` 拒绝、ProfileResolver 授权收紧等）。这些改动被保留，本任务只在其上增加准入逻辑。该 WIP 本身仍需随 S1 整体审阅。

## 3. Design Reason

- **准入放在 router 而非 `acquire_key` 内部**：Case A 的验收要求 `acquire_key` 调用次数为 0；把准入放在 router 的授权与 acquire 之间，满足该约束，也保证授权负例不会触发任何 Provider 或 key 读取。
- **准入由 `KeyPoolManager` 持有**：`allow_test_stub` 已在 KeyPoolManager 上，准入与 `build_provider` 共用 `_is_connected`，避免两套 provider 策略。没有新增 SPI 或平行 adapter 层。
- **准入只读 Provider 行**：使用现有 `PlatformRepository.get_by_id`，检查的字段是 `Provider.is_active` 与 `provider_type`。Provider 模型没有其他 active 语义，不发明新字段。KeyPool 的 `is_active` 只在 acquire 阶段使用。
- **通用错误**：准入失败的所有分支使用同一文案且无 details，调用方无法区分“禁用”“未接通”“不存在”“stub 未 opt-in”，避免枚举 provider 状态。
- **`_require_provider` 替换 acquire 中的 `require_by_id` 直接调用**：原实现在 round-robin 更新之后才读 Provider，且 NotFound 会带 `id` 进入响应。现改为先读、失败转为通用 ProviderError，并用 `from None` 切断异常链，避免 traceback 携带 UUID。
- **不改变 round_robin 语义**：只有在 Provider 存在且有 active key 时才推进 round_robin。
- **保留 `build_provider` 防御性拒绝**：即使绕过 router 直接调用，正式 provider 仍被拒绝。
- **不删除既有 fallback 或预算逻辑**：本任务范围外。

## 4. Tests Added

`tests/test_provider_admission.py`（13 项）：

- Case A（参数化 4 项：`disabled`、`unconnected_type`、`stub_without_opt_in`、`missing_provider`）：真实 ProfileResolver/ModelRouter/KeyPoolManager；断言错误为 502、文案通用、`details == {}`；`acquire_key` 调用 0 次；key_pool SELECT 0 次；provider SELECT 1 次；round_robin 不变；错误内容不含 provider UUID、key UUID、URL、秘密。
- Case B（1 项）：授权且 provider 为显式 test stub 但无 active key；错误为 502、文案为 `No active keys available for provider`，`details` 为空；key_pool SELECT 1 次；provider SELECT 2 次（准入一次、acquire 一次）；错误内容不含 provider UUID、key UUID、URL、active 或 inactive 秘密。
- Case C（1 项）：显式 test opt-in 的 active stub 且 active key；`acquire_key` 调用 1 次；构造的是真实 `StubProvider`，其 `api_key` 为占位符而非存储的秘密；stub 的 `chat` 返回空内容（这是 stub 行为，不是模型成功）。
- 授权负例（参数化 4 项：`unbound`、`override`、`foreign_owner`、`wrong_use_case`）：在 `allow_test_stub=True` 下仍在 provider 与 key 读取之前拒绝；`acquire_key` 0 次，provider SELECT 0，key_pool SELECT 0。
- 本地 ASGI 映射（3 项）：标准 FastAPI 应用加 `register_error_handlers`，使用真实 `get_auth_claims`、`get_tenant_ctx`、`get_db_session` 依赖覆盖和真实 ModelRouter。
  - 无身份 → 401 `AUTHENTICATION_REQUIRED`，无 acquire。
  - 越权 override → 403 `PERMISSION_DENIED`，响应不含 profile UUID，无 provider 与 key 读取。
  - 授权但 provider inactive → 502 `PROVIDER_ERROR`，`details` 为空，内容脱敏。

测试基础设施说明：

- 数据库为临时 in-memory SQLite，表定义来自真实 SQLModel 类（`Tenant`、`Provider`、`KeyPool`、`ModelProfile`、`TenantModelBinding`）。
- 为 SQLite 做的适配仅作用于元数据副本：删除 PG 专属的 `server_default`，并通过编译钩子把 `JSONB` 渲染为 `JSON`。原模型类不被修改。
- 观测方式：`engine` 上的 `before_cursor_execute` 事件统计语句；`KeyPoolManager.acquire_key` 通过 `monkeypatch` 包装（调用原函数，不替换）。
- 为避免假阴性，已确认 SQLite 查询带 `main.` 前缀，并将模式修正为 `(?:main\.)?`；B 组断言中的 provider 读取次数据此实际测得。
- 对变异做了验证：临时删除 `admit_provider` 调用（仅在快照副本中）后，A 组 4 项、B 组 1 项、API 502 1 项共 6 项失败，证明测试能捕获回归。已用干净快照恢复安装。

## 5. Test Result

所有结果均为本轮在隔离环境中的实际运行输出：

- 改动前针对性基线（11 个文件）：`151 passed`，输出见 `docs/validation/s1-1-gateway-hardening/baseline-pytest.txt`。
- 改动后针对性回归（上述 11 个文件 + 新文件）：`164 passed`（151 + 13），见 `after-targeted-pytest.txt`。
- 改动后完整套件：`791 passed, 21 skipped`，见 `after-full-pytest.txt`。
  - 21 个 skip 全部来自 `tests/test_index_integration.py` 与 `tests/test_storage_integration.py`，原因是 `RAGOS_TEST_PG_DSN` 未设置（本环境未连接持久 PG，符合约束）。
  - 本轮未运行改动前的完整套件基线，因此不声称“完整套件基线与改后相同”；仅能确认改后无失败。
- `ruff check`（router.py、key_pool.py、测试文件、run_counts.py）：`All checks passed!`。
- `ruff format --check`：3 个文件已格式化。
- `git diff --check`：exit 0。
- 行为计数（`behavior-counts.txt`，由 `run_counts.py` 生成，使用真实链路）：

| 场景 | 结果 | acquire_key 调用 | key_pool SELECT（key 读取） | 备注 |
|---|---|---|---|---|
| A disabled | 502 通用 | 0 | 0 | round_robin 不变 |
| A unconnected type | 502 通用 | 0 | 0 | round_robin 不变 |
| A stub without opt-in | 502 通用 | 0 | 0 | round_robin 不变 |
| A missing provider | 502 通用 | 0 | 0 | round_robin 不变 |
| B admitted, no active key | 502 `No active keys…` | 1 | 1 | 无 details，无 UUID/URL/秘密 |
| C explicit test stub | 进入 `StubProvider` | 1 | 1 | round_robin 前进（正常 acquire） |

A 组结论：`acquire_key` 调用次数 0，key 读取次数 0。
B 组结论：错误脱敏，key 查询发生（符合允许），不回显任何标识或秘密。
C 组结论：`acquire_key` 调用次数 1，构造真实 provider 类。这是 stub 路径，不是真实模型成功。

## 6. 可追溯证据

`docs/validation/s1-1-gateway-hardening/`：

- `baseline-pytest.txt`：改动前针对性基线（151 passed）。
- `after-targeted-pytest.txt`：改后针对性回归（164 passed）。
- `after-full-pytest.txt`：改后完整套件（791 passed, 21 skipped）。
- `behavior-counts.txt` 与 `run_counts.py`：行为计数。
- `static-checks.txt`：`git diff --check`、ruff check、ruff format。
- `source-digest.txt`：源码 sha256、安装产物 sha256、快照树摘要、requirements 摘要、锁文件未变证明。

源码摘要（工作区当前状态）：

- `router.py` `244dc7fd4973c9ef162ec36958bced052f3c51d670dd53208813dd58e37b3a3d`
- `key_pool.py` `a0afade5edad2a0be07a9e4c35e3d7afc509871f3a2a9a1750388bb30a689c5c`
- `tests/test_provider_admission.py` `6e94f95f77ad862c1aef7cc5674961c1a458a4a312fc1ca6f9f342efb71b516c`
- 安装的 `site-packages/ragos/model_gateway/{router,key_pool}.py` 与上述两个源文件哈希完全一致。

## 7. 环境与命令

- 冻结依赖：`uv export --frozen --extra dev --no-hashes --no-emit-project --all-packages -o /tmp/opencode/s1-1/requirements.lock.txt`（只读 uv.lock，未修改）。
- 临时 venv：`uv venv /tmp/opencode/s1-1/venv --python 3.11`，Python 3.11.17。
- 依赖安装：`uv pip install -r /tmp/opencode/s1-1/requirements.lock.txt`。
- 源码快照：`git ls-files -co --exclude-standard` 打包到 `/tmp/opencode/s1-1/snap`，不写入仓库。
- 项目安装：`uv pip install --no-deps --no-editable /tmp/opencode/s1-1/snap`（非 editable，从安装产物导入，未使用 PYTHONPATH）。每次源码变更后通过 `/tmp/opencode/s1-1/reinstall.sh` 重新快照和重装。
- 测试命令：`/tmp/opencode/s1-1/venv/bin/python -m pytest -q -p no:cacheprovider …`，在仓库根目录运行。
- 运行环境未启动任何服务，未连接 PG、Redis、Qdrant，未调用真实模型，未读取真实凭据（测试秘密均为占位字符串）。

## 8. Git 状态（前后对比）

开始前（`git status --short`，HEAD `f0914adf7d97dc0271f80ba3e1db79dccdd44f99`，分支 `main`）：

```
 M docs/architecture.md
 M docs/project-progress.md
 M packages/model-gateway/src/ragos/model_gateway/key_pool.py
 M packages/model-gateway/src/ragos/model_gateway/profiles.py
 M packages/model-gateway/src/ragos/model_gateway/router.py
 M packages/retrieval/src/ragos/retrieval/gateway.py
?? docs/opencode-s1-task.md
?? docs/validation/s1-model-authorization-2026-10-10/
?? tests/test_gateway_evidence_budget.py
?? tests/test_model_profile_authorization.py
?? tests/test_provider_capability.py
```

结束时新增的仅为本任务文件：

```
?? docs/s1-1-implementation-report.md
?? docs/validation/s1-1-gateway-hardening/
?? tests/test_provider_admission.py
```

既有文件的修改状态未变（`key_pool.py`、`router.py` 继续为 M，其中包含本任务在既有 WIP 之上的增量）。HEAD 未变，无 commit，无分支切换，无 push/merge。

## 9. Remaining Risks

- **SQLite 替代 PG**：准入链路在 SQLite 上验证。PG 的 RLS、JSONB 语义和 schema 行为未在本任务中验证。
- **API 映射不是生产路径**：401/403/502 通过本地 ASGI 应用验证。生产 `RequestContextMiddleware` 的成员关系解析需要 PG，因此未运行 `create_app` 完整路径；测试中的 identity 中间件是测试替身，只复用了真实的依赖与异常映射。
- **既有 WIP 未审阅**：`profiles.py` 的授权收紧、`router.py`/`key_pool.py` 的 `build_provider` 拒绝等未提交改动是本任务的前置条件，它们与本任务的边界需要 Reviewer 一并确认。
- **`acquire_key` 仍会把加密 key 放入 `KeyLease.provider_kwargs`**：这是既有行为。stub 路径不使用它，但真实 adapter 接入时必须改为按别名解析、最小范围注入。本任务未改。
- **`admit_provider` 检查的是 Provider 行**：provider 级别的预算、秘密解析、出站策略均未在本任务中实现，报告和 retrieval 的注释已说明这一点。
- **stub 路径的 round_robin 仍会推进**：仅限显式 test opt-in，生产 `ModelRouter(session)` 默认 `allow_test_stub=False`，不会进入。
- **21 个 PG 集成测试未运行**：需要 `RAGOS_TEST_PG_DSN` 指向一次性本地 PG，本任务未连接。
- **未运行改动前完整套件**：仅有针对性基线（151）与改后完整套件（791 passed, 21 skipped）。

## 10. Rollback

- 回滚 `router.py` 中的 `admit_provider` 调用与 `allow_test_stub` 透传，以及 `key_pool.py` 中的准入、`_require_provider` 与 `acquire_key` 的顺序变化，即可恢复到既有 WIP 状态。
- 测试文件 `tests/test_provider_admission.py` 与证据目录可独立删除，不影响其他测试。
- 无数据库迁移、无持久数据变更，无需数据回滚。

## 11. 建议 Commit Message

`fix: reject unavailable providers before key acquisition`

（未提交，等待 Reviewer 审阅后由用户决定。）

## 12. 未执行事项

- 未执行 S1.2。
- 未运行 Codex Review（按要求由 Codex 独立执行）。
