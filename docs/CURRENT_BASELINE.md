# 当前工程基线（唯一实时状态入口）

**核对日期：2026-10-21。** 仓库：`musuntana/GRPO-Rag`；主线在本次治理分支创建时为 `b321d9a6d574dd1d5b96b47fb9eeba361cc2bea5`（PR #9 已合并）；**主线最近一次完整 CI**：[932 passed / Ruff / Alembic / PG+Qdrant](https://github.com/musuntana/GRPO-Rag/actions/runs/39585791768)。后续主线 SHA 和运行数应以 GitHub 当前 `main` / Actions 为准，不把本快照冒充实时结果。

这是**主线和当前能力的汇总**，与某一次本地运行日志不同。历史实验日志仍保留在 `docs/validation/`；旧 `docs/project-progress.md` 是按时间记录的 S0 审计，不再作为“最新”来源。

## 能力与验收口径

| 领域 | 已纳入 main 且有测试证据 | 仍未实现或未验收 |
|---|---|---|
| 文档来源 | 文本型 PDF 字节经 Poppler 逐页提取；PageChunker、跨度/不可变版本契约 | 扫描件 OCR、复杂版面保真、生产隔离的任意文件上传 |
| 索引 | PostgreSQL 冻结表示与索引计划、任务租约/CAS 发布、Qdrant 读回对账 | 持久 Qdrant **服务器重启**与备份恢复、企业正式上传 |
| 检索 | Qdrant HTTP 客户端重连、租户范围过滤、PG 已发布原文回填、来源身份不一致拒绝 | 真实模型/授权生产网关下的完整用户问答；全量跨文档语义效果 |
| 模型 | OpenAI 兼容 Chat/Embedding 请求与响应解析、按需本机探测；冻结模型版号/向量维度 | 密钥可信解析、预算计量、生产租户 Provider admission；本机真实 Qwen 验收 |
| 评估 | 页级去重、MRR@10/NDCG/Hit@6 口径、冻结 JSONL 离线报告与合成测试 | 带人工标注且审计的数据集上的真实 Recall/Hit/NDCG；语义引证一致性 |
| 训练 | SFT 样本准备及 GRPO reward/排序辅助函数 | 真实 SFT/GRPO/LoRA 梯度更新、checkpoint、模型效果对照 |
| 用户体验 | 若干后端与测试契约 | 生产签名鉴权/成员授权、可用 Web UI、完整 SSE 幂等重连 |

**结论：S0 本地工程基线已通过；S1 是可测试的纵向工程切片，但没有达到真实企业 RAG 用户旅程的退出标准。**

## 本轮治理内容

- `push` 与 `pull_request` 在旧 workflow 同时触发完整 CI，容易为一次代码修改重复记账。统一改为 **手动 `workflow_dispatch`**：`Quick checks (manual)` 和 `Full integration (manual)`。不满足自动强制合并门槛时，由 reviewer 核对 **最新 HEAD 与手动 CI 结果一致**，不可盲信旧 run；团队如需 required status checks 应单独调整 GitHub 分支保护。
- `FrozenEmbeddingSpec.version` 把 **向量维度** 也纳入 Qdrant 物理空间身份，防止相同模型名称/权重标识但尺寸不同的向量共用旧索引。这是身份版本迁移：开发环境的旧集合保留，只能影子重建/对账，**禁止原地混写或删生产数据**。
- 旧分支已按 GitHub `compare main...head` 检查。9 条历史 `feat/*` / `fix/*` / `test/*` 分支 `ahead_by=0`、对应 PR 已合并；正在开发的 `feat/s1-authorized-page-ranking-capture` 对 main ahead=4（PR #10 开放），不得清理。仅提供手动 `Prune merged branches`，默认 dry-run、需要显式确认才能执行删除；**仅部署清理入口不等于远程旧分支已删除**。

## 唯一后续技术优先级

1. 经过权限、凭据/预算约束，实际运行一次真实 Qwen Embedding / Chat，并冻结模型 ID、权重身份和向量维度。
2. 从已发布的 PostgreSQL + Qdrant 检索真实人工审查页级 Gold，收集 **真实** dev/holdout traces。PR #10 的采集改动以实际合并状态为准。
3. 同数据集比较 Dense 基线与成熟现成 Reranker；GRPO 仅在有稳定可归因的错误模式且收益证据时实现。
4. 优先完成最小鉴权、上传/检索/引用用户旅程，不再增加未经实测的占位模块。

## 当前验证和操作入口

- CI 策略与适用分支、手动操作：[`CI_POLICY.md`](CI_POLICY.md)
- 历史 S0 进度与验证资料：[`project-progress.md`](project-progress.md)
- 本地冻结 Qwen 环境说明：[`validation/s1-frozen-qwen-embedding-2026-10-21.md`](validation/s1-frozen-qwen-embedding-2026-10-21.md)
- 页面级 JSONL 评估口径：[`validation/s1-page-ranking-baseline-2026-10-21.md`](validation/s1-page-ranking-baseline-2026-10-21.md)
- GitHub Actions 最新事实：[workflow runs](https://github.com/musuntana/GRPO-Rag/actions)

不要把 CI 的合成数据通过、自动化测试数量、代码辅助函数视为真实模型效果或生产验收。
