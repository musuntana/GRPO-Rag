# RagOS / GRPO-Rag

面向企业复杂文档的多租户 RAG 与 AI workflow 项目，采用模块化单体和隔离 worker。近期聚焦可复现的证据检索、排序及 Claim–Citation Verification：先建立真实 baseline，再由错误分析决定 SFT、GRPO 或多模态是否值得引入。

截至 2026-10-09，S0 已通过本地退出验收：标准安装、领域与存储契约、索引读回发布、任务生命周期及实际临时数据库验证已完成。下一步进入 S1 真实 RAG。模型 gateway 仍是 stub，生产签名鉴权、真实模型与完整前端尚未接通；不能认定完整 MVP 或训练成功。

## 开发入口

| 阅读内容 | 作用 |
|---|---|
| [AGENTS.md](AGENTS.md) | 所有 agent 的统一入口和不可突破规则 |
| [GLOSSARY.md](GLOSSARY.md) | 身份、证据、训练与运行的稳定业务语义 |
| [架构](docs/architecture.md) | Module 职责、数据流、存储与隔离 |
| [开发顺序](docs/development-roadmap.md) | S0–S5 目标、退出条件、证据记录 |
| [开发约束](docs/development-constraints.md) | 安全、并发、前端、验证、观测与回滚 |
| [多 agent 协作](docs/multi-agent-development.md) | 所有权、任务合同、依赖、交接与集成 |
| [ADR](docs/adr/) | 重要架构决策与修订原因 |

开发顺序：修复可运行性 → 打通真实 RAG → 修正评测与身份边界 → 实现 SFT → GRPO 对照实验 → 产品与运维扩展。安全与隔离从一开始约束开发，最小真实前端旅程在 S1 交付；不会等到最后补 UI。

## 本地入口

当前 Makefile 有 `install-dev`、`lint`、`test`、`api`、`worker`、`up`、`down`、`migrate`。包声明 Python >=3.11；当前锁定安装已验证 Python 3.12，其他版本未验证。`.env.example` 和 `infra/docker/docker-compose.dev.yml` 是本地开发配置，不能直接作为生产配置。

S0 已验证非 editable 标准安装与全部模块导入，真实临时 PostgreSQL 的 0001–0012 迁移及 ORM 对照。具体结果见 [推进记录](docs/project-progress.md)。这不表示真实 RAG、workflow 或训练可用；三个 worker 入口在访问数据库前明确拒绝尚未接通的能力。

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install '.[dev]'
pytest -q
```

真实存储测试只在显式指定已迁移的**独立临时数据库**时执行：设置 `RAGOS_TEST_PG_DSN`（SQLAlchemy `postgresql+psycopg://...` 格式），再运行 `pytest -q tests/test_storage_integration.py`。未设置时会明确跳过，不能作为真实存储通过。`alembic upgrade head` / `alembic check` 使用 `RAGOS_PG_DSN`。

`python scripts/verify_s0_processes.py` 检查安装模块、不可用 worker 的拒绝和 API 实际 lifespan；它会 setup checkpoint 表，只用于临时库。生产配置拒绝启动，因为签名鉴权和成员授权尚未接通。旧 smoke 脚本依赖默认 log-only/Noop 成功，已删除。

默认 Qdrant URL 为空；真实连接由环境明确配置。hash embedding 不默认启用。旧 [implementation](docs/implementation.md) 的示例与历史勾选不是当前验证记录。

## 变更习惯

先领域和架构，后接口与实现；每个文件只有一个写入 owner。内部旧契约优先一次性迁移并删除，持久数据需可验证迁移与回退。每次交付记录实际行为、验证边界、观测信号和回滚路径。没有真实实验数据，不写性能提升或训练收益数字。

S0 的最新本地验收见 [推进记录](docs/project-progress.md)：当前全量 775 项测试通过，真实临时 PostgreSQL 迁移至 0012、ORM 对比和空库回退/重升级通过，全新冻结安装的 API lifespan/checkpoint setup 通过。默认未接通的 worker 明确不可用；显式 test hash 只验证机械索引，不证明真实模型检索。生产鉴权、真实 provider 和 S1 用户旅程尚未实现，本轮未部署。

向量集合名称冻结 profile、embedding version 与 tier；旧集合保留，重建并对账后才切换。历史消息的引用状态由服务规范化；缺少来源和核验事实时显示 unverified，不修改保存的历史数据。

下一轮可直接使用 [S1 推进指令](docs/next-step-instructions.md)，先冻结真实模型、数据与调用预算合同，再交付真实入库、问答和引用阅读的最小用户旅程。
