# 开发顺序与阶段验收

更新：2026-10-09。基线提交：`d5a2709814b4c4b82f27ebf870f60e8e23669195`。本文件决定实施先后，替代旧 [implementation](implementation.md) 的 M0–M10 顺序；[architecture](architecture.md) 仍决定架构，验收执行 [constraints](development-constraints.md)。

## 1. 项目目标与当前事实

目标是在模块化单体、多租户隔离和完整用户体验的基础上，构建复杂企业文档的证据检索、排序、回答与引用验证闭环。训练和多模态是由实验结果决定的能力，不能因仓库名含 GRPO 就优先实现。

本次源码核对：现有 API/workflow/ingest/train worker 源码、retrieval、memory、8 个迁移和规则测试；尚无 apps/web。标准安装显式包列表未包含 retrieval/memory/ingest/train worker；基础 TenantContext、ORM、vector 读写接口未对齐，train handler 缺失；gateway 仅 StubProvider，writer 默认 hash embedding。SFT/GRPO 辅助函数存在但不构成真实优化循环。

以上是源码状态，不是最新训练/生产/浏览器实测。前次审查的测试结果属于当时的隔离环境，本次仅调整治理文档，不重新据此认定任何阶段完成。

用户提供的算法路线和平台对比是目标输入；其中示例指标、简历能力勾选和竞品“未见原生支持”不是本项目实验数据或已经核验的市场事实。未产生真实结果前，只写目标/假设/待验证。

## 2. 所有阶段的底线

- 安全隔离、领域契约、幂等故障恢复、基础 trace、验证与回滚从 S0 起实施；S2/S5 提高覆盖和运维深度。
- 领域/架构契约先冻结，随后按 [多 agent 协作](multi-agent-development.md) 并行。阶段之间有依赖，不能同时开发互相依赖且未确认的核心接口。
- 前端随用户旅程纵向交付；S0 保持既有体验，S1 提供最小真实 UI，此后每阶段维护成功与故障状态。
- 阶段退出依赖证据，不依赖 checkbox 数量或固定工期。未达门槛继续修复，不以新功能掩盖失败。
- baseline 与新结果区分代码、数据、模型、profile、成本/延迟范围；失败样本单独保存。无真实指标不编数字。
- S1 的 baseline 和评测从第一次真实链路开始；S2 修口径与验证独立性，不把评测推迟到训练后。

## 3. 六个阶段

| 阶段 | 目标 | 核心产物 | 退出条件 |
|---|---|---|---|
| S0 修复可运行性 | 把当前恢复提交整合为一致的安装与契约基线 | 包/入口、TenantContext、ORM/迁移、repository、vector profile/payload/filter、任务路由与回收；README 与 CI | 干净环境标准安装产物可导入；API/worker 启动能力检查；完整测试可收集；相关关键测试/迁移验证通过；没有以 PYTHONPATH/stub/放宽权限掩盖缺口 |
| S1 打通真实 RAG | 一个用户通过真实文档得到可定位证据的真实回答 | 一个真实 provider/embedding + 现成 reranker baseline；原文件→解析→chunk→双索引→gate→answer/citation；最小真实 Web/页面旅程；冻结初始 eval | UI 可上传、看入库状态、提问、看原文引用；正常/失败/取消/重连旅程可运行；真实向量与引用；逐 case 基线记录；成本/延迟来自真实调用；基本租户/用户隔离通过 |
| S2 修正评测与身份边界 | 让实验比较及资源授权可信 | 统一 evidence identity、split、防泄漏、MRR/NDCG/precision、非空晋级门槛；签名鉴权、完整范围矩阵、事务与 RLS、memory 与 replay 授权 | 同数据/版本比较，错误页/重复/空集/NaN/错 scope 反例通过；两租户与同租户两用户的资源/缓存/产物负例；真实故障恢复；记忆影响上下文且不混 KB；引用验证 UI 诚实 |
| S3 实现 SFT | 强现成模型之外的可加载训练基线 | 审核后的 train hard negatives、pairwise/listwise 目标、隔离训练运行时、weights/config/seed/manifest、产物 registry 与回滚 | 真实训练与加载推理；同 holdout 比较现成/SFT；owner/digest/版本一致；晋级原子可回滚。若无可靠收益，保留现成模型且记录结论 |
| S4 GRPO 对照实验 | 检验 RL 是否补足 SFT 排序瓶颈 | 明确可采样排序动作/约束、正确 objective 与 reference KL、train reward、dev 调参、冻结 holdout、多 seed 等预算对照 | 增益在排序/答案/引用/拒答和成本上可解释；reward hacking 反例通过；晋级不损坏关键指标。无收益时 GRPO 不上线，阶段以有效否定结论结束 |
| S5 产品与运维扩展 | 在已验证主线之上补需求和运行能力 | 完善 Web/admin、workflow DSL/HITL、sandbox/tools、scheduler、quota、OTel、备份恢复；必要时多模态 | 业务旅程与权限/故障/回滚验证；容量、成本和维护责任清晰；有负载证据后才扩容/拆分 |

S0 修复次序：先 canonical identity/evidence 契约和基础模型；再存储/索引映射、包与进程接线；最后安装、启动、迁移、收集与关键场景检查。缺失能力要显式关闭，不允许注册一个空 handler 便称训练可用。

S1 使用本地/隔离环境与受控真实数据；在签名鉴权、秘密/预算和资源授权未达到上线门槛前不能公开部署。可以先用薄服务端页面，不能只有 API 加一张“未来 Web”设计图。

S2 不把全部指标一次塞进平台：先冻结相关定义和黄金样本，再逐类验证。RAGAS/LLM judge 辅助答案质量，不替代确定的检索 gold、结构引用检查和人工标注；judge 也要固定版本与校准。

S3/S4 离线执行，不占用请求/入库 worker 的计算预算。训练 run 使用授权数据与独立预算，在线调用实际产物仍经 model gateway。train/dev/holdout 严格区分；最终 holdout 不能反复用于选候选。

## 4. 算法升级的判定依据

| 失败类别 | 必须先确认 | 可实验的下一步 |
|---|---|---|
| 解析/布局 | Gold 内容是否进入可读产物、页/span 是否真实 | OCR、表格/布局恢复、解析器；不可先怪 embedding |
| 召回 | Gold 不在固定候选 K，且权限/索引/解析正常 | chunk 粒度、embedding、lexical+dense、rewrite；按需多模态 |
| 排序 | Gold 在候选集但不在最终 evidence K | 现成 cross-encoder→hard negatives→SFT→可选 GRPO |
| 生成 | 送给模型的正确证据是否充分、完整、预算内 | 上下文组织、prompt、答案验证、多页推理 |
| 归因 | claim 与引用是否同版本、同页、正确跨度且支持 | 结构/语义验证、引用 UI、拒答策略 |
| 不可回答/越权 | 权限、版本、无 gold 或证据不足 | 拒答与权限错误；不能靠调低门槛产生答案 |

page-as-parent 是当前已接受设计，不代表效果已被证明。其成本/可变父块大小和其他 granularity 仍需消融；多模态只有在图表/扫描/布局失败占比与成本证据支持时引入。

## 5. 实验记录与阶段交接

每个实验保留独立 manifest 和结果引用，实际敏感样本放授权 artifact 存储，不提交进 Git。最小记录包含：

- 问题、瓶颈假设、对照方法、预先确定的成功/失败判据；
- code commit、dataset/labels/split/version/digest、文档/index、parser/chunker/embedding/retriever/prompt/judge/model 版本；
- seed、硬件/依赖、预算/延迟口径、样本数、去重/分母/指标方向；
- 逐 case 结果、分类型失败、异常/缺指标、真实成本与时延；
- 结论、局限、promotion/rejection、回滚产物指针和审核者。

没有权重、可加载产物和真实指标时，状态为“规则/数据准备”或“未验证”，不写 trained、提升百分比或生产稳定。mock、源码检查、格式检查、浏览器截图分别说明其证明范围。

阶段交接按多 agent 规范记录 owner、路径、Interface、验证、已知基线缺陷与回滚。下一阶段只消费已冻结并验收的契约；合并负责人验证完整用户旅程后才能判定阶段完成。
