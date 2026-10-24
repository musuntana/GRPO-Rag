# CRAG 重检后重新判定（2026-10-24）

基线 main `3265245`。本切片实现 architecture §8.4 与 development-constraints 的既有合同，不新增检索/模型框架。

## 行为

- 首轮 relevant 直接沿用正常路径，irrelevant 拒答；ambiguous 最多重检一次。
- 重检、合并及排序后，用 refined query 和最终候选再次调用同一个租户 grader。只有最终 relevant/proceed 进入生成。
- 最终仍 ambiguous 时保留真实 verdict，但 action 为 refuse，表示本次重检预算已耗尽；最终 irrelevant 同样拒答。
- 拒答复用同一路径，不调用生成器，evidence/citations 为空。gate 与 explain 的判定、动作、置信度和 prompt ID 均为最终结果；re_retrieval_attempted 等字段保留重检事实。
- judge 协议要求对每个输入候选恰好返回一次整数 0–3 评分。缺失、未知或重复 ID，非整数/bool/越界分值、畸形响应均抛 ProviderError，不再强制转换、截断或补零伪装正常 irrelevant。
- judge/provider 异常直接传播；不回退为成功、irrelevant 或跳过判定。空候选仍按既有无证据路径拒答，不调用 judge。
- 最多两次 grader 调用，每次沿用已有模型网关、租户上下文与请求预算约束；预算/服务拒绝时明确失败，不启用无限重检。

## 验证

- 重新判定新增回归在基线安装产物失败 5 项，确认缺口。
- 严格协议新增回归在旧解析器失败 16 项，确认错误分类与非法响应问题。
- 标准冻结依赖、非 editable 安装后，pipeline/query-engine/profile/chat-service/retrieval-routes：171 passed。
- CI 范围 Ruff 通过；独立 Standards 与 Spec 复核通过。
- 旧排序回归现在显式模拟第二轮 relevant，继续验证新增证据及首轮排序胜者可进入 Top-K；不通过降低生产阈值来让旧测试变绿。
- 最终提交手动 CI 结果记录于 PR；本地测试不替代最终 HEAD 检查。

测试为合成候选与确定性 judge 输出；未验证真实 judge 的误拒答/误接受率、模型质量、人工 Gold 或生产计费预算，也不构成 S1 退出。

## 变更与回退

没有 schema/profile/prompt/阈值/索引迁移，也未部署或训练。API/SSE 沿用既有字段，其 gate 信息现在表示最终判定；依赖这些诊断字段的客户端不应把 re_retrieve 当作完成运行的最终动作。非法 judge 响应从正常拒答变为 ProviderError 是有意收紧的错误合同。

回退本切片会恢复重检后未重新判定及协议故障补零的已知缺口；无需数据回滚。
