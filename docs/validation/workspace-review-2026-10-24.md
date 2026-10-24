# 工作区遗留改动审查（2026-10-24）

比较基线：main `88209788be17aa65c54a9336e2d2a8c53b3674a5`。审查原工作区 13 个已跟踪修改和 48 个未跟踪文件，按现行网关、冻结评估与阶段边界决定取舍。

## 保留并重新实现

- `compare_bundles`：空指标、指标集合不同、NaN/Infinity 不能作为晋级证据；非法比较容差也拒绝。有效同集合指标继续按既有方向和容差比较。
- `precision_at_10` 越高越好，修正此前将其当成成本指标的错误。
- 开发 API 示例及 PG/Qdrant/Valkey 发布端口默认只绑定 `127.0.0.1`。需要其他访问范围时应显式配置；本切片不重启服务、不修改数据库卷或已运行部署。

## 移出工作区

- 旧 DockerModelProvider/test grant、模型身份 lock、对应测试和 tools 配置已被当前 local_openai admission 与显式 Settings 替代。整文件套用会删除新 worker 接线及网关边界，故不合并。
- 旧 runtime/gateway 替换会覆盖已接通的真实 Dense Recall 与更严格的 reranker 输入/输出合同，故不合并。
- 旧 Dockerfile/测试 override 缺少当前 PDF 工具要求，且存在 Qdrant tmpfs 权限与服务启动步骤缺口，不作为可运行入口交付。
- 未提交的 10-12 日志、探针输出和旧状态说明归档在本机恢复包；没有删除主线已提交的任何历史验证证据，也不以旧测试数冒充当前验证。
- 旧页级评估整体替换不直接移植：当前已有冻结 JSONL 页级评估器。legacy eval 的 case/split/scope 一致性与版本化 Grade 仍值得另行梳理，避免新旧评测口径无迁移混用；原实现已归档可恢复。

原始 61 文件的压缩备份与逐文件 SHA256 位于仓库本地 `.git/governance/20261024/workspace-review/`，逐字节校验通过；不推送原始日志或本机模型探针。

## 本轮验证

- 标准 `uv sync --frozen --no-editable --extra dev --python 3.12` 安装产物；本地额外重新安装 ragos 以验证本次源码。
- 新门禁回归在修复前：12 failed、31 passed，确认实际缺陷。
- 修复后 metrics、reranker training、page eval baseline、retrieval pipeline：142 passed。
- 修改文件 Ruff 通过。
- Docker Compose 实际配置解析成功，断言全部 4 个发布端口的 host_ip 为 127.0.0.1；API 示例同样为 loopback。
- 合并前核对最终 PR HEAD 的手动 Full integration。全量 CI 的最终结果记录在 PR，不能以本报告替代。

本轮没有运行真实语料质量评估、生产授权验收、UI 用户旅程或 SFT/GRPO 训练。

## 回退

回退本切片提交即可恢复原门禁与默认开发绑定，无 schema 或持久数据迁移。旧门禁的错误晋级风险会随回退恢复。原始遗留文件只应在隔离目录解包审查，不整体覆盖最新主线。
