# 下一步推进指令：S1 真实 RAG 与最小用户旅程

> **历史 S1 开发提示（基于 2026-10-09）。** 当时的环境、测试数和未实现列表不一定适用于当前 main。执行前先看 [CURRENT_BASELINE](CURRENT_BASELINE.md) 与 [CI_POLICY](CI_POLICY.md)，只开发当下未完成的真实阻塞。

适用基线：2026-10-09 已通过本地验收的 S0。使用前核对当前 commit 和工作树，最新阶段以 project-progress 的最终裁决为准。以下提示词可以直接用于下一轮开发。

---

你是 `/home/avaarg/GRPO-Rag` 的推进与集成负责人。遵守 AGENTS.md，阅读 architecture、GLOSSARY、development-roadmap、development-constraints、multi-agent-development、project-progress 的最终裁决与相关 ADR。

本轮目标是 **S1：把已验收 S0 的契约接成真实 RAG，并交付最小真实用户旅程**。先核实当前提交、已有改动归属和 S0 证据，保留已验证的身份、不可变来源/index、读回发布、fencing、deadline/cancel 与安装契约。只有发现相关回归才补修 S0，不重写已通过的结构，也不提前做 SFT、GRPO、agentic 或大平台扩展。

## 1. 冻结本轮合同与资源条件

先给简短设计与任务安排，再继续授权范围内的本地实现，不停留在计划。

明确一个真实 provider，固定 chat/embed/rerank 的模型用途、embedding version/维度、出站策略、secret alias、调用预算/超时/取消与用量归属。模型调用全部经 gateway，预算或身份不可验证时拒绝。不得读取额外凭据或扩大权限；需要用户提供条件时请求最小必要信息，并继续独立工作。

确认可授权使用的真实文档、内容版本与用途，准备少量经过审核的问题/页级 gold 和不可回答用例，按来源/近重复检查 train/dev/holdout 边界。示例数字不填入结果表，未经审核的反馈不成为 gold。

冻结上传、入库状态、会话/消息、run/SSE、citation 原文定位与验证状态的 DTO。共享 KB 使用资源 ACL，私有会话/记忆再限制 user owner。生产身份未接通前只在隔离本地环境验证，不能公开部署。

## 2. 按依赖组织多 agent

由集成负责人维护任务表，列出 owner、精确文件范围、前置任务、冻结契约、验收、观测和回滚；单文件单写者，共享类型/迁移/依赖由指定 owner 串行修改。

建议按需要设置以下任务，职责可合并，不能因分工复制业务规则：

- S1-01：真实模型 gateway 与身份/秘密/出站/预算合同。
- S1-02：真实文件解析、固定表示、IndexPlan 准备、隔离 ingest worker、独立 Qdrant 服务与双索引发布。
- S1-03：直连/现成 reranker baseline、逐 case 评测与错误分析。
- S1-04：薄 Web 或服务端页面，贯通上传、进度、问答和原文引用。
- S1-05：独立安全/故障/前端审查与整条旅程集成验收。

先完成契约与身份来源，再并行独立的 Adapter、评测和 UI 消费者。只有验证所需的最小依赖可引入，不新增微服务或万能框架。

## 3. 完成真实端到端路径

实现并验证：

授权文件 → 原文与不可变 DocumentVersion/Representation → 结构保留的 children/parents → 持久 IndexPlan/任务 → 真实 gateway embedding → PG 与 Qdrant 独立写入及读回 → 对账成功/CAS 发布 → query recall/fusion/现成 rerank → CRAG → 预算内父块上下文 → grounded answer/拒答 → 引用结构验证与诚实语义状态 → 原文版本/页/span 阅读。

冻结 provider 实际 embedding 身份，不能只配置一个名称而任意更换模型。Qdrant server 的集合、维度、payload、权限过滤与 hydration 必须验证；hash、StubProvider 和 Qdrant local 引擎不能替代真实链路验收。

入库/模型执行不持有长 DB 锁，超时和取消释放资源，旧 worker 不发布结果。保持 run_id 一致；SSE 先持久再交付，重连只 replay 原 run，不重新调用模型或计费。未经语义核验的 citation 不显示为 verified。

## 4. 同步完成前端体验

用户可以选择授权 KB、上传真实文件、看到队列/处理/成功/失败状态，发起问答并打开引用原文。

覆盖 loading、空、无权限、错误、重试、取消和断线恢复；切换租户/会话清理缓存和订阅，旧响应不能覆盖新页面。保持键盘/焦点、移动布局与可读恢复动作。未接通功能隐藏或明确不可用，不能以静态页面、截图或假 token 流宣称完成。

## 5. 验收与交付

按风险完成必要验证，区分已有基线结果与本轮新结果：

- 锁定安装与关键契约无回归；真实临时 PG + 独立 Qdrant server 的实际进程旅程。
- 两租户、同租户两用户、未授权 KB、旧文档版本与历史引用的负例。
- 双侧部分提交、worker 崩溃、租约失效、超时/取消、重复提交和 SSE 重连。
- 浏览器真实上传→状态→问答→引用路径及失败恢复。
- 固定代码/数据/index/profile/model/prompt/judge 版本的逐 case baseline，分解析、召回、排序、生成、引用与拒答失败记录真实质量/成本/延迟。

更新 docs/project-progress.md，保存原始验证证据、失败与未执行项，说明实际行为、S1退出裁决、观测信号和配置/模型/索引/数据回退。没有真实凭据、预算或数据时准确记录阻断，不用 mock 代替；完成不受阻断影响的工作后报告所需条件。

本轮允许必要的本地实现与隔离验证；commit/push、公开部署、付费训练、删除持久数据和远程发消息需另有明确授权。现在开始核实基线、资源条件并冻结 S1 任务合同。
