# RagOS / GRPO-Rag

企业复杂文档 RAG 与 Agent Runtime 的工程化实验仓库。当前路线：先使文档入库、检索、证据引用与评估可复现，再根据真实结果决定是否训练 SFT/GRPO。

> **唯一当前基线**：[docs/CURRENT_BASELINE.md](docs/CURRENT_BASELINE.md)。历史过程与日志在 `docs/project-progress.md`、`docs/validation/`，不再把历史测试数、开发分支或旧里程碑当成最新 HEAD 状态。

## 已交付与尚未完成

- **已具备测试证据**：PDF 文本页解析、PageChunker、冻结证据与索引身份、PostgreSQL 索引准备/CAS 发布、Qdrant HTTP 服务端写入和客户端重连、租户过滤与证据回填、OpenAI 兼容 API 传输、本地显式 Qwen Embedding 探测入口，以及页面级 JSONL 离线评估器。
- **未完成 S1 生产用户旅程**：生产级签名与成员授权、Model Gateway 可信解密/模型准入/计量、上传 API、持久 Qdrant 节点重启恢复、完整前端、真实 Qwen 全链路指标、扫描件/复杂表格、多模态、可恢复问答 SSE。
- **未实现模型训练闭环**：SFT / GRPO 辅助数据和奖励函数不等于训练完成；没有本仓库可复现的权重更新、LoRA checkpoint 或真实指标提升证据。

## 快速开始

推荐 Python 3.12 和项目锁定依赖；Mac M1 可以运行本地合成探测，生产部署另行设计：

```bash
git clone https://github.com/musuntana/GRPO-Rag.git
cd GRPO-Rag
uv sync --frozen --no-editable --extra dev --python 3.12
.venv/bin/pytest -q tests/test_page_eval_baseline.py
.venv/bin/python -m ragos.retrieval.offline_eval \
  examples/eval/synthetic-page-trace.jsonl --output /tmp/page-rank-report.json
```

样例是合成数据，不代表实际模型检索性能。需要真实 PostgreSQL、Qdrant 的集成测试时，仅连接**可销毁的独立测试数据库**，参照 [CI 策略](docs/CI_POLICY.md) 和 [环境约束](docs/development-constraints.md)。

## 开发约定与文档入口

| 权威入口 | 作用 |
|---|---|
| [CURRENT_BASELINE](docs/CURRENT_BASELINE.md) | 当前 HEAD、能力/阻塞、已核验 CI、接下来唯一优先事项 |
| [AGENTS](AGENTS.md) | 安全、权限、开发约束 |
| [GLOSSARY](GLOSSARY.md) | 统一领域语义 |
| [architecture](docs/architecture.md) | 数据流和模块边界 |
| [development-roadmap](docs/development-roadmap.md) | S0–S5 目标、退出条件 |
| [CI_POLICY](docs/CI_POLICY.md) | **CI 只按需手动触发**、合并准入、旧分支清理 |
| [ADR](docs/adr/) | 架构决策 |
| [validation](docs/validation/) | 有日期的历史验证原始材料 |

## 省额度的验证流程

**GitHub Actions 不再因 push、PR 编辑或合并自动运行**。先在本地执行改动对应的测试；准备合并时到 Actions 手动运行 `Quick checks (manual)`，涉及持久化/权限/模型/跨模块时再运行 `Full integration (manual)`。在结果通过且与最新 PR HEAD 对齐后再合并。详见 [CI_POLICY](docs/CI_POLICY.md)。

历史文档 `docs/implementation.md` 与 `docs/next-step-instructions.md` 仅供参考，不是当前事实或执行命令的唯一来源。没有实测数据，就不宣称 GRPO 提升、生产可用或端到端成功。
