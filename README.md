# RagOS / GRPO-Rag

面向企业复杂文档的多租户 RAG 与 AI workflow 项目，采用模块化单体和隔离 worker。近期聚焦可复现的证据检索、排序及 Claim–Citation Verification：先建立真实 baseline，再由错误分析决定 SFT、GRPO 或多模态是否值得引入。

截至 2026-10-09，代码基线 `d5a2709814b4` 仍处于未完成集成状态，不能按目录/文件存在认定完整 MVP 或训练成功。基础类型、ORM/迁移、向量读写与安装注册存在缺口；模型 gateway 仍是 stub，没有真实训练闭环或前端应用。当前任务是 S0 修复可运行性。

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

当前 Makefile 有 `install-dev`、`lint`、`test`、`api`、`worker`、`up`、`down`、`migrate`。包声明 Python >=3.11，实际支持版本和锁定依赖应在 S0 验证。`.env.example` 和 `infra/docker/docker-compose.dev.yml` 是本地开发配置，不能直接作为生产配置。

完整可运行 quick start 应在 S0 实测后补齐；当前不承诺这些命令已能串成成功流程。旧 [implementation](docs/implementation.md) 的示例与历史勾选不是当前验证记录。

## 变更习惯

先领域和架构，后接口与实现；每个文件只有一个写入 owner。内部旧契约优先一次性迁移并删除，持久数据需可验证迁移与回退。每次交付记录实际行为、验证边界、观测信号和回滚路径。没有真实实验数据，不写性能提升或训练收益数字。
