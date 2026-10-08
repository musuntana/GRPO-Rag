# 开发约束与验收契约

更新：2026-10-09。这里定义未来变更必须满足的约束，不表示当前代码已满足。架构归属见 [architecture](architecture.md)，术语见 [GLOSSARY](../GLOSSARY.md)，顺序见 [roadmap](development-roadmap.md)，并行规则见 [multi-agent-development](multi-agent-development.md)。

## 1. 从领域问题到 Module

先给出业务情境、输入输出、资源所有者、合法状态与失败含义，再决定代码位置。例如：“同一租户的另一用户能看共享 KB，但不能读取该会话的私有记忆”，必须分别建模共享资源权限和用户作用域，不能靠同一字符串过滤所有数据。

每个有外部调用者的 Module 应有小而明确的 Interface，包括：

| 契约项 | 要明确的内容 |
|---|---|
| 业务输入/输出 | 规范术语、对象身份、版本、单位；DTO 不暴露 ORM 或 provider 原始对象 |
| 不变量 | 所有者、合法状态、可用资源、权限与一致性要求 |
| 失败 | 领域错误码、是否可重试、对持久状态的影响 |
| 副作用 | 幂等范围、提交点、重放策略、补偿/对账责任 |
| 运行约束 | deadline、并发/队列上限、取消、连接/文件/设备释放 |
| 观测与验证 | 关联标识、失败信号、可从同一 Interface 执行的验证 |

由 Module 的 Implementation 吸收复杂度，不把调用顺序、事务细节、provider 例外散布到调用者。只有真实变化点需要 Seam；Adapter 实现存储、解析、模型等外部契约。避免只转发参数的类、万能 registry、跨包互相导入和不确定用途的抽象。

业务权限、证据身份、任务状态、指标定义各有一个 owner。前端通过 DTO 复用这些业务语义，不能从展示文本推断权限、从 badge 推断任务状态、从 URL 拼出证据所有权。跨 package 的公开契约用显式类型；可扩展 metadata 不能替代决定权限/版本/晋级的结构化字段。

## 2. 依赖与事务责任

目标依赖流由 [architecture §0](architecture.md#0-当前目标实施状态与文档职责) 固定：

`入口/传输 Adapter → 应用 service/runtime → 领域 Module 的 Interface → 存储/模型/文件 Adapter`。

- API 不查 storage、不调用 provider SDK，不自行实现 ranking/mining；worker 只组装依赖、领取和驱动任务。
- retrieval / memory 提供可注入调用的业务计算，runtime 组装 orchestration；storage repository 不依赖 runtime、HTTP、UI。
- model-gateway 持有 provider Adapter；训练运行时只优化模型，训练中的模型辅助调用和上线推理仍经过 gateway。
- core 只容纳跨域的基础契约；不把 KB、memory、billing 全搬入 core 或 utils。
- 应用 service/工作单元拥有业务事务，repository 执行查询/写入且不偷偷 commit。请求依赖、RLS set_config 和 service 必须约定唯一事务 owner，禁止 read/autobegin 后再次无条件 begin。
- model/network 调用不持有长时间 DB 锁；准备、领取、外部执行、提交结果分开，并通过持久状态和执行代次保护竞态。
- Migration、ORM、repository、API DTO、索引 payload 是同一领域契约的不同映射，必须同批更新并验证。不同存储的副本谁是权威、何时可读、如何修复必须写明。

### 2.1 API 与领域异常

资源集合用复数名词（保留已有 `/tenant/kb` 约定），多词路径用 kebab-case；权限分区是 `/platform/*`、`/tenant/*`、`/chat/*`。分页 page/page_size、状态码和 SSE 约定遵循 AGENTS；一次变更迁移既有偏差的全部调用方，不长期保留两种分页语义。

业务 service 抛以 `RagOSError` 为根的领域异常，单一 FastAPI exception handler 负责传输映射，HTTP handler 不自行发明业务错误。下表是目标契约，不能据表认定所有类已接通：

| 领域异常 | HTTP | 含义 |
|---|---|---|
| AuthenticationError | 401 | 身份缺失或无效 |
| NotFoundError | 404 | 授权可见范围内未找到资源 |
| ConflictError | 409 | 业务版本/状态冲突 |
| ValidationError | 422 | 输入或领域约束不满足 |
| PermissionDeniedError | 403 | 没有执行该操作的权限 |
| QuotaExceededError | 429 | 配额耗尽 |
| ProviderError | 502 | 模型/外部提供方失败 |
| SandboxError | 500 | 隔离执行失败 |
| TimeoutError | 504 | 操作超时 |

响应为 `{"error":{"code":"...","message":"...","details":{}}}`，code 稳定、message 可读、details 只包含允许公开的字段。原始异常、SQL、stack trace、内部路径、secret 和其他租户信息只进入适当脱敏的内部记录；X-Request-Id 与 trace_id 可用于定位。安全拒绝不能为追求统一错误而暴露资源存在性。

## 3. 身份、范围与安全默认

可信链路为“验证身份 → 成员/角色授权 → 校验资源归属与 ACL → 派生上下文 → 执行业务”。Host/域名用于选择租户，不能独立授予成员资格；可信 token claim 也不能自动授予其未拥有的资源。

| 资源 | 所需范围 | 必须拒绝的情况 |
|---|---|---|
| Tenant / Workspace / KB / 文档 | 已认证租户成员 + 资源 ACL | 其他租户、未授权 workspace/KB、伪造 body scope |
| 会话 / 消息 / memory fact/summary | 租户范围 + 明确用户/会话所有权 | 同租户另一用户、只校验 tenant、不存在 actor user |
| 训练/评测数据及产物 | 租户/授权 workspace + 所有数据来源与用途权限 | 训练 run 与数据/产物 owner 不一致、跨租户模型复用 |
| 平台管理 | 独立 platform_admin 权限 + 审计 | 将 tenant_admin 当 platform_admin、缺 tenant 自动走平台权限 |
| Worker / replay / 导出 | 服务身份 + 任务权威记录重建的资源范围 | 信任任意任务 payload 身份、缓存/路径缺范围 |

`scope_key` 是可信范围的可编码标识，不是授权本身。共享 KB 使用经过 ACL 校验的租户/资源范围；私有记忆、会话再限制 user。缓存、prompt cache、幂等、文件、vector filter 与事件 replay 必须携带所需范围。不能省略范围或只依赖一个未验证前缀。

默认开启/拒绝规则：

- 正常运行验证 token 签名、issuer、audience、有效期和受信角色；dev token、Noop、hash embedding、空 StubProvider 只在显式 local/test profile 可用，生产启动拒绝。
- 所有对象 read/write/delete/count/export/replay 均授权。RLS 作为第二道保护，范围在同一业务事务设定；worker、连接池复用、回滚路径也有负例。
- identity、secret、硬预算、隔离策略缺失或不可验证时 fail closed；限流降级只能按记录的平台策略启用，不能降级权限/预算/网络隔离。
- 出站默认拒绝，允许域名经解析/重定向及出口策略复核；HTTP、Script、MCP 不能各造一套可绕过的客户端规则。
- Script 执行与网络隔离独立实施；隔离环境不可用就失败，不回退到 API/worker 内执行。
- secret alias 只为本次调用解析，按租户、用途、时限最小注入，存储加密并有轮换路径；日志、UI、错误、trace 和模型产物不带原密钥。
- 文档/检索文本/历史消息/工具输出是不可信数据；prompt injection 不得修改系统指令、读取范围或工具授权。上传文件验证类型、大小、解压预算和安全路径，渲染 HTML/Markdown 防 XSS。
- runtime 产生的 actor、tenant、run、cost、权限信息不能被模型输出覆盖；每次 provider fallback 重新校验 entitlement 和预算。

## 4. 文档、索引与证据身份

同一 doc_id 的另一版本或另一页不是同一 evidence。跨入库、检索、gold、mining、training、citation、原文阅读器复用以下目标契约：

- 文档以 doc_id + document_version 表示不可变内容，内容摘要校验版本；页证据由文档版本 + page_no 标识，span/chunk 继续标识精确内容。
- 稳定分页的 PDF/PPTX 使用 page parent；DOCX 只有冻结渲染版本才允许真实 page_no；不稳定分页来源用 section/token-window，不伪造页码。
- parent_unit、parser/chunker/embedding 版本与索引 manifest 绑定，不能由某个 endpoint 临时判断。同一个 chunk_id 只能解析到同一版本内容。
- 表格表头与行关系、列表、代码和图注不可被静默切坏；超预算元素标为 oversize，保留布局并采用明确处理策略。
- document-global 和 page-relative offsets 分别定义坐标系，原文与 enriched 文本分开。不能在改变文本后复用原偏移。
- Qdrant collection 的 profile、维度、distance、point id、payload、filter、PG hydration 字段由统一契约生成；不混模型/维度，不各端手工拼 filter。
- 持久 indexing manifest 先提交计划，再进行幂等 FTS/vector 写入；只有两端核对成功才标 indexed。部分失败可重试/修复；失败版本不可被正常检索使用。
- 删除、重建、权限撤销和 TTL 到期也必须同步两索引与缓存。reindex 用独立版本/影子索引，验收后原子切换，保留回退指针而不无限保留旧代码。
- child 召回后按 profile fusion/rerank，父块扩展后控制上下文预算。候选、被选证据、扩展父块和真实送入 generator 的内容分别可追溯。

## 5. 检索、引用与算法证据

Baseline 先跑最小直连链，固定文档、query、gold、profile 和模型版本。只在错误分析指向特定瓶颈时升级：gold 未召回才改解析/chunk/embedding/hybrid；高候选 recall、低 evidence hit 才改 rerank；证据正确答案错才改生成；答案/引用错位则修 attribution。

- 新默认配置必须比较相同冻结数据和版本，记录消融、失败分布、延迟与成本；参考数字、mock、手工规则不是 benchmark。
- CRAG 在生成前必需。ambiguous 采用有预算的重检并重新判定；仍不足时拒答。judge/provider 故障明确报错，不能当 irrelevant 或成功。
- 引用结构验证检查范围、文档版本、证据集成员、页号、span 边界；评测包含 claim 与 evidence 的语义验证。
- 在线语义验证按 profile/预算启用；结构有效不等于语义 supported。DTO/前端区分结构状态、语义状态和 suppressed，未运行 judge 要显示“未做语义验证”。
- 数值题、跨页、同模板不同年份、表格、无答案、权限撤销等类别单独留结果；page-only、doc-only、chunk-level 指标不能混名。
- Recall/Hit/MRR/NDCG 的相关标签、去重粒度、截断 K、分母与方向显式定义；空集、缺指标、NaN、case/version/scope 错配拒绝晋级。无可用分母报告不可计算，不伪造满分。

训练数据与实验约束：

| split | 唯一用途 | 禁止 |
|---|---|---|
| train | mining、sampling、训练 reward、梯度更新 | 从 holdout/未知标签直接生成负例 |
| dev | 选择模型、阈值、reward、超参 | 用 dev 提升替代最终泛化结果 |
| holdout | 冻结候选最终评测/晋级 | mining、调参、训练奖励；看结果后反复选择候选 |

按文档版本、来源模板/近重复和必要的时间边界分组划分，记录泄漏检查。未标注候选要先审核，grade 1 与 grade 0 分开，不能把“不是 gold”直接当“不相关”。反馈是审核输入，不自动改 gold。holdout 被用于选型或反复调参后应标记已消耗并重新建立独立集。

真实 SFT 必须产生可加载权重及 manifest；GRPO 必须明确可采样动作、参考策略、objective、KL 估计、reward 与多 seed 等预算对照。训练轮数/退出码/奖励提高不能代替 holdout 质量。训练集改善正常，但不是泛化证明。

产物登记与晋级校验 tenant、数据授权、非空指标、完整 manifest、digest、加载能力及 profile 版本；用原子状态变化避免并发同版本/重复晋级。上线引用不可变产物，不允许换权重而复用版本。

## 6. 并发、故障与可恢复性

默认假设请求会重发、worker 会崩溃、队列会重复/丢通知、用户会断线、provider 会超时、PG/Qdrant 只有一端成功。设计保证可恢复的业务效果，不能承诺跨系统 exactly-once。

| 触发 | 应有的行为 | 验证点 |
|---|---|---|
| 重复提交/双击/超时后重发 | 服务端限定范围的幂等记录返回同一业务结果；同 key 不同 payload 拒绝 | 不重复创建 run、收费或晋级 |
| Worker 崩溃/租约超时 | 可回收 leased 状态，心跳续约；执行代次失效的旧 worker 不能提交 | 回收后只有新 owner 的结果生效 |
| 多类型 worker 并行 | 只领取已注册 kind；限并发并隔离训练/入库/交互资源 | 不抢走无 handler 任务、不饿死交互请求 |
| 单端索引失败/进程中断 | manifest 留下权威状态，幂等重试/对账，版本不可见或降为明确故障 | PG/vector 一致后再发布 |
| Provider 瞬时/永久失败 | deadline 内分类重试，指数退避+jitter；永久错误直接终止 | 重试不超预算、不泄漏秘密、不重复副作用 |
| 流消费者慢/断线 | 有界队列、背压、持久游标；独立 run 与 stream 生命周期 | 内存有界、replay 不重新运行 |
| 取消/过期/权限撤销 | 持久取消意图及授权复核，释放资源，禁止后续副作用 | UI/服务端终态一致；撤销后不能导出/replay |
| 并发会话/摘要更新 | 每会话序列或乐观版本冲突检测，memory writeback 按版本幂等 | 不丢消息、不覆盖新 summary、不混用户 |

run_id 从创建、任务、日志、消息、计费到 UI 使用同一身份。event_id 在同一 run 内单调且并发写安全；先持久化再发布，replay 检查 actor/resource。GET/HEAD replay 不执行模型或变更工作流；HITL resume 用授权 POST 和一次性状态变更。

事务边界、连接池容量、文件锁/单写者、模型设备、队列长度都有上限和释放责任。超时、失败和取消要有可解释终态，SSE 即使 producer 出错也发终止/关闭并持久状态。业务 error 不伪装成空列表/空答案。

## 7. 完整且稳定的前端体验

当前没有 `apps/web`。新旅程从 S1 采用薄 Web UI 或服务端页面打通，再按阶段演进；不能宣称 UI 已存在。技术栈按实际需要选，不为此规范创建新服务。

每个用户可见能力的验收矩阵：

| 旅程 | 至少覆盖的状态 |
|---|---|
| 选择租户/KB/文档 | 授权可见、空集、loading、失效选择、权限撤销、切换隔离 |
| 上传/解析/入库 | 文件校验、进度、排队、处理中、indexed、失败/重试、取消 |
| 问答 | 新会话、多轮、发送中、真实流/明确非实时响应、成功、拒答、失败、重连、取消 |
| 引用阅读 | 原文版本/页/span 定位、语义验证状态、证据缺失/已删除/无权限 |
| 评测/模型发布 | 执行中、失败、不可比较、未达门槛、已晋级、回滚可确认 |

UI 根据服务端 DTO/状态展示，不能自己推断 indexed、verified 或 trained。浏览器订阅在切换/卸载时关闭，旧请求响应不覆盖新会话；恢复 cursor 防重复 event，跨标签页重试不重复 run。刷新后从服务端恢复，不能以本地内存作为任务唯一状态。

保持统一导航和主操作、可读错误与恢复动作、表单输入保留、焦点/键盘、合理的移动布局。权限拒绝与不存在资源按安全策略展示，不泄露资源内容。长列表分页/虚拟化、超长回答分段、原文预览按需加载；错误边界不使整站白屏。

前后端契约改动同批完成：DTO/事件 version、前端消费者、状态文案、浏览器测试/截图。仅截图证明外观，仅 mock UI 证明状态展示；真实成功/失败/断线旅程才证明集成。检查网络重复请求、控制台错误与卸载后的资源泄漏，不用动画掩盖后端未接通。

## 8. 可验证、可观测、可回滚

| 变更类型 | 相关最低验证 | 观测/回滚 |
|---|---|---|
| 纯文档 | 链接、术语、阶段/契约冲突、diff | 明确规范与事实；可恢复文件 |
| 公共类型/包/接口 | 安装产物导入、调用方契约、全量收集 | 记录版本，完整调用方同时回退 |
| Tenant/auth/storage | 跨租户和同租户跨用户负例、真实事务/RLS、迁移一致性 | 审计、拒绝码；应用/数据/schema 匹配的恢复 |
| Task/stream/network | 取消/重复/崩溃/慢消费者/重连/永久故障 | 状态、attempt/lease/retry/队列信号；暂停领取/撤回 profile |
| Retrieval/training | 确定规则反例 + 冻结真实数据集、逐 case/分层评测 | 数据/profile/model/代码 digest；旧产物/索引指针回退 |
| 用户界面 | 状态矩阵、浏览器真实旅程、键盘/移动、恢复 | run/request id 可关联；界面与服务契约同版本回退 |

日志记录 trace_id、run_id、task_id、租户范围的安全标识、版本、阶段、error.code、时间和真实 token/cost。敏感文本默认脱敏/最小化；高基数标识放 trace/log，不无限扩成 metrics label。异步任务延迟、租约回收、孤儿索引、拒答/引用失败、provider 重试、预算拒绝要有信号和负责的处置动作；不暴露原始私有推理链。

回滚区分应用、配置/profile、模型/索引和数据：应用回滚要与 schema/DTO 匹配，迁移要有恢复演练或明确不可逆步骤及备份，重建索引先验收影子版本。不能用一条 git revert 假装能撤销数据库或已发生的外部效果。

## 9. 删除优于兼容与维护上下文

尚无真实外部消费者的内部契约优先直接替换：删除旧 alias、双路径、silent fallback、feature flag、dead code 与过时测试，在同一变更中迁移所有生产者/消费者。不要用 getattr 默认值、广泛 except、空 stub 或不检查 owner 的旧接口托住坏契约。

存在持久数据或真实外部消费者时，先列消费者及数据版本，设计一次性迁移与可运行回退。如果必须临时兼容，要记录 owner、受影响版本、使用量信号、删除条件和截止点；达成后删除。禁止无限期双写/双语义，也禁止删除数据库、历史证据、tenant 数据来绕过迁移。

维护者上下文记录：业务不变量及原因、重要替代方案及取舍、数据/schema/profile 版本、必要验证证据、故障定位和回滚方法。Glossary 保持定义简短，architecture 保持数据流，ADR 只记录重要决策；不要把会话流水账或互相冲突的计划堆进规范。

合并前由集成负责人给出“实际改变/验证成功/已知基线失败/未验证/观测/回滚”的结果。外部资料、示例表和愿景不等于已交付；格式检查、mock 及退出码各自只证明自己的范围。
