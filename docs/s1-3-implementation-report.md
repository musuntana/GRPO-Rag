# S1.3 Secret Intake Fail-Closed 实现报告

状态：返工完成（Codex REWORK 之后的原范围修正），等待 Independent Reviewer（Codex）复审。未 commit、未 push、未 merge、未部署、未执行 S1.4 / R1。

## 0. 返工记录（Codex REWORK）

Codex 独立审查裁决 REWORK。复现：本地 FastAPI 探针中 body 类型为 `Annotated[dict[str,int], Body()]`，请求体键为 `S13_ALPHANUM_SECRET_SENTINEL`、值非法，422 响应 `field` 为 `body.S13_ALPHANUM_SECRET_SENTINEL`，即动态 dict 键被回显。

- **根因**：`errors.py` 中 `_FIELD_NAME` 正则只检查字符形状，不是可信字段名 allowlist；用户动态键只要形如标识符即可原样进入 `field`。
- **RED（返工前产物，先写负例）**：`docs/validation/s1-3-secret-intake/rework-red-run.txt`，命令同 §5，结果 `11 failed, 39 passed`，退出码 1。失败项：动态 dict 键回显、`query`/`header`/`cookie`/未知来源动态段回显、不可哈希 loc 与 dict loc 导致 500、OpenAPI 仍广告 201。
- **最小修复**：
  - `errors.py`：字段路径改为路由声明路径的最长前缀。声明路径来自 `route.dependant`（query/path/header/cookie 的 alias）与 endpoint 类型注解中的 BaseModel 字段名/alias（body）。非声明段、非字符串段、未知来源、动态 dict 键、union tag、额外字段一律截断。不可哈希段在类型检查后处理，不再进入 frozenset。无 route 或注解解析失败时退化为来源级字段（fail-closed）。不硬编码全仓库字段，不建通用 schema 框架。
  - `routes/model_center.py`：`create_key_pool` 装饰器改为 `status_code=422` 并显式 `responses[422]`，描述“Secret intake unavailable: raw API keys are not accepted or stored”；OpenAPI 不再包含 200/201。业务逻辑未改。
- **新增 RED 负例（均在 `tests/test_secret_intake.py`）**：真实 FastAPI 探针（动态 dict body、模型 body 额外动态键、路径参数哨兵）；`RequestValidationError` 对 16 种 loc（动态段、未知来源、不可哈希段、dict 段、union tag、索引段、声明段对照）构造，并让 input/ctx/msg 含纯字母数字哨兵，断言 response 与 caplog 无值、无 500；混合 loc 的序列化；OpenAPI 断言。
- **GREEN 结果**：
  - 定向（`tests/test_secret_intake.py tests/test_model_center_route.py`）：`50 passed`，`docs/validation/s1-3-secret-intake/rework-green-targeted.txt`。
  - S1.1 相关回归（provider admission/capability、model gateway、gateway evidence budget、model profile authorization、model bindings route）：`53 passed`，`rework-s1-1-regression.txt`。
  - 全量：`832 passed, 21 skipped`（21 个 skip 均为 `RAGOS_TEST_PG_DSN` 未设置），退出码 0，`rework-green-full-pytest.txt`。
  - ruff（5 个相关文件）通过，`rework-ruff.txt`；`git diff --check` 通过。
- **前一版 811 不作为最终候选 PASS**。最终候选为本轮 `832 passed, 21 skipped`。
- **已知残余事实（如实记录，不擦除框架内部对象）**：FastAPI 内部 `RequestValidationError` 对象在 handler 执行期间仍在内存中，其 `str()`/`errors()` 含原始输入（已在探针中确认）。本项目保证：对外响应、当前日志链（caplog 断言）不含值；不尝试擦除或改写框架 request 对象。


## 1. 基线与 Git 状态

- 基线 HEAD：`f0914adf7d97dc0271f80ba3e1db79dccdd44f99`（分支 `main`）。
- 开始前已有的 S1 / S1.1 WIP 全部保留，未 reset、stash、清理或覆盖。
- 三个目标实现文件开始时无 diff，本任务仅在其上做最小修改。
- 当前 `git status --short` 中与本任务相关的条目：
  - `M apps/api-server/src/ragos_api/errors.py`（本任务）
  - `M apps/api-server/src/ragos_api/routes/model_center.py`（本任务）
  - `M packages/storage/src/ragos/storage/repositories/model_gateway.py`（本任务）
  - `M tests/test_model_center_route.py`（本任务）
  - `?? tests/test_secret_intake.py`（本任务新增）
  - `?? docs/s1-3-implementation-report.md`、`?? docs/validation/s1-3-secret-intake/`（本任务证据）
  - 其余 `M`/`??`（`docs/architecture.md`、`docs/project-progress.md`、model-gateway 三文件、retrieval gateway、S1.1 测试与报告、`docs/opencode-s1-task.md` 等）均为既有 WIP，本任务未触碰。
- `uv.lock`、`pyproject.toml` 无 diff。

## 2. ChangedFiles

| 文件 | 类型 | 说明 |
|---|---|---|
| `packages/storage/src/ragos/storage/repositories/model_gateway.py` | 实现 | 新增 `reject_raw_credential_intake()`；`KeyPoolRepository.create` 在任何 ORM 构造 / add / flush / commit 前抛 `ValidationError` 422 |
| `apps/api-server/src/ragos_api/routes/model_center.py` | 实现 | `create_key_pool` 在任何写事务、repository 调用前拒绝；移除不再使用的 `session` 依赖 |
| `apps/api-server/src/ragos_api/errors.py` | 实现 | 新增 `RequestValidationError` 统一处理器：仅输出字段定位与稳定错误码，输出既有 `error.code/message/details` 契约 |
| `tests/test_secret_intake.py` | 新增测试 | Repository 直接调用、API 真实 `create_app`、校验错误、日志与异常序列化无秘密 |
| `tests/test_model_center_route.py` | 修改测试 | `test_create_key_pool` 由“期望 201”改为“期望安全 422”；删除不再被调用的假 `create` |
| `docs/s1-3-implementation-report.md` | 文档 | 本报告 |
| `docs/validation/s1-3-secret-intake/` | 证据 | 基线、RED、GREEN、ruff、源码与安装摘要、顺序探针输出 |

未修改：gateway / S1.1 测试、retrieval / embedding / ingestion、schema / migration、frontend、其他路由、budget / egress / provider adapter / index 契约、依赖锁。未迁移其他已有测试。

## 3. RootCause

1. `KeyPoolRepository.create` 把调用方传入的 `api_key` 原样写入 `KeyPool.api_key_encrypted`，没有任何可信加密或密文通道。字段名 `*_encrypted` 不构成可信密文。
2. `POST /platform/model-center/providers/{id}/key-pools` 直接调用该 repository 写库并返回 201，原始凭据可以持久化。
3. FastAPI 默认 `RequestValidationError` 响应为 `{"detail": [...]}`，并附带 `input` 字段，校验失败时会回显提交的值（包括 api_key）；该形态也不符合 `error.code/message/details` 契约。

## 4. MinimalFix

- Repository：`create` 的唯一行为是抛出 `ValidationError`（422，`VALIDATION_ERROR`，`details={"reason": "secret_intake_unavailable"}`），不再构造 ORM 对象、不调用 `add` / `flush` / `refresh`。签名保留，使旧调用方得到明确拒绝而不是静默降级；未提供 `encrypted=`、`trusted=` 等绕过参数。
- Route：认证与授权仍由路由级依赖完成（401 / 403 顺序不变）；已授权且 body 通过 Pydantic 校验的请求，在返回前调用 `reject_raw_credential_intake()`，因此不开启写事务、不构造 repository、不访问数据库会话。
- 校验错误：新增 `RequestValidationError` 处理器。字段定位是路由声明路径的最长前缀（见 §0），动态与未声明段全部丢弃；错误码映射为 `REQUIRED`、`TOO_LONG`、`TOO_SHORT`、`OUT_OF_RANGE`、`MALFORMED_JSON`、`INVALID_TYPE`、`INVALID_VALUE`；不输出 `input`、`ctx`、`msg`、`url`。
- 返工前的 `_FIELD_NAME` 正则已删除，它不是可信 allowlist。
- 不做：Secret Manager、alias、rotation、既有数据清理、provider 调用、budget / egress 变更。

## 5. RED / GREEN

### RED

- 基线（修改前）：`docs/validation/s1-3-secret-intake/baseline-full-pytest.txt`，`791 passed, 21 skipped`，退出码 0。
- RED 命令：`/tmp/opencode/s1-3/venv/bin/python -m pytest -q -p no:cacheprovider tests/test_secret_intake.py tests/test_model_center_route.py -rfE --tb=short`
- RED 结果：`17 failed, 12 passed`，退出码 1。输出见 `docs/validation/s1-3-secret-intake/red-run.txt`。
- 失败原因均为行为性的：
  - Repository 直接调用未抛错（`DID NOT RAISE ValidationError`），4 个样例（原始 sentinel 与 3 个加密外观值）全部进入写入路径。
  - API 授权 + 有效 key 返回 201，而非 422。
  - 校验错误返回 FastAPI 默认 `detail` 形态，不符合 `error` 契约，且回显输入（path、query、tenant_id、畸形 JSON、过长 key 等）。
- 诚实说明：RED 前曾有一次误操作（临时删除假 `create`），使 `test_create_key_pool` 变为 `AttributeError`。该次运行已判定为测试装配问题，已恢复假 `create` 并重跑，上述 RED 结果才是有效证据；无效运行未作为证据保存。
- 12 条通过项：4 条为本任务新增守卫（401、403、请求 ID 回传、无 bypass 参数），修改前即通过，符合预期；其余 8 条为未改动的既有路由测试。

### GREEN

- 定向：`tests/test_secret_intake.py tests/test_model_center_route.py`，`29 passed`，退出码 0。`docs/validation/s1-3-secret-intake/green-targeted.txt`。
- 全量（首轮，已被 §0 返工后的 832 取代）：`811 passed, 21 skipped`，退出码 0。`docs/validation/s1-3-secret-intake/green-full-pytest.txt`。
- 首轮全量相对基线净增 20 条测试（791 → 811）；返工后相对基线净增 41 条（791 → 832）；21 个 skip 均为 `RAGOS_TEST_PG_DSN` 未设置，不计为 PASS。
- ruff：`ruff check` 对 5 个相关文件 `All checks passed!`。
- `git diff --check`：无输出，退出码 0。

## 6. Negative tests（覆盖）

- Repository 直接调用：4 个值（sentinel、`enc:v1:`、`fernet:`、`sk-` 前缀）均抛 422，session 调用为 0，`KeyPool` 构造为 0。
- 拒绝载荷：`str`、`repr`、`to_response_content()`、`json.dumps`、`traceback` 渲染、`details` 均不含 sentinel；`__context__` / `__cause__` 为空。
- 无绕过参数：`create` 签名不含 `encrypted` / `trusted` / `ciphertext` / `api_key_encrypted` / `skip_validation`。
- API 授权 + 有效 key：422，响应只含 `error.code/message/details`，不含 sentinel；observing session 无任何 `begin` / `add` / `flush` / `commit` / `exec` 调用。
- 未认证（401）、tenant_admin（403）：在任何写行为前结束，session 调用为 0。
- 校验错误：缺字段、过长（600+ 字符）、空串、错误类型、多个无效字段（全部列出）、畸形 JSON、path / query / 业务字段中的 sentinel：均返回安全 422，字段定位正确，响应中无 sentinel、无 `input` / `ctx`，session 调用为 0。
- 日志：畸形 JSON 与已拒绝请求的 caplog（DEBUG）不含 sentinel。
- `X-Request-Id`：校验错误响应回传原请求 ID。

## 7. Regression

- 既有 `test_list_key_pools`、`test_create_provider`、`test_create_provider_conflict`、`test_create_profile`、`test_list_profiles` 等路由测试通过。
- S1.1 相关测试（`test_provider_admission.py`、`test_provider_capability.py`、`test_model_gateway.py`、`test_gateway_evidence_budget.py`、`test_model_profile_authorization.py`）随全量运行通过；未修改其内容。
- 现有 key 读取路径（`KeyPoolManager` 读取 `api_key_encrypted`）未修改。

## 8. Error contract

- 校验错误（422）：
  ```json
  {"error":{"code":"VALIDATION_ERROR","message":"Request validation failed","details":{"errors":[{"field":"body.api_key","code":"TOO_LONG"}]}}}
  ```
  `X-Request-Id` 与请求头一致。
- 拒绝录入（422）：
  ```json
  {"error":{"code":"VALIDATION_ERROR","message":"API key intake is unavailable: no trusted secret channel is configured, raw credentials are not stored","details":{"reason":"secret_intake_unavailable"}}}
  ```
- 401 / 403 与 S1.1 provider 故障 502 未改。本轮不把拒绝录入映射为 502。
- 全局 `RequestValidationError` 处理器会改变所有路由的 422 形态（由 `detail` 改为 `error`），符合契约统一要求；全量测试未发现依赖旧形态的测试。普通非敏感字段（如 `body.api_key`、`body.label`、`path.provider_id`、`query.limit`）保留定位。
- OpenAPI：`create_key_pool` 只广告 422（描述为录入不可用、不接受或存储原始 key）。装饰器的 `status_code=422` 仅用于去掉成功项，路由函数不会正常返回。

## 9. 顺序探针

`docs/validation/s1-3-secret-intake/order-probe.txt`：
- 未认证 + 畸形 JSON → 401（中间件在解析请求体前完成 `/platform` 认证）。
- 未认证 + 有效 body → 401。
- session 调用为 0。

## 10. 风险

1. FastAPI 内部 `RequestValidationError` 对象自身的 `str()` 与 `errors()` 仍包含输入值（已在独立探针中确认）。本项目处理器消费该异常，不记录、不返回输入；但若未来有代码记录该异常对象，会泄露。
2. 既有数据：若数据库中已存在原始 key（历史写入），本任务不处理，S1.1 网关仍读取 `api_key_encrypted` 字段。需要单独的迁移 / 轮换任务，不在本任务范围。
3. OpenAPI 已改为只广告 422（返工项）；若未来接通录入，需要同批更新该声明与测试。
4. 字段定位依赖路由 `dependant` 与 endpoint 类型注解。`get_type_hints` 失败或 body 注解为 `Annotated[...]` 包裹的模型时，会退化为来源级字段（只丢失定位，不泄露）。嵌入多参数 body 分支已实现，但没有专门测试；Annotated-wrapped 模型同样没有测试。两者都是 fail-closed 降级，不是已验证的字段定位。
4a. 框架内部 `RequestValidationError` 对象仍持有原始输入（见 §0），不擦除。
5. `KeyPoolRepository.create` 保留签名并抛错，未删除参数；这是有意为之（显式拒绝，不留隐藏入口），后续 S1.4 或 alias 任务可以再决定是否删除。

## 11. Unverified

- 未连接 PostgreSQL：Repository 的“零写入”由 observing session 证明，未使用真实 SQL 计数。`RAGOS_TEST_PG_DSN` 未设置，21 个 PG 相关测试 skip。
- 未启动 uvicorn 服务做端到端日志观察；日志断言基于 pytest caplog。
- 未执行浏览器 UI 旅程（本任务无前端改动）。
- 未对既有数据库行做任何检查或变更。
- 未验证 Codex 审查结论（待 Review）。

## 12. 环境、安装与回滚

- 冻结依赖：`/tmp/opencode/s1-3/requirements.lock.txt`，由 `/tmp/opencode/s1-1/requirements.lock.txt` 复制（SHA256 `8cc2b4466cf831136877a340c8e7f57157dcfe90416375e4849ea5735099a042`）。`uv.lock` 与 `pyproject.toml` 未改（SHA256 `ecb49d1e…` / `581f4445…`）。
- 独立 venv：`/tmp/opencode/s1-3/venv`，Python 3.11，按上述冻结 lock 安装；快照目录为 `/tmp/opencode/s1-3/snap`，未覆盖 s1-1 的 venv、快照或证据。
- 安装：`/tmp/opencode/s1-3/reinstall.sh`，非 editable，从 `git ls-files -co --exclude-standard` 快照安装，未使用 PYTHONPATH。每次源码变更后重新执行。
- 源码与安装摘要：`docs/validation/s1-3-secret-intake/source-digest.txt`。安装产物 `errors.py`、`model_center.py`、`model_gateway.py` 与源文件 SHA256 完全一致。
- 回滚原则（fail-closed）：
  - 不得通过恢复原始写入路径回滚。`KeyPoolRepository.create` 与 `create_key_pool` 的拒绝不变量是不可回退的；若需回退，应先修复再重新部署，不能恢复原始 key 写入。
  - 不得使用 `git reset`、`git checkout`、`git stash` 或 `git clean` 处理这些文件，以免覆盖或删除 S1 / S1.1 WIP。
  - 若需临时回退 `errors.py` 的字段定位，只能回退到“统一但不回显”的状态，不得退回 FastAPI 默认 `detail`（它回显输入值）。
  - 删除代码不等于删除数据；本任务未改变数据库，也没有迁移或数据清理。
  - 本报告未执行任何回滚。

## 13. 建议提交信息（仅建议，未提交）

```
fix: reject unsafe raw credential persistence
```

## 14. 证据索引

- `docs/validation/s1-3-secret-intake/baseline-full-pytest.txt`
- `docs/validation/s1-3-secret-intake/red-run.txt`
- `docs/validation/s1-3-secret-intake/green-targeted.txt`
- `docs/validation/s1-3-secret-intake/green-full-pytest.txt`
- `docs/validation/s1-3-secret-intake/ruff.txt`
- `docs/validation/s1-3-secret-intake/order-probe.txt`
- `docs/validation/s1-3-secret-intake/source-digest.txt`（首轮）
- `docs/validation/s1-3-secret-intake/rework-red-run.txt`（返工 RED，11 failed / 39 passed）
- `docs/validation/s1-3-secret-intake/rework-green-targeted.txt`（50 passed）
- `docs/validation/s1-3-secret-intake/rework-s1-1-regression.txt`（53 passed）
- `docs/validation/s1-3-secret-intake/rework-green-full-pytest.txt`（832 passed, 21 skipped）
- `docs/validation/s1-3-secret-intake/rework-ruff.txt`
- `docs/validation/s1-3-secret-intake/source-digest-rework.txt`（返工源码与安装摘要）
