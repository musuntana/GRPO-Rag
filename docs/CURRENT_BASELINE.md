# 当前工程基线（唯一实时状态入口）

**核对日期：2026-10-21。** 仓库：`musuntana/GRPO-Rag`。本次确认的功能合并提交为 `c40d2983ae6518ef4f09e362a9a92fdfd9cca877`（PR #18；后续纯文档提交使 main SHA 继续变化）。最近相关完整回归：[PR #18 / 本地模型网关 985 passed，Ruff 与 Alembic 通过](https://github.com/musuntana/GRPO-Rag/actions/runs/39623355538)。验证在功能分支提交 `37e700f1` 上执行；合并前**仅恢复手动 CI 工作流配置**，没有修改通过测试的功能代码，合并后的 main 没有重复运行完整 CI。历史 S0–S1 运行可在 [Actions](https://github.com/musuntana/GRPO-Rag/actions) 核对，不把合成模型响应测试宣传为真实 Qwen 效果。

这是**主线和当前能力的汇总**，与某一次本地运行日志不同。历史实验日志仍保留在 `docs/validation/`；旧 `docs/project-progress.md` 是按时间记录的 S0 审计，不再作为“最新”来源。

## 能力与验收口径

| 领域 | 已纳入 main 且有测试证据 | 仍未实现或未验收 |
|---|---|---|
| 文档来源 | 文本型 PDF 字节经 Poppler 逐页提取；PageChunker、跨度/不可变版本契约 | 扫描件 OCR、复杂版面保真、生产隔离的任意文件上传 |
| 索引 | PostgreSQL 冻结表示与索引计划、任务租约/CAS 发布、Qdrant 读回对账 | 持久 Qdrant **服务器重启**与备份恢复、企业正式上传 |
| 检索 | Qdrant HTTP 重连、租户过滤、PG 激活/对账证据回填及源身份核验；PR #19 在开发环境接通本地 ModelRouter Embedding Dense 查询，并加入 SQL 冻结索引版本过滤 | 实际 Qwen 权重的全文索引重建、生产授权/计量网关、真实用户问答与跨文档语义评估 |
| 模型 | OpenAI 兼容 Chat/Embedding、本机真实 PDF 验收 CLI、冻结向量维度；PR #18 增加仅在显式开启且 API 绑定回环地址时可用的租户绑定本地 Chat/Embedding ModelRouter | 真实 Qwen 权重及效果未在 CI 验收；生产密钥可信解析、预算计量与授权 Provider 准入、真实 Dense–PG 证据全链路 |
| 评估 | 页级去重、MRR@10/NDCG/Hit@6 口径、冻结 JSONL 离线报告与合成测试 | 带人工标注且审计的数据集上的真实 Recall/Hit/NDCG；语义引证一致性 |
| 训练 | SFT 样本准备及 GRPO reward/排序辅助函数 | 真实 SFT/GRPO/LoRA 梯度更新、checkpoint、模型效果对照 |
| 用户体验 | PR #12–#14：聊天 SSE 统一 run_id、已完成运行授权 GET 回放、SQL 提交先于终态 SSE、POST 同键持久幂等（迁移 0013） | 生产签名鉴权/成员授权、可用 Web UI、运行中跨进程 SSE 续传、丢失生产者后的安全恢复与真实计费核验 |

**结论：S0 本地工程基线已通过；S1 是可测试的纵向工程切片，但没有达到真实企业 RAG 用户旅程的退出标准。**

> **2026-10-21 增量（PR #12 已合并）**：聊天断线重连不再通过重新 POST 调用模型；现在有完成运行的会话授权 GET SSE 回放。该修复在 PR 分支的 [完整 CI #39593995266](https://github.com/musuntana/GRPO-Rag/actions/runs/39593995266) 上验证了 **946 passed**，且恢复了手动 CI 触发器；**未宣称此后 main 被自动重新测过**。完整流式在途恢复、持久请求幂等和 SQL 最终提交先于 run_finished 的时序问题仍待修复。

> **2026-10-21 增量（PR #13 已合并）**：`ChatService` 现在使用显式持久化回调，在 PostgreSQL 成功提交用户/助手消息、引用和记忆写回任务**之后**，才发布 `memory_writeback_scheduled` 与 `run_finished` SSE。对应开发分支 [CI #39595380720](https://github.com/musuntana/GRPO-Rag/actions/runs/39595380720) 检查 **949 passed**。终态事件现在不会先于 SQL 提交，但仍没有幂等请求唯一键、跨进程运行中恢复，也不能保证 SQL 与文件日志原子提交。合并后 main 未再运行完整 CI，以避免重复消耗额度。

> **2026-10-21 增量（PR #14 已合并）**：聊天 POST 现在必须携带 `Idempotency-Key`，首次请求在 PostgreSQL 事务中先取得带唯一约束的持久运行声明再调用模型。同一租户/用户/会话/键并发请求最多有一个执行者；相同请求的重复提交直接返回原运行 ID 与状态，不会再次生成；同键不同请求返回冲突。数据库新增 `app.chat_request`（RLS、请求摘要、终态不可逆），所需 schema 版本升级为 **0013_chat_request_claims**，部署 API 前必须迁移。开发分支 [完整 CI #39600665111](https://github.com/musuntana/GRPO-Rag/actions/runs/39600665111) **956 passed**，包含真实 PostgreSQL 并发争用测试；其后仅恢复手动 CI 并合并，**没有声称最新 main 被再次全量验收**。这保证的是“同一个请求键不会重新启动模型”，不是跨进程任意重试 exactly-once：运行中崩溃会留下 claimed，未实现生产者恢复、事件日志原子提交或实际计费核验。

> **2026-10-21 增量（PR #15 已合并）**：新增读取 PostgreSQL `chat_request` 的用户/会话隔离运行状态 API `GET /chat/conversations/{conversation_id}/runs/{run_id}`。运行中状态只提示轮询或人工排查（10 分钟阈值不构成租约失效证明），已提交的回答必须通过 SQL 助手消息与完整连续 SSE 日志双重验证才能标记为可回放；失败、事件丢失或损坏均不自动再次调用模型。开发分支 [完整 CI #39602526009](https://github.com/musuntana/GRPO-Rag/actions/runs/39602526009) **964 passed**，Ruff 和 Alembic 通过；CI 临时 push 触发随后恢复为仅手动，合并后的 main 没有再次运行完整套件。仍待解决心跳/任务租约、真正安全的人工对账修复、分布式在途 SSE 恢复及真实模型计费核验。

> **2026-10-21 增量（PR #16 已合并）**：加入可在本机执行的 `scripts/check_local_pdf_rag.py`，使用真实的合成 PDF 字节与 Poppler、显式指定的 Qwen Chat/Embedding OpenAI 兼容接口、Qdrant 检索及结构化引用核验，输出不含原文或模型答案的 JSON 验收报告。开发分支 [完整 CI #39609139927](https://github.com/musuntana/GRPO-Rag/actions/runs/39609139927) **971 passed**，Ruff / Alembic 通过，测试使用的是合成模型响应而非真实 Qwen 权重；Qdrant HTTP 和 PDF 解析是实际服务/解析器。CI 触发规则已恢复为仅手动，**最新 main 没有再跑全套**。实际 Qwen 模型、官方模型权重版本/维度、真实 API 响应仍需操作员在 Mac 上运行脚本并保存测试报告；这不是企业正式 ModelRouter、已发布 PostgreSQL 文档、真实评估集或 GRPO 训练。

> **2026-10-21 增量（PR #18 已合并，取代未合并的 #17）**：在已有 `ModelRouter` 内加入关闭为默认值的本机 `local_openai` Chat/Embedding 适配：只允许明确配置的开发环境、回环绑定 API、本机模型端点、平台 Provider UUID、租户 use-case 绑定与精确模型名，拒绝未经批准的模型和 URL；不会将数据库 `KeyPool.api_key_encrypted` 当作 API Key 外发。Chat 限制 max_tokens、输入量，Embedding 校验维度、有限数值和非零向量。开发分支 [完整 CI #39623355538](https://github.com/musuntana/GRPO-Rag/actions/runs/39623355538) **985 passed**，Ruff/Alembic 通过。GitHub 前一个 PR #17 的 head 快照一度滞后于安全恢复的手动 CI 提交，因此关闭 #17、重新从同一最终分支建立 #18 后核对无工作流差异再合并。**仅开发网关部分可用；正式 Dense 检索尚未连接 Qwen 已冻结向量与已发布 PG 索引，真实权重、生产鉴权及预算未验证。** 使用说明见 [S1 开发本机网关验收文档](validation/s1-dev-local-model-router-2026-10-21.md)。

> **2026-10-21 增量（PR #19 已合并）**：正式检索构造器在明确的开发环境/本地 ModelRouter 准入下，支持将冻结身份的 Qwen Embedding 查询向量送入 Qdrant Dense Recall；结果继续通过 PostgreSQL `active_index_id`、`indexed`、PG/向量 digest 对账及不可变源身份校验，**新增与查询模型完全匹配的 `ChunkManifest.embedding_profile` 和 `embedding_version` 回填条件**。开发分支功能代码在 [CI #39626426003](https://github.com/musuntana/GRPO-Rag/actions/runs/39626426003) 验证 **987 passed**、Ruff/Alembic 通过。采用独立一次性 CI 分支，之后强制恢复该分支到仅手动工作流，PR #19 本身不包含 CI 改动。该测试实际使用 PostgreSQL/Qdrant/Poppler，但 Embedding 响应为确定性模拟；未验证 Mac 上实际 Qwen 权重，也未实现通用企业 PDF 上传与真实向量重建。此轮无迁移，使用说明见 [S1 冻结 Dense/PG 对账](validation/s1-dev-frozen-dense-pg-recall-2026-10-21.md)。

## 本轮治理内容

- `push` 与 `pull_request` 在旧 workflow 同时触发完整 CI，容易为一次代码修改重复记账。统一改为 **手动 `workflow_dispatch`**：`Quick checks (manual)` 和 `Full integration (manual)`。不满足自动强制合并门槛时，由 reviewer 核对 **最新 HEAD 与手动 CI 结果一致**，不可盲信旧 run；团队如需 required status checks 应单独调整 GitHub 分支保护。
- `FrozenEmbeddingSpec.version` 把 **向量维度** 也纳入 Qdrant 物理空间身份，防止相同模型名称/权重标识但尺寸不同的向量共用旧索引。这是身份版本迁移：开发环境的旧集合保留，只能影子重建/对账，**禁止原地混写或删生产数据**。
- 旧分支曾按 GitHub `compare main...head` 检查，9 条历史 `feat/*` / `fix/*` / `test/*` 分支 `ahead_by=0`，相关 PR 均已合并。最新分支搜索仅返回 `main`、`chore/ci-on-demand-and-baseline-hygiene`、`feat/s1-authorized-page-ranking-capture` 这 3 条；PR #10 和 PR #11 均已 merged，因此这两条也是候选，但脚本目前只自动识别 `feat/` / `fix/` / `test/` 前缀，`chore/` 仍需在 GitHub 分支页面手动删除。当前只部署了默认 dry-run 的 `Prune merged branches` 入口，**不宣称是本次操作删除了远程历史分支**。

## 唯一后续技术优先级

1. 经过权限、凭据/预算约束，实际运行一次真实 Qwen Embedding / Chat，并冻结模型 ID、权重身份和向量维度。
2. 从已发布的 PostgreSQL + Qdrant 检索真实人工审查页级 Gold，收集 **真实** dev/holdout traces。PR #10 的可信检索结果 JSONL 采集逻辑已并入主线，但目前只有合成模型接口的集成测试证据。
3. 同数据集比较 Dense 基线与成熟现成 Reranker；GRPO 仅在有稳定可归因的错误模式且收益证据时实现。
4. 优先完成最小鉴权、上传/检索/引用用户旅程，不再增加未经实测的占位模块。

## 当前验证和操作入口

- CI 策略与适用分支、手动操作：[`CI_POLICY.md`](CI_POLICY.md)
- 历史 S0 进度与验证资料：[`project-progress.md`](project-progress.md)
- 本地冻结 Qwen 环境说明：[`validation/s1-frozen-qwen-embedding-2026-10-21.md`](validation/s1-frozen-qwen-embedding-2026-10-21.md)
- 页面级 JSONL 评估口径：[`validation/s1-page-ranking-baseline-2026-10-21.md`](validation/s1-page-ranking-baseline-2026-10-21.md)
- GitHub Actions 最新事实：[workflow runs](https://github.com/musuntana/GRPO-Rag/actions)

不要把 CI 的合成数据通过、自动化测试数量、代码辅助函数视为真实模型效果或生产验收。
