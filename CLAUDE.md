# CLAUDE.md

本仓库的开发入口是 [AGENTS.md](AGENTS.md)。所有 agent 使用同一套领域、架构、阶段和协作规则；本文件不复制另一套约束。

- 架构主事实源：[docs/architecture.md](docs/architecture.md)。
- 领域术语：[GLOSSARY.md](GLOSSARY.md)。
- 当前实施顺序：[docs/development-roadmap.md](docs/development-roadmap.md)。
- 相关验收约束：[docs/development-constraints.md](docs/development-constraints.md)。
- 多 agent 协议：[docs/multi-agent-development.md](docs/multi-agent-development.md)。
- 实现草图：[docs/implementation.md](docs/implementation.md)，属于历史材料。

## 当前命令与状态

以下命令来自现有 Makefile，存在命令不代表当前安装/启动已成功：

| 命令 | 用途 |
|---|---|
| `make install-dev` | 创建 .venv 并安装开发依赖 |
| `make lint` / `make test` | Ruff / pytest |
| `make api` / `make worker` | API / workflow worker 入口 |
| `make up` / `make down` | 开发基础设施启动/停止 |
| `make migrate` | 数据库迁移 |

2026-10-09 的源码基线仍有包注册、基础类型和模型接线缺口，见 roadmap S0；Makefile 尚无 ingest-worker/train-worker target。未经用户相应授权不要运行部署、删除数据或付费训练。不得把历史通过记录或 stub 响应作为当前完成证据。
