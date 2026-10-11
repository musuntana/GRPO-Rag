# 项目推进与集成记录

更新：2026-10-09。集成负责人：`/root`。当前裁决：**S0 本地退出条件通过；下一步安排 S1，尚未执行 S1**。

下文按时间保留历史快照；“未提交”“S0 尚未退出”及早期测试数量只描述当时状态，最新状态以“最终本轮裁决”和文末提交整理为准。

## 1. 基础状态与工作归属

- 分支 `main`；基础提交 `5858088dd93337576dc97e7bc2f619ebdbd2bd24` 的实际完整 SHA 以 [manifest](validation/s0-2026-10-09/manifest.json) 为准。
- 开始时工作树干净，无须覆盖或猜测未提交工作的归属。已有实现来自历史提交；本轮改动仍未提交。
- 已阅读 AGENTS、GLOSSARY、architecture、roadmap、constraints、multi-agent-development 和 ADR-0001–0007；新增 ADR-0008 解释真实发现的 RLS 缺陷与安全回滚。
- 先由两个只读审查角色核对领域/存储，再冻结 S0-C1，分配不重叠写入。所有写者交接后，协调者接管集成；独立审查不计算作者对自己代码的结论。
- 未 commit/push、部署、远程发消息、付费训练或删除既有持久数据。数据库验证只使用本轮独立 tmpfs PostgreSQL 容器；没有对项目现有数据库迁移。

## 2. 冻结领域、模块与契约

**S0-C1 身份、存储与任务。** core 持有唯一 TenantContext，可信认证/成员关系产生身份。共享 key 为 `tenant/<UUID>`；私有 actor key 为 `tenant/<UUID>/actor/<percent-encoded actor_id>`。不创建无成员模型的 workspace。上下文禁止动态属性与外部 scope 注入，`bind_user(UUID)` 在服务端解析有效用户后返回派生上下文。共享检索使用 tenant + KB ACL；会话/记忆额外校验 user owner，不能用查询用户的私有 key 过滤共享 KB。

存储第一批按既有 0001–0008 迁移恢复 ORM，不创造平行表/类型。SearchChunkUpsert → FTS/Qdrant → SearchHit → recall 贯通现有 page/span/结构字段。冲突写入不能更换既有 tenant/KB/document/user。vector collection 按 embedding profile + tier；point ID 带 tenant/resource namespace，payload chunk_id 为原始 PG UUID。hash embedding 只在显式测试 opt-in；缺真实 writer 时拒绝默认索引。

任务 pending/retryable → leased → completed/failed；只领取 registry 的类型。attempts 是执行代次，旧执行者不能提交；锁定/终态 SQL 必需 expected_tenant_id + expected_attempts。过期 lease 可回收，连续崩溃达到次数上限后 failed。相同幂等 key 的不同 kind/payload 拒绝；重复相同输入返回既有任务。

**S0-C2 RLS。** 0003 的零参数函数未绑定当前行，缺范围/异常会放行。0009 使用显式 row tenant 参数，缺失/空/非法范围拒绝，覆盖 app/search。事务开始事件 SET LOCAL，避免依赖先查询造成 autobegin 冲突；worker 从权威任务 ref 绑定事务身份，特权连接仍使用 tenant SQL predicate。LangGraph 独立 setup checkpoint 表，Alembic 只管理 app/search，不能生成删除 checkpoint 的迁移。

数据流仍为：入口 → service/runtime → 领域 Interface → repository/gateway → 持久状态/事件 → DTO。PG 与 vector 独立提交，**本轮没有交付完整 manifest 对账协议**；没有真实 workflow、ingest 或 train handler 的进程在 DB 初始化前明确拒绝启动。默认 Noop/hash/log-only 不构成成功。

## 3. 任务、所有权与实际状态

| ID | 目标 / owner | 精确写入范围 | 前置 / 契约 | 实际状态与验收 | 前端 / 观测 / 回滚 |
|---|---|---|---|---|---|
| S0-01 | 身份契约 / identity_audit | core/tenant.py；test_tenant_context.py；test_tenant_isolation_probes.py；第二批 test_dense_recall.py、test_memory_facts.py、test_memory_manager.py、test_retrieval_routes.py | 只读审查 → S0-C1 | 已交接并集成；不可注入 scope、共享/私有范围、用户绑定负例通过 | 无 UI；trace/scope；身份与消费者整批回退 |
| S0-02 | ORM / storage_audit | storage/models/app.py、search.py、__init__.py、base.py | S0-C1，0001–0008 | 已集成；完整安装导入、metadata consistency、真实 PG schema 对照通过 | 无 UI；schema diff；本批仅代码映射，无数据删除 |
| S0-03 | 安装与消费者 / root | pyproject.toml、uv.lock、core/config.py；runtime/vector.py、ingest.py、retrieval.py、search.py、chat.py；storage/repositories/search.py、conversations.py；memory/fact_store.py、manager.py；retrieval/recall/memory.py；tenant/resolver.py；API/main.py、dependencies.py、routes/chat.py、tasks.py；ingest/workflow main.py；对应消费者测试 | 01/02 → S0-C1 | 标准/锁定非 editable 安装通过；80 模块导入；563 项收集/执行通过；部分生产能力明确不可用 | 没有 Web；task/lease/scope/manifest；新 vector profile 要影子重建，不混旧点 |
| S0-04 | 独立审查 / worker_fix、storage_audit、identity_audit | 只读他人 diff / 新迁移 / 证据 | 集成 → 审查 → 修复复验 | 发现并修复同租户 UUID 覆盖、私有绑定遗漏、错误 scope、提前 DB 副作用、特权终态无 tenant 条件、无限崩溃回收 | 不写文件；保留剩余缺口；不据 mock 宣布阶段完成 |
| S0-05 | worker 生命周期 / worker_fix，交接后 root | runtime/tasks.py；storage/repositories/tasks.py；train-worker/main.py；test_task_dispatcher.py、test_task_retry_policy.py | S0-C1；API attempts DTO 同批 root 更新 | 类型限制、代次失效、分类失败、过期回收/崩溃上限通过；train 明确不可用 | 无 UI；task/attempt/lease；API与仓储同时恢复 |
| S0-06 | RLS / root | migrations/0009_fail_closed_rls.py、migrations/env.py；storage/db.py；API/dependencies.py；ADR-0008；test_storage_integration.py | S0-C2；独立审查 | 升级/回退/再升级及非 superuser 负例通过；无范围/非法/外国 tenant 拒绝 | 不删业务数据；downgrade 保留默认拒绝，不恢复 unsafe policy |
| S0-07 | 维护与复现 / root | README、GLOSSARY、architecture、project-progress；.github/workflows/s0-local-checks.yml；scripts/verify_s0_processes.py；.gitignore；validation/s0-2026-10-09/* | 以上集成 | 本地证据已落盘；CI 文件已写，未在 GitHub 执行；删除依赖 log-only/Noop 的两个旧 smoke 脚本 | 无 UI；manifest/digest；恢复源码但不回退安全政策 |

表中模块路径均相对其 `packages/<module>/src/ragos/<module>`，API/worker 路径相对各 apps 的 src 包；测试均在 tests。交接后的所有修改由 root 单写，其他角色只读。安装、容器、迁移、依赖锁与最终测试均由 root 串行执行。

## 4. 验证证据与边界

原始输出与代码文件摘要在 [validation/s0-2026-10-09](validation/s0-2026-10-09/manifest.json)。

| 检查 | 实际结果 / 证明范围 |
|---|---|
| 原基线非 editable 安装后收集 | 133 项可收集，18 个收集错误；[原始输出](validation/s0-2026-10-09/baseline-collection.log)，不是 baseline 通过 |
| Python 3.12.14 标准安装 | `uv pip install '.[dev]'` 非 editable 通过；未设置 PYTHONPATH |
| 全新锁定安装 | uv 0.12.23 `sync --frozen --no-editable --extra dev` 到另一个空临时环境，通过；未证明其他 Python 版本 |
| 完整收集/全量测试 | **563 collected / 563 passed**；[最终输出](validation/s0-2026-10-09/locked-tests.log)；包含 8 个真实 PG integration cases，其余含确定规则/mock，不全部是端到端 |
| 真实存储 | PostgreSQL 18 tmpfs：全文读回/页跨度、跨租户和同租户跨用户 UUID 冲突、幂等 payload、lease reclaim/fencing、重复崩溃终态、实际 dispatcher、事务复用；Qdrant 本地引擎验证 payload/filter/profile，hash 只作为测试向量 |
| 非 superuser RLS | NOBYPASSRLS/非 owner 临时角色，app/search 缺/错/非法范围为空，正确范围只读本 tenant；不能据迁移 superuser 通过认定隔离 |
| 迁移与回滚 | 0001–0009 升级通过；0009 downgrade→0008 后 RLS 负例仍通过，再升级；[回滚负例](validation/s0-2026-10-09/rollback-negative.log)。不证明回退到 0003 之前安全 |
| ORM 实库对照 | API 完成 checkpoint setup 后 app/search `alembic check` 无差异；[结果](validation/s0-2026-10-09/schema-check.log)。checkpoint 由 LangGraph 管理 |
| 安装模块/进程 | 80 模块导入；API 实际 lifespan、PostgresSaver setup、healthz、无身份拒绝通过；三个 worker 启动能力检查明确 unavailable，不称真实 handler 已运行 |
| lint / diff | apps/packages/tests/scripts + 本轮 env/0009 lint、diff whitespace 通过；扩展到历史迁移发现 **117 项既有 lint 问题**，未顺手重写旧迁移；[结果](validation/s0-2026-10-09/historical-migration-lint.log) |
| 未执行 | GitHub CI、真实 provider/语义 embedding、Qdrant server 版本组合、真实文档/模型评测、浏览器 UI、生产身份授权、GPU训练、负载测试 |

沙箱内首次安装 DNS 失败，受限 ASGI 测试出现停滞；受审查的隔离本地执行完成验证。环境故障不转换为代码通过。没有重用生产数据，没有真实算法指标，不能由测试数量推断质量或训练收益。

## 5. 当前能力分类与退出裁决

- **已实现且已验证**：标准包/入口、基础 scope 接口、现有 ORM 映射、受控 search/vector 字段、任务类型/回收/代次/失败上限、默认拒绝 RLS、本地 API生命周期。
- **局部已接通**：安装的 chat/retrieval/profile API 和私有身份消费者；规则及 mock API 验证通过，不等于真实模型问答闭环。
- **明确不可用**：默认 workflow/ingest/train 进程、生产启动。缺失能力在访问 DB 前失败。
- **占位实现**：StubProvider、显式测试 hash/Noop、SFT/GRPO辅助规则。它们不构成模型服务、训练权重或收益。
- **尚未实现/验证**：不可变 document/representation version 跨全链身份；持久 indexing plan + 双写幂等/对账/恢复；长任务无锁外部执行、心跳、硬截止/取消；生产签名身份/成员/完整 ACL；真实 Web旅程。

因此 **S0 尚未退出**。没有将下一阶段需求提前设为默认，也没有把不可用入口或本地 mock 当作 S0 全部验收。

## 6. 观测、风险与回滚

观察 task_id/tenant/attempts/lease_until/status、trace_id、scope 拒绝码、向量 profile/维度及缺 hydration；worker 启动出现 unavailable 是已知能力边界。当前 handler 仍持有数据库行锁；长外部执行会阻碍回收，尚无 heartbeat/deadline/cancel，不能用于真实长任务。

数据回滚不等于 Git 回退。0009 downgrade 保持安全 predicate；不要恢复原缺范围/异常放行函数。point ID/profile 已变，既有向量必须用新影子 profile/集合重建并读回，不原地混用旧点，不删除原数据。没有真实外部消费者或现有数据迁移证据；生产身份和数据库角色仍需单独验收。恢复应用必须同批恢复类型、API attempts DTO、仓储与调用方，且仍遵守安全策略。

## 7. 下一步依赖（仍先做 S0）

| 后续任务 | owner / 前置 | 最小交付与证据 |
|---|---|---|
| S0-08 canonical evidence version | root 契约 owner；先冻结 document/representation/index identity、旧行迁移与来源校验 | schema/DTO/vector/citation/gold 同批对齐；错版本、错页、错 resource 反例；不得用虚构默认版本 |
| S0-09 indexing recovery | S0-08 → 持久 plan、独立提交、幂等对账；允许路径与单写者在派工前细化 | PG/vector 单侧成功、进程退出、重复入库与恢复；只在两端一致后发布 indexed |
| S0-10 task execution lifecycle | 现有 task fencing → 定义无长锁的执行提交、heartbeat、deadline/cancel | 真正旧执行者失效、worker崩溃/取消终态、超时资源释放；专用非 owner角色验证 |
| S0-11 完整退出复验 | 08–10 与基础身份授权修复 → 独立审查 | 锁定安装、迁移/负例、真实入口能力检查与故障验收，才重新裁决S0 |

S1 入口依赖 S0 通过、授权文档与真实 gateway chat/embed/rerank 配置：上传→持久状态→问答→版本化引用阅读，浏览器成功/失败/取消/重连旅程与冻结 baseline。S2 在此基础上验证独立 split、指标、完整身份/RLS/replay 矩阵。S3 需要审核 train/dev/holdout、独立预算和真实权重加载；S4 仅在 SFT 后等预算对照，GRPO 无可靠收益则保留基线；S5 在主线验证后扩展。当前 S0剩余主要是代码/契约缺口，不能归咎于凭据或算力；真实模型/数据仅是 S1之后的额外条件。

测试清理：最终验证后检查容器无 Mounts、仅有 tmpfs，再移除本轮 `ragos-s0-validation-01a11`。既有容器、镜像、数据库和持久卷未改动。测试安装环境保留在 /tmp 供本地复查，不能代替仓库锁文件复现。

## S0-08 第二批冻结（S0-C3，2026-10-09）

上一轮已验证结果不作为本轮新代码通过证据。基础工作树仍为main/5858088，所有已有diff归属本目标；agents已停止旧写入。新ADR-0009冻结 DocumentVersion → DocumentRepresentation → ChunkManifest(index_id) →企业Chunk，active_index指针只发布对账版本。旧行NULL/unversioned，memory独立，不能发明版本。

本批 root 串行拥有：新 retrieval/identity.py；retrieval/types.py、citation.py、verify.py；storage/models/{app,search,__init__}.py；0010迁移；architecture/GLOSSARY/ADR/progress、依赖配置。字段与接口落盘后再交接独立仓储/消费角色。解析/向量/引用等消费者不得各自定义来源类型。权威原文跨度由冻结表示约束；页数按representation，不按doc_id。

S0-C3 字段/接口已冻结落盘：retrieval.identity.EvidenceIdentity（tenant/kb/document/document_version/representation/index/chunk UUID、representation_char_count、parent_unit与page_count）；ScoredChunk.source_identity/source_text；GoldEvidence.source_identity；Citation.source_identity + structural_verdict/semantic_verdict（初始not_checked）。存储投影的对应UUID字段允许旧记录NULL，SearchHit增加representation_char_count/parent_unit；新企业写入必须完整且仓储验证owner，memory投影不伪造版本。模型DocumentVersion/DocumentRepresentation与manifest/active-index字段、0010迁移由root串行完成。

| ID | owner | 唯一允许写入 | 冻结依赖 / 验收 / 前端 / 观测与回滚 |
|---|---|---|---|
| S0-08R | storage_audit | packages/storage/src/ragos/storage/repositories/kb.py；packages/storage/src/ragos/storage/repositories/search.py；tests/test_evidence_repository.py | S0-C3；来源owner/摘要/ref/发布CAS/legacy读取负例；无Web；version/index/scope；保留数据和旧active指针 |
| S0-08C | identity_audit | packages/retrieval/src/ragos/retrieval/{verify,pipeline,fusion,reranker,refine,expander}.py；recall/{lexical,hot,dense,parents,vision}.py；tests/test_citation_verify.py、test_retrieval_pipeline.py、test_dense_recall.py、test_parent_expansion.py、test_evidence_identity.py | S0-C3；完整身份与原文传播/错版本错span/页界限/未检验状态；无Web；citation verdict；整批消费者恢复 |
| S0-08V | worker_fix | packages/runtime/src/ragos/runtime/vector.py；tests/test_vector_writer.py；tests/test_evidence_vector.py | S0-C3；payload/point namespace含完整version，legacy默认拒绝，scope与维度负例；无Web；profile/index；影子集合重建 |

root继续唯一写models/0010/types/identity/citation/parse/runtime/tasks/ingest/API/其它测试/文档。共享文件变更必须报告root，不越界写。所有安装、数据库/服务与迁移只由root执行。上述角色已收到合同后开始独立生产者/消费者实现。

### S0-C4 准备/恢复契约（2026-10-09，继续S0）

root 已串行新增 storage/artifacts.py 与 runtime/index_plan.py。ArtifactStore 使用范围隔离、内容地址、原子不覆盖写入与SHA-256读回；原文、FrozenRepresentation JSON、IndexPlan JSON在文件存储，PG只存ref/digest。IndexPlan固定tenant/KB/doc/version/representation/index/profile、预期active指针和全部原文chunk；严格核验全局/页内跨度、parent-child原文关系及实际表示切片。个人记忆不进入企业计划。JSON协议schema_version=1是序列化协议，不是虚构文档版本。

63项同步测试已通过，包含真实临时文件的摘要/跨范围/符号链接/预算、计划原文和页坐标负例。这不证明PG迁移或两索引恢复。0010真实迁移因自动审批服务账户用量限制未执行；离线SQL生成通过。异步to_thread诊断在受限环境超时，首个定向异步测试进程仍需确认/处理，未将其记为通过。

下一子批沿用manifest作为唯一索引状态：pending→indexing→indexed/failed。只有PG与vector都按计划读回、指纹相等且执行代次仍有效，才提交reconciled标记和active指针CAS。新增pg_digest/vector_digest/reconciled_at由root通过0011串行修改；发布与默认读必须检验这些事实，不能仅相信indexed。PG与vector独立提交，失败保留计划和已完成阶段，重放同index/原文幂等；外部调用不持DB锁。旧raw-chunk handler将同批移除，不以Noop对账。

S0-C4 typed readback已冻结：storage/index_projection.ChunkProjection及projection_digest为两端唯一指纹定义，含完整lineage、原文摘要、搜索字段摘要、原始坐标和表示边界。IndexPlan.projection_digest(ctx)派生计划期望。Manifest新增pg_digest/vector_digest/reconciled_at；0011降级有读回事实时拒绝。真实0010/0011迁移仍未执行，不称schema已接通。

| ID | owner | 精确唯一写入范围 | 契约 / 验收 / 前端与回滚 |
|---|---|---|---|
| S0-09R | storage_audit | packages/storage/src/ragos/storage/repositories/kb.py、search.py；tests/test_evidence_repository.py | manifest.create(index_id必需)；发布检验pg/vector等于计划期望与reconciled；新增read_index_for_reconciliation返回typed projections，仍验证owner/KB；非active read仅该用例不加公共fallback；无Web；digest/index/cas，可恢复旧active指针 |
| S0-09V | worker_fix | packages/runtime/src/ragos/runtime/vector.py；tests/test_evidence_vector.py、test_vector_writer.py | projection统一写payload；reconcile(ctx,tier,chunks)->digest真实retrieve核对payload/namespace/finite维度；拒绝缺点/多点/错身份；不再embedding以免对账触发模型；无Web；vector候选保留不删除 |
| S0-09I | identity_audit | packages/runtime/src/ragos/runtime/ingest.py；tests/test_index_recovery.py | IngestionService(session_factory,tenant_ctx,artifacts,vector_writer,execution_guard)；execute(manifest_id)读取实际source/representation/plan，独立短事务PG写/读与外部vector写/读，最终fence+receipts+CAS；永久失败明确failed，保留plan/阶段；测试mock边界明确；无Web；不宣称跨库原子 |

root唯一拥有projection/IndexPlan/ArtifactStore/models/0011、Task接口/入口/API、实际PG集成、文档/依赖与安装。旧raw handler由root同批迁移；新进程仍不可用直到S0-10的无长锁/fence/心跳/取消接线，不能把S0-09接口存在称worker已接通。所有外部验证受自动审批阻断时不绕过。

## 当前集成裁决：S0-C3/C4（后续覆盖第一批“当前”状态）

- canonical来源链、企业/记忆分离、原文与完整身份传播、独立引用verdict、来源/manifest/chunk不可变与读回发布契约已修改代码。0010/0011新增真实DDL，但**尚未执行实库迁移**。旧数据不填造v1；无读回事实的历史indexed候选将按迁移保留数据、取消active并转failed待核对。
- ArtifactStore逐层dirfd/NOFOLLOW打开，拒绝FIFO/设备，内容地址原子不覆盖与摘要读回，fsync新建tenant目录链。配置root必须为服务端控制目录；该root本身的创建与主机断电恢复未做故障演练。
- IngestionService从实际原文件/表示/计划核验字节、切片、页坐标及来源MIME，分离PG和vector提交，按统一projection读回并在执行fence下CAS发布；失败保留计划/阶段。模型profile/version不符拒绝，已发布重放不embedding。新Noop路径已删除，旧任意chunks任务与API明确拒绝。
- 独立审查所发现的无标记猜引用、vision无hydration、同id改plan、SQL增删已发布chunk、manifest改id、父目录symlink竞态/FIFO、模型版本/MIME不符均已修代码并补适当规则/文件负例；SQL触发器只完成静态及离线生成，不写成实库通过。
- **676项可收集；142项选定同步测试通过**，覆盖真实临时文件、Qdrant本地引擎和mock事务恢复。全部相关lint/diff通过；0010/0011离线升级SQL生成通过。完整输出与新代码摘要在 [本批证据](validation/s0-source-index-2026-10-09/manifest.json)。第一批563全量通过属于旧快照，不能套用到当前树。
- 首批定向异步运行handle `38012` 在受限环境仍有停滞，尚未确认终态；独立 `asyncio.to_thread` 诊断8秒超时，不能当算法/服务性能数据。未因观察超时重启同测试；后续需确认句柄并处理。同步验证另用 `/tmp/ragos-s0-c4-locked`，避免复用该运行环境。
- 真实0010迁移自动审批**未执行**：审查服务报告账户用量限制，未完成审核，并非动作被判断不安全。不绕过审批；需要审批服务恢复后才能运行新临时PG的完整迁移、RLS/复合FK/SQL触发器/回退及真实双写故障验证。当前专用容器 `ragos-s0-c3-01a11` 仅tmpfs，为本批准备，尚无新迁移验收；不能把容器启动称集成通过。
- 新契约下旧 `tests/test_storage_integration.py` 企业写入fixture仍需迁移为真实版本/表示/计划，再由IngestionService真正对账发布；不将未设置DSN而跳过作为验收。全量API/异步旅程、当前process验收与GitHubCI未运行。
- 默认ingest/workflow/train和生产入口仍明确不可用；新handler要求dispatcher先释放领取事务，原dispatcher长事务路径还未替换，故不能宣称worker已接通。S0-10下一步修typed领取信封、lease心跳/到期失效、deadline/cancel、无外部IO长锁和确定终态；同时补真正准备计划及任务的服务、模型版本collection约束、旧引用诚实展示与API模块职责。
- **S0未退出，目标保持进行中。** 继续已授权本地实现；数据库验收的最小外部条件是审批服务可用，不能通过mock/放宽scope/跳过验证解除。回滚保留来源文件、计划和既有指针；0010有新来源、0011有读回事实时拒绝删证明字段，不能以Git回退代替数据恢复。无commit/push、部署、付费训练或既有持久数据删除。

## S0-C5 生命周期冻结（2026-10-09）

root串行拥有Task模型、0012迁移、runtime/tasks.py/shared TaskEnvelope、API与入口。ADR-0011冻结首次领取绝对deadline，重试不延长；租约到期即失效；cancel intent、timeout、crash达到确定终态；heartbeat只续当前代次。硬终止用独立子进程，不能以thread.cancel伪装停止。

| ID | owner | 精确允许路径 | 验收 / 前端 / 观测与回滚 |
|---|---|---|---|
| S0-10R | storage_audit | packages/storage/src/ragos/storage/repositories/tasks.py；tests/test_task_retry_policy.py；tests/test_task_lifecycle.py | C5 fields冻结后实现期限/取消/续租/严格fence，旧attempt和expired负例；无Web；task/attempt/deadline/cancel/terminal；不丢持久任务 |
| S0-10P | worker_fix | packages/runtime/src/ragos/runtime/process_runner.py；tests/test_process_runner.py | 服务端argv，deadline/cancel检测，kill process group并wait；真实本地CPU子进程验证，无DB/网络；无Web；pid/exit/error_code；停止本轮子进程 |
| S0-10A | identity_audit | 只读当前生命周期与ingest接线 | 作者与审查者分离，检查身份/失败/取消/长锁缺口；不写文件/不启动服务 |

models/0012/TaskEnvelope先由root落盘再允许存储实现。安装、实际迁移和统一集成验证仍root唯一执行；审批限流未解除，不绕过。

### S0-C5 集成结果（继续S0，2026-10-09）

- root将dispatcher改为短事务准备frozen TaskEnvelope（tenant/task/kind/attempt/lease/deadline与不可变JSONpayload），服务端command builder之后在独立进程组执行；处理器不持ORM session/DB锁。准备阶段确定错误立即按数据库权威intent/deadline/current attempt提交失败；stale结果忽略。
- 存储实现严格future lease/deadline/无cancel fence、首次绝对deadline不被重试延长、aware时间、受预算限制的heartbeat；pending/retryable取消立即终态，live leased先意图后kill/wait ack。历史attempts>0而deadline缺失明确TASK_DEADLINE_MISSING，不造开始时间。
- ProcessRunner Linux绝对argv/shell=False/明确env/DEVNULL输出，TERM→grace→KILL→wait；deadline独立watchdog可在DB回调短暂阻塞时杀进程组，worker shutdown也传播到watchdog。只有server标记replay_safe的任务在关闭后可有界重试。不能把process group当script安全sandbox；setsid逃逸需更强隔离，任意Script未启用。回调与终态响应不等于硬实时，DB连接/pool/statement/lock已加显式上限。
- 可信ingest child只接受task/tenant/attempt参数，关dotenv，从DB重建状态、payload与scope，解析只允许manifest资源引用；每阶段fence。普通/生产ingest仍缺real gateway→明确不可用；仅显式app_env=test + task_test_embedding配置允许已标注hash机械索引测试，不称模型检索完成。workflow/train仍未启用。
- 0012添加持久deadline/cancel/finish/error摘要与timeout/status/attempt CHECK，task身份/kind/payload/timeout不能改写，总deadline不能重置。数据/终态事实存在时downgrade拒绝删除。API加入受范围限制cancel POST与timeout字段，尚未通过当前真实HTTP/PG旅程。
- **当前725项可收集，215项选定同步测试通过；生命周期子集78项通过**。包含真实本地子进程退出/超时/取消/子孙清理/大输出、真实文件和Qdrant local；数据库及dispatcher使用mock的部分不是实库证据。新安装产物下运行，无PYTHONPATH。相关lint、diff通过；0012离线SQL生成通过。原始输出与快照在 [生命周期证据](validation/s0-worker-lifecycle-2026-10-09/manifest.json)。
- 全量当前测试与实际PG/worker尚未验收。初始临时真实PG测试中的旧inline handler/legacy index fixture须迁移新命令/计划协议；未设置DSN跳过不能视为完成。source/index迁移0010–0012自动审批尚未执行，原阻断未解除。调用方代码已接线不表示真实claim→process→heartbeat→reconcile→terminal旅程已验证。
- S0仍未退出。后续继续：实际准备plan+幂等任务服务、迁移真实integration fixture/非owner角色故障矩阵、模型version collection隔离、旧citation读取诚实状态、API入口service边界、完整安装/启动/故障验收；生产成员/签名身份仍保持不可用。恢复PG验证的最小外部条件仍是审批服务可用，不用mock代替。

## S0-C6 准备与集成冻结（2026-10-09）

IndexPreparationService接收服务端生成的IndexPlan和opaque retry key，在DB外真实校验source/representation字节、原文slice并写不可变计划；短事务重新核对source/KB/profile，创建manifest和manifest-only任务。重试按trusted tenant/actor/operation命名空间取得同一任务；同key不同语义计划/timeout拒绝。语义摘要排除服务端index UUID/预期active指针，并将chunk-parent图按计划顺序规范化，不能改变原文/配置/来源。未暴露客户端任意plan/命令入口。

| ID | owner | 精确写入范围 | 冻结 / 验收 / 前端与回滚 |
|---|---|---|---|
| S0-11P | identity_audit | packages/runtime/src/ragos/runtime/index_preparation.py；tests/test_index_preparation.py | prepare(plan,idempotency_key,timeout_seconds)->PreparedIndex；ArtifactStore/IndexPlan既有contract；PG事务只仓储calls，无外部IO；来源/重复/改输入负例，mock边界明示；无Web；source/index/task可关联，不删原数据 |
| S0-11T | storage_audit | tests/test_storage_integration.py；tests/test_index_integration.py | 当前0010–0012/C4/C5；真实version/representation/plan注册并execute发布后验证查找/边界/SQL不变量；registry为TaskCommand不inline handler；只写测试不访问DB/服务，审批限流不绕过；无Web；恢复临时资源 |
| S0-11M | worker_fix | 只读collections/profile/contracts | 给出model version collection隔离和依赖方向最小一致方案；不写/启动 |

root串行拥有TaskRepository helper、公共contract/模型/迁移/入口/API/文档与所有验证。TaskRepo新增lock_preparation_key(key)、get_by_idempotency_key(key)，以短事务advisory锁收敛同tenant+actor操作的并发准备，不变成runtime调度框架。已有文件所有者已交接。

S0-11P安装产物下8项同步测试通过，使用真实临时文件和mock仓储；未验证实际PG advisory锁或并发提交。

S0-11V追加冻结：worker_fix唯一写入storage/vector.py（从runtime/vector.py移动并删除旧入口）、core/config.py、runtime/retrieval.py、runtime/ingest_job.py、retrieval/recall/dense.py及相关vector/dense测试；S0-11T两个integration文件仍归storage_audit。集合身份由profile、immutable embedding_version、physical tier的规范JSON SHA-256派生，两端共用一个函数。旧集合保留但不默认读取；重建和读回成功才切换。生产gateway尚不能固定并核对实际embedding身份，因此dense接线明确不可用；显式test hash-test/sha256-hash-v1只验证机械索引。无前端变化，无删除向量数据，观测profile/version/collection与manifest。

S0-11C冻结并交接：identity_audit唯一写runtime/chat_history.py、tests/test_chat_history.py；root迁移API chat历史读取。历史DTO复用现有verdict，仅完整身份/坐标一致且记录structural ok、semantic entailed时保留原verified；缺事实降unverified，跨范围内容脱敏，suppressed不升级。不重跑judge、不补造版本、不改存储行，verification_origin=stored_record/source_available=false区别当前证据可读性。无页面改动，API/真实HTTP尚需验收；回滚DTO应保持拒绝伪称verified。

root串行补S0启动readiness：storage/db.py release revision检查、API及worker入口在任何setup写前调用；缺失/旧/未来/分支版本拒绝，不自动迁移。测试仅模拟版本查询，实库仍未验收。当前要求0012_task_execution_lifecycle，后续迁移必须同批更新release检查与验收。root同时给test ingest child显式传递profile/version并拒绝错误hash身份，避免子进程默认值取代父配置。

## 最终本轮裁决：S0 本地退出条件通过（2026-10-09）

本节覆盖前文各历史快照的“尚未实库验证”和“S0未退出”，不删除历史失败证据。分支仍为 main，HEAD 5858088dd93337576dc97e7bc2f619ebdbd2bd24；本轮改动均未提交，无commit/push、部署、训练或既有持久数据删除。

审批服务复核恢复后，root在本任务tmpfs临时PostgreSQL容器执行0001–0012真实迁移，alembic check返回No new upgrade operations。21项真实PG/文件/Qdrant local测试通过，覆盖授权与非owner RLS、来源复合FK/不可变触发器、准备服务幂等与进程并发、两侧读回与CAS、部分提交恢复、真实CPU dispatcher/续租/终态。显式hash没有语义质量结论。完整当前测试 **775 passed，无skip**；此前2项gateway异步停滞在本次权限正常环境中通过，旧handle38012已不存在，不将旧运行记为通过。

首次完整运行773 passed/2 failed已保留：非法element_type校验发生在DB访问之后，root将纯输入校验提前；过滤测试旧expectation漏掉已要求的doc_id非空谓词，同批更新。修复后完整重跑通过，不放宽权限或删除负例。另在独立空tmpfs库验证head→0009→head回退/重升级成功；这不证明带生产历史数据的降级可行，有新来源/receipt/执行事实的迁移仍应拒绝降级。

全新/tmp/ragos-s0-c6-clean通过离线frozen/no-editable安装，无PYTHONPATH；87个安装模块可导入，退休runtime.vector/chunking确实不存在。发现并清理本轮生成build缓存残留后重新构建，避免已删除模块留在wheel。实际API lifespan、LangGraph checkpoint setup、healthz 200、无认证401通过；workflow/train/普通ingest明确拒绝未接通能力。相关lint、编译、diff检查通过。[当前证据与源摘要](validation/s0-preparation-2026-10-09/manifest.json)包含完整/首次失败/实库/回退/安装/进程日志。

| S0退出条件 | 当前证据 | 裁决 |
|---|---|---|
| 干净标准安装及导入 | clean-install.log、clean-process-check.log；非editable、退休模块缺失 | 通过 |
| API/worker能力启动检查 | 实际API setup及认证拒绝；未实现worker真实启动拒绝 | 通过；不声称真实RAG或训练可用 |
| 完整收集及关键测试 | 775收集；775全量通过且指定实际临时PG DSN | 通过 |
| ORM/迁移/范围/任务/索引关键验证 | 真实迁移及alembic check、21项集成、空库回退重升级 | 通过当前临时数据范围 |
| 不以stub/宽权限伪造能力 | 默认hash关闭、生产与未知模型身份拒绝；scope负例 | 通过；真实provider留S1 |

已实现且已验证：S0领域身份、来源/index计划、范围仓储、双侧读回发布、任务租约/取消/期限、标准安装与显式能力边界。尚未接通：真实gateway embedding/reranker、完整ingest child与独立Qdrant服务旅程、生产签名/成员授权、上传/问答/引用Web。训练仅规则/准备代码，未训练；不称SFT/GRPO完成。S0不要求假造这些后续能力，因此本轮停在阶段边界。

观测必须关联tenant/task/attempt/deadline/cancel/error_code、document/version/representation/index、profile/version/collection、两侧digest/reconciled_at及active CAS；不记录秘密或详细token。失败保留计划和阶段事实，按同index重放；旧执行者不能发布。回滚保留原文/计划/旧active候选，不原地混用旧向量集合；有事实时拒绝破坏性downgrade，需显式迁移方案。主机断电、生产历史升级/负载、任意Script sandbox、真实模型质量尚未验证。

### 下一阶段入口与依赖（仅安排，未执行S1）

| 顺序 | 目标 / 预定owner | 前置与验收 |
|---|---|---|
| S1-01 | 冻结真实模型与出站/秘密/预算合同 / root | 明确一个provider、不可变embedding身份与维度、可信凭据别名及预算；缺条件拒绝，不能用hash代替 |
| S1-02 | 真实解析→准备→隔离worker→双索引 / backend owner待分配 | 消费本轮source/manifest/task契约；真实Qdrant服务与gateway，实际子进程故障/取消/恢复 |
| S1-03 | 冻结baseline及现成reranker / eval owner待分配 | 授权真实文档、审核gold、train/dev/holdout隔离；逐case来源/配置和真实调用记录；不提前SFT/GRPO |
| S1-04 | 最小上传/状态/问答/原页引用 / frontend owner待分配 | 后端DTO先冻结，覆盖成功/加载/空/权限/错误/取消/重连，实际浏览器旅程及两租户两用户负例 |

后续S2只消费真实baseline做评测/身份加固；S3需权重加载独立评测，S4需可靠收益对照，S5依业务与容量证据扩展。本轮没有默认开启HyDE/agentic/vision或提前更换训练策略。下一阶段最小输入是受控真实provider凭据/预算与可用于gold的授权文档；不需要公开部署。

验收后已清理本任务专用tmpfs PostgreSQL容器及其临时库，代码、安装环境与全部证据保留；未删除既有持久数据。

## 提交整理与下一步（2026-10-09）

本次将已交接的 S0 集成实现、迁移、测试、CI 配置、ADR 和原始验收证据整理为完整本地提交；对应 SHA 以 Git 提交记录为准，未推送远端。未改变业务代码或重跑原数据库实验。

提交前复核：最新 C6 manifest 的 20 个源码摘要匹配，当前 119 个源码文件与验收安装环境一致；相关 Ruff 与源码/文档 diff 检查通过；原始验收日志保留工具输出的尾随空格，不改写为格式化文本。既有证据记录 775 项全量测试、21 项实际存储集成通过，本次没有将记录复核说成重新执行测试。README 和 roadmap 已同步最新阶段，旧失败记录仍保留。

下一阶段使用 [S1 推进指令](next-step-instructions.md)。先确认真实 provider、可固定的 embedding 身份/维度、秘密别名、调用预算和授权文档；契约冻结后按独立文件所有权并行开发 backend/eval/frontend。真实模型条件缺失时先完成不依赖调用的工作，不使用 hash/stub 伪造 S1 验收，也不擅自公开部署或付费训练。

## S1 接续：模型授权边界（2026-10-10）

现场基线main/f0914adf7d97；工作树只有docs/opencode-s1-task.md未跟踪，保留原内容。原派工PID241700已不存在、临时日志不存在，且授权修复未落盘；不能将“已派发”视为完成。本次接续S1，不重写S0、不调用未知收费模型。

冻结领域：ModelProfile是平台模型配置；TenantModelBinding是可信tenant+use_case使用授权；override仅选择同一绑定，不能扩大范围。ProfileResolver先查询绑定再取得profile，拒绝必须早于key/provider副作用。停用profile仍拒绝。缺资源/用途不返回其他租户profile身份。现有fallback遍历本轮不自动启用；router未实现retry不能宣称已实现。数据流ctx→binding→profile→key→provider，失败在授权边界终止，不改持久状态。

| ID | owner | 精确写入范围 | 依赖 / 验收 / 前端 / 观测与回滚 |
|---|---|---|---|
| S1-01A | gateway_author | packages/model-gateway/src/ragos/model_gateway/profiles.py、router.py；tests/test_model_gateway.py、新tests/test_model_profile_authorization.py | 以上冻结合同；mock副作用负例+标准安装；无页面变化；tenant/use_case/error，禁止秘密日志；代码回退不得重新开放override绕过 |
| S1-01R | root | docs/project-progress.md、集成证据及独立审查 | 实现交接后统一安装验收；不覆盖OpenCode原任务记录 |

真实provider、固定模型身份/维度、出站、凭据别名、预算与授权文档待用户指定；已经提出最小资源问题。缺这些条件继续本地授权修复与契约核验，不启用stub/hash假装真实RAG。S1尚未退出；frontend/eval/ingest依赖共享模型与DTO冻结，不同时复制新契约。

S1-01B串行冻结（root唯一owner）：key_pool.py、新tests/test_provider_capability.py、retrieval/gateway.py仅真实能力说明。build_provider不再将任意provider类型转Stub；默认拒绝，Stub只能server显式allow_test_stub=True且provider_type=stub，并忽略lease密钥/地址。未知/真实provider未接通时抛现有ProviderError，不将数据库api_key_encrypted解释为明文。无模型调用或迁移，不改变key选择测试；后续真实adapter需秘密别名与权威预算先验收。修复与01A文件无重叠。

### S1-01A/B 当前集成结果（2026-10-10）

用户决定先Qwen、稳定后考虑OpenAI；选择本地服务，但后续确认地址/模型/文档条件“暂无”。本轮不安装大模型、不开收费调用、不读取密钥。已查官方Model Studio embedding/rerank说明供后续分用途接线；本地协议/具体模型仍未冻结，不将托管服务端点套给本地服务。

已修改并接通当前调用方：binding先授权override，同tenant+use_case的绑定模型才可选择；profile自身owner/use_case也必须匹配。默认provider factory拒绝未接通类型；测试stub必须server显式允许且类型为stub，不接收保存密钥或外部URL。生成max_evidence_chars落实为输入证据总字符上限，超限在dispatch前拒绝，不截断/重排引用；这不是token/费用预算，也不覆盖问题和系统prompt。

标准frozen/noneditable新安装产物下最终 **120项选定回归通过**（gateway/profile/provider/字符预算/API routes/dense/profile），相关Ruff/diff通过；初始11项baseline通过。mock仓储/provider与真实测试Stub构造，未使用实库或真实Qwen；完整测试仅收集，非全量执行。证据在validation/s1-model-authorization-2026-10-10/。旧S0 775实库全量属于已提交基线，未拿来声称S1当前全量通过。

独立审查者gateway_review发现stub构造缺参和bound profile owner未核对，两者已修复并增加实际构造/错配binding负例；最终回归通过。gateway_author只负责01A，root集成01B及审查修复，所有权串行移交。当前ModelProfile实际非nullable tenant-scoped，拒绝owner缺失/错配，不发明全局模型绕过。

尚未实现：权威预算账本/预授权结算、secret alias resolver、gateway短事务线程内session、真实固定embedding身份/批维数校验、出站/期限/取消，以及Qwen adapter。真实调用前必须冻结并验证这些合同；公开ResolvedRoute/provider接口仍待同批迁移，不能认为gateway入口本身已提供成本/秘密控制。尚无真实上传/问答/引用Web、黄金文档baseline。本轮S1未退出。

观测：授权错误在key/provider前终止，记录trusted tenant/use_case/error，不输出凭据/他租户profile；未接通模型明确ProviderError，证据字符预算ValidationError。回滚不改任何持久数据，但不可恢复override越权或任意provider→stub假成功。真实Qwen最小阻断条件：可连接地址、分用途固定模型/embedding维度、受控认证别名/调用预算、授权文档与gold；确认前继续预算/秘密/事务合同实现，无需公开部署。无commit/push、训练或服务部署；原OpenCode任务文件保留。

## S1 本地合并、有界清理与维护提交（2026-10-12）

本节只记录本轮本地合并/清理/维护的实测事实，不改写任何历史快照；权威证据引用 [S1 RC 证据包](validation/s1-release-candidate-2026-10-11/README.md) 与外部持久备份，不复制新框架。

- 现场基线（先核验后动工）：共享 `main` 与 `feat/grpo-rag-platform-v1` 均 HEAD `e244035d1164ddccb0b8e7562258c5e5484332e2`、tree `0257ef938aac95bede8b946ff77391291792f401`，两工作树干净；feature 使用已注册 worktree `/home/avaarg/.local/share/grpo-rag/worktrees/s1-local-20261012`。
- 4 个本地提交（相对 base `f0914adf7d97dc0271f80ba3e1db79dccdd44f99`）已按序冻结：A `841f9765477e26608d2670243d6f7f077e82588e`（feat(s1) 模型 profile 授权边界 + 显式 stub 门 + 证据字符预算）、B `200eb817da236dcb68c88f17e014d54c466037b9`（feat(s1.1) provider 准入）、C `cef5e7a7c8adb5bddbcebe89e8e2f8615513d930`（feat(s1.3) 拒绝明文凭据摄入）、D `e244035d1164ddccb0b8e7562258c5e5484332e2`（docs: archive immutable S1 RC evidence）。
- 本地 `main` 已真实 fast-forward 到 D：`git merge --ff-only feat/grpo-rag-platform-v1`（Updating f0914adf…→e244035d…），main 保持 D tree `0257ef93…`，相对 origin/main ahead 4；未 push、无 PR、无第二次 merge。
- 全量测试（作者实际日志）：**832 passed, 21 skipped**；21 项 PG 集成测试因 `RAGOS_TEST_PG_DSN` 未设置而跳过，不视为通过。安装产物对账 `INSTALLED_SHA_OK=True`（170 个 .py）。
- 独立审查：Codex 对 C 目标回归 **98 passed**，判 LOCAL PASS 并明确不得由此推断 commit/merge 授权；摘要已随 B 提交归档 [codex-review](validation/s1-3-secret-intake/codex-review.md)，同类内容含于 RC 包 `raw-evidence/bundle/{codex-review.md,codex-composition-review.json}`。
- 全仓 ruff 既有 **117 项错误**（116×E501 + 1×I001，均在既有 migration 0001/0004/0005），本轮 **0 新增**；目标文件 ruff 全部通过。
- whitespace 事实：历史 C vs base 的 `git diff --check` exit 2（两份 RED 日志共 16 个尾随空白行，历史字节保留）；D vs base 与 D vs C 均 exit 0。
- 未验证/未执行（NOT RUN）：真实 provider（Qwen/OpenAI）调用、PostgreSQL/RLS 实库、Qdrant 服务、浏览器旅程、迁移执行；**R1 仍 blocked**，未执行 S1.4/R1 或任何新实现。
- 本轮维护提交 E（docs-only，一条）：`chore: retire stale S1 handoff and record local integration`。删除 `docs/opencode-s1-task.md`（旧待执行合同已由 A/B/C/D 与本记录取代，原字节可自 Git A history 与冻结 `A.patch.gz` 恢复）；仅追加本节；新增 [本地集成记录](validation/local-integration-2026-10-12/README.md) 与 [cleanup-manifest.json](validation/local-integration-2026-10-12/cleanup-manifest.json)。E 不改变任何业务源码/测试/schema/lock；E 自身 SHA 不自我引用，实际值记录在外部 release/cleanup 日志。
- 有界 FS 清理（仅共享 main 工作树，gitignored/未 tracked）：删除 `build/`、`.pytest_cache/`、`.ruff_cache/` 及 `apps/packages/tests/scripts` 下 22 个 `__pycache__`（共 523 个文件、7,268,319 字节 apparent）；`/tmp/grpo-rag-*` 目标核验时均已不存在，无可删项；feature worktree 未移除（留待 E merge 后 Codex 单独处理，record NOT RUN）。这是清理操作统计，不是性能基准，也不保证磁盘即刻回收。明细见 [cleanup-manifest.json](validation/local-integration-2026-10-12/cleanup-manifest.json)。
- 权威证据仅引用：RC 包 `validation/s1-release-candidate-2026-10-11/`（manifest.sha256 8/8、raw-evidence.tar.gz 85 成员 OK/96 tar 条目、`patches/{A,B,C}.patch.gz` 解压 SHA 与原始一致）；外部持久备份与审批包 `/home/avaarg/.local/share/grpo-rag/releases/s1-local-20261011T161256Z/`（含 approval/D.patch `0639d514…`、commit-validation/、local-merge/、independent-review/、cleanup/）全部保留未动。
