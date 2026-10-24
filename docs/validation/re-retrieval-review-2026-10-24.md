# 二次检索排序修复交付（2026-10-24）

基线 main `d3b25a2`；原 PR #29 head `16090b1fc0c1401779968f3a58e8cb9fdcff256e`。在原分支合入最新主线，未新建平行检索接口。

## 行为与合同

- 先用首轮结果做 CRAG；需要重检时，合并二轮召回与首轮 reranker shortlist，在既有 top_k_rerank 窗口内用 refined query 统一排序。
- 两轮共有候选只占一个位置，首轮排序胜者参与有限候选窗口；有限窗口不保证保留全部候选，也不保证模型正确性。
- 重复 chunk ID 的文本、版本、来源坐标等不可变字段必须一致。单 channel 多子查询、跨 channel、跨轮次均在裁剪前检查，冲突拒绝。
- 检索响应与聊天 retrieval_completed 事件新增原查询 fusion、重检 fusion、最终 rerank 分数与重检候选数；无模型排序时 rerank_scores 为空。
- 不新增 provider 调用入口，沿用网关与 reranker 候选快照合同；有模型时最多首轮与最终两次 rerank。

## 本地验证

- 主线标准安装产物运行两个新回归：首轮排序胜者保留、跨轮次重复 ID 文本漂移拒绝，均失败，确认缺陷。
- 按锁定依赖、非 editable 安装修复产物后，retrieval pipeline/routes、chat service、gateway rerank、reranker integrity：133 passed。
- CI 范围 Ruff 全部通过；独立 Standards 与 Spec 审查无本轮阻断项。
- 最终 HEAD 仍须核对手动 Quick checks 与 Full integration；结果记录在 PR，不以历史运行替代。

这组测试使用合成候选/模型响应，不能证明真实模型质量、人工 Gold、生产授权、UI 或 S1 退出。

## 已有缺口和下一步

原流程重检后尚未再次调用 CRAG 判定；该缺口在本次修复前即存在，本次仅修复排序与身份验证，不宣称已完成重检后安全拒答。下一步应补上有界重检后的重新判定，以及仍 ambiguous/irrelevant 时不调用生成器的回归。

## 回滚

回退本 PR 即恢复此前排序行为，既有数据库与索引不变；新增 explain 字段会消失，消费者应按可选诊断字段处理。无 schema、数据迁移或线上部署。

后续状态：重检后重新判定及 judge 协议故障语义已由 [CRAG 安全闭环切片](crag-regrade-2026-10-24.md) 修复；本报告保留原切片范围和验证历史。
