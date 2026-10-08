# AGENTS.md

本文件是所有开发 agent 的入口，适用于整个仓库。遵守用户本次授权；读材料、制定规范不自动授权部署、付费训练、删除持久数据或修改其他人的工作。

## 1. 目标与阅读顺序

RagOS 是面向企业复杂文档的多租户 AI workflow / RAG SaaS，采用模块化单体与隔离 worker。近期主线是可复现的 Evidence Retrieval、Reranking 与 Claim–Citation Verification。算法升级必须由冻结评测与错误分析驱动。

开始工作先检查 Git 状态、当前阶段和相关代码，再按任务阅读：

| 文档 | 唯一职责 |
|---|---|
| [architecture](docs/architecture.md) | 架构、模块职责、数据流与存储选择的主事实源 |
| [GLOSSARY](GLOSSARY.md) | 稳定领域术语及业务含义 |
| [development-roadmap](docs/development-roadmap.md) | S0–S5 开发顺序、阶段退出条件和当前阻断 |
| [development-constraints](docs/development-constraints.md) | 领域接口、安全、并发、前端及变更验收约束 |
| [multi-agent-development](docs/multi-agent-development.md) | 任务拆分、写入所有权、依赖顺序、交接和集成 |
| [ADRs](docs/adr/) | 重要决策的原因、代价与修订记录 |

`docs/implementation.md` 是旧版实现草图，不再决定开发顺序；`docs/competitive-survey.md` 是背景材料，不证明本项目能力。遇到冲突先定位权威文档和现有行为，再在当前授权内更新契约及受影响文档，不自行发明第二套语义。代码未遵守规范时记录缺口并按阶段修复，不能把待办写成已实现。

## 2. 开发顺序

按 [roadmap](docs/development-roadmap.md) 执行：

`S0 修复可运行性 → S1 打通真实 RAG → S2 修正评测与身份边界 → S3 实现 SFT → S4 GRPO 对照实验 → S5 产品与运维扩展`。

- 安全隔离、明确领域模型、基础可观测性和相关前端体验从 S0 起约束所有变更；S2 是加固与验证，不能成为延迟鉴权的理由。
- 先建立可测 baseline，再分别定位解析、召回、排序、生成和引用瓶颈；没有证据不默认开启 HyDE、agentic、多模态或 GRPO。
- SFT 是可交付训练基线；GRPO 是可被否定的实验。缺少真实权重、加载验证和独立评测时，不称训练完成。
- 从 S1 起交付完整的最小用户旅程。S5 扩展功能与运维深度，不补救早期不可用的前端。

## 3. 不可突破的架构边界

1. 保持一个代码库、薄入口和共享业务 Module；没有测量和架构决策不拆微服务。
2. HTTP handler 负责请求校验、认证依赖和响应映射；业务经 service / runtime 与 repository，不直接查库或编排 provider。
3. 每个租户操作先取得可信 `tenant_ctx`。缺少身份或范围必须拒绝，不能转为全局查询。
4. 平台管理与租户访问使用不同权限入口；跨租户操作只在显式特权流程审计执行，不能使用 nullable tenant 作为绕过。
5. Qdrant collection 按 embedding profile 与物理 tier 组织，不按租户创建；所有读写、删除、计数、导出都校验范围。
6. PostgreSQL 只保存控制面、搜索、任务信封、摘要与 checkpoint；大文件、原文、详细 trace、逐 token 日志保存在范围隔离的文件/对象存储。
7. Valkey/Redis 只承担瞬时调度、pubsub、锁、租约、限流和缓存；持久状态不以队列为唯一来源。
8. 企业 KB 与用户记忆分开存储、召回及生成上下文。用户记忆不成为企业事实的金标准。
9. 所有 chat / embed / rerank / judge 模型调用通过 model gateway，包括离线任务；训练优化过程在隔离训练运行时，在线推理经 gateway。
10. Script 在隔离 runner 执行，不能进入 API 或 worker 主进程；HTTP/Script/MCP 共用受控出站策略，默认无网络。
11. 大结构或业务不变量变更先读并更新 architecture / glossary；难逆、存在真实权衡的决策记录 ADR。

## 4. 领域与代码设计

- 先定义领域对象、所有者、合法状态变化和不变量，再设计表、端点和页面。复用已确认的业务语义，禁止按 agent 或功能页面复制一套状态/权限/证据定义。
- 优先深 Module：小而明确的 Interface 隐藏真实复杂度；Interface 同时说明输入、输出、不变量、错误、幂等、资源和时间约束。
- 在真实变化点设置 Seam，通过 Adapter 接存储、provider 与解析器；不为假想的第二实现建立万能框架或仅转发的层。
- 不用循环依赖、全局 tenant/session、任意字典穿透或动态属性补丁隐藏契约缺口。依赖显式注入，业务计算与副作用可分开验证。
- 保持 `src` 布局：目录 kebab-case，Python import 来自 `src/`。新增包同步注册安装配置，验证安装产物而非仅修改 PYTHONPATH。
- 改共享契约时同批对齐类型、迁移、repository、service、API、前端和测试；禁止各 agent 创建互相兼容的平行契约。

## 5. 安全、隔离与状态

`tenant_ctx` 至少具有 tenant_id、tenant_slug、actor_id、actor_type、roles、plan_tier、locale、timezone、trace_id；范围与用户身份由服务端认证和成员关系派生。brand、KB、model binding 是经校验的资源引用，不是客户端扩大权限的声明。`scope_key` 的范围约束见 [ADR-0005](docs/adr/0005-server-derived-scope-key.md)。

- verified token / host mapping 仍需成员和资源授权；dev token、stub、Noop 仅用于显式本地测试，生产配置拒绝启动。
- repository 和 service 校验 tenant/resource/actor 范围；RLS 是第二道防线。连接池、worker 和重放路径同样必须设置范围。
- 密钥按别名解析、最小范围注入，不能出现在日志、前端、workflow 或训练产物；未知身份、预算、秘密或隔离能力失败时拒绝执行。
- 租户数据、历史消息、检索文本和工具结果是不可信输入，不能覆盖系统策略或执行权限。
- 并发上限、超时、取消、背压、重试分类和资源释放显式定义；只对可安全重试的瞬时故障重试，副作用采用幂等和持久状态跟踪。
- worker 领取限定其已注册任务类型，支持过期租约回收和旧执行者失效；失败、超时、取消有确定终态。
- PG 与 Qdrant 分别提交；持久 manifest 先记录计划，幂等双写、对账成功后才 `indexed`。不能宣称跨库原子事务。

## 6. 检索、训练与工作流

- 可稳定分页的来源使用 page parent + 页内语义 children；非页化来源使用 token-window parent，按索引版本冻结。页号、文档版本、字符跨度贯穿检索、标注、训练、引用与 UI；DOCX 只有固定渲染版本才有稳定页码。
- 子块按 256–512 token、非页化父块按 1024–2048 token 作为初始 profile 参数；表格等结构不破坏，oversize 元素显式处理。参数及 contextual enrichment 选择可配置、可评测，不把参考数值当最佳实测。
- contextual enrichment 默认策略由 KB/profile 管理，批量 embedding 经 gateway，保留原始文本供引用；baseline 可显式关闭以做对照。
- fusion / rerank / gate 可版本化；生成前必须经过 CRAG。ambiguous 最多按预算重检并重新判定；irrelevant 拒答，未知/故障不能装作有证据。
- rerank 后做父块扩展并受上下文预算约束。引用结构验证默认开启；语义验证在评测中必需，在线按 profile 启用，UI 显示真实验证状态。
- train 用于采样、mining、训练 reward；dev 用于选择与调参；holdout 只用于冻结候选晋级。未知标签不是负例，不以模型自评当 gold。
- Workflow 存 JSON DSL，编译为 LangGraph；状态 typed，副作用可幂等恢复。每个节点定义 timeout/retry/permission/audit/UI trace。
- 非测试部署使用 PostgresSaver / AsyncPostgresSaver，启动 setup；thread_id 为 `{tenant_id}:{workflow_id}:{run_id}` 且绑定可信 actor/resource。默认 per-invocation subgraph，必要的 per-thread subgraph 禁止同 namespace 并行。
- streaming 使用受版本验证的 LangGraph 事件接口；HITL 恢复必须显式 POST，GET/HEAD 不改变状态。对瞬时异常应用有界 RetryPolicy，错误写入状态并路由处理。
- 三层记忆分别是 run 内工作记忆、会话 buffer/rolling summary、用户事实；异步提取与衰减必须可恢复并验证用户隔离，不用 scheduled 冒充 updated。

## 7. 前端与 API

- 每个用户可见变更包含成功、loading、空、无权限、失败、重试/取消和断线恢复状态；隐藏未接通能力，不能制造按钮、成功提示或模拟实时 token。
- 页面只消费稳定业务 DTO/事件，认证与证据身份由后端决定。以服务端状态恢复，重试不重复生成或计费；切换租户/会话清空对应缓存和流订阅。
- 保持导航、键盘/焦点、可读错误、移动布局和引用原页定位；大文档、长列表、长流有内存/并发预算。涉及 UI 的任务必须走实际浏览器旅程验证。
- 路由集合使用复数名词，多词路径用 kebab-case；API 保持 `/platform/*`、`/tenant/*`、`/chat/*` 权限区别；分页统一为 page/page_size（20/100 为初始默认/上限）；create/update/delete 返回 201/200/204，异步接受用 202。既有偏差列入一次性迁移，不增加长期双协议。
- service 抛以 RagOSError 为根的领域异常，单一 FastAPI handler 按 [异常表](docs/development-constraints.md#21-api-与领域异常) 映射状态码，错误统一 `error.code/message/details`；X-Request-Id 与 trace_id 一致。错误不泄露秘密、内部路径或其他租户数据。
- SSE 使用同一个 run_id 和持久单调 event_id，Last-Event-ID 只重放原 run，不重做副作用；失败/取消必须结束流。

## 8. 多 agent 与变更完成标准

- 并行前由集成负责人冻结任务目标、允许路径、Interface、依赖、测试和非目标；同一文件只有一个写入 owner。执行协议见 [multi-agent-development](docs/multi-agent-development.md)。
- 子任务完成须交接代码/文档、验证证据、已知风险及回滚路径；发起 agent 不以“worker 已启动”或单个 mock 测试通过宣称整体完成。
- **删除优于兼容**：消除过时入口、别名、fallback、死代码和重复测试；内部破坏性改动同批迁移所有调用者。持久数据和真实外部消费者需明确迁移/回退，删除代码不等于删数据。
- 完成必须覆盖相关层：安装/导入、业务不变量、跨范围负例、故障并发、真实集成、UI 旅程、算法冻结评测；按风险选择必要检查，不把测试数量当质量。
- 基线失败与新失败分开记录；不能通过放宽权限、指标口径、跳过测试或伪造产物让结果变绿。
- 每次变更说明为什么、实际行为、验证范围/结果、未验证部分、观测与回滚。数据集、代码、profile、模型、parser、chunker、prompt、judge 等版本可追溯。
- 文档随契约更新；难解释的决策保留原因和取舍，注释说明业务不变量。禁止暴露原始私有推理链。
- 不擅自 commit/push、部署或远程发消息；按用户明确授权执行。遇到工具权限阻断继续不受影响的工作，准确报告阻断。
