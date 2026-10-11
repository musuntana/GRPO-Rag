# OpenCode S1 推进合同

日期：2026-10-09。基础提交：`f0914adf7d97dc0271f80ba3e1db79dccdd44f99`；派工前工作区干净。

## 当前任务 S1-01A：模型 profile 授权与调用边界

协调与审查：Codex；唯一实现写者：OpenCode。任务状态：已派发，等待交付与独立验收；不代表 S1 完成。

运行记录：OpenCode session `ses_ee138a9d4ffe5Y44IKKnMezamO`；启动 PID `241700`（仅供本次运行定位，状态需重新检查）；日志 `/tmp/ragos-opencode-s1-20261009/run.jsonl`；命令为 `opencode run --pure --format json`，使用已有默认模型配置，外层执行上限 1800 秒。已确认读取合同并检查 Git 基线。临时日志不是持久验收产物，交付时应将必要证据归档到仓库验证记录。后续任务尚未启动，须先审查本任务交接。

目标：在真实 provider 接通前，阻止租户通过 profile_override 使用未绑定的模型；明确 gateway 当前能力与后续真实接线的资源条件。

允许写入：`packages/model-gateway/src/ragos/model_gateway/profiles.py`、`packages/model-gateway/src/ragos/model_gateway/router.py`、`tests/test_model_gateway.py`、新增 `tests/test_model_profile_authorization.py`、新增 `docs/opencode-s1-handoff.md`。其他路径只读，需要越界时先在交接记录建议，不自行修改。

冻结语义：可信 TenantContext 是租户来源；租户 use_case binding 是 profile 使用授权；override 不能扩大授权。拒绝发生在 key 取得及 provider 构造/调用之前；保留现有领域异常体系，错误不能包含其他租户资源或秘密。不新增第二套身份、nullable tenant 绕过或隐式全局模型。

执行顺序：阅读 AGENTS 与阶段/架构/约束/领域文档及相关 ADR → 核实当前实现与基线 → 记录有限设计与调用方影响 → 实现授权检查 → 针对性负例/回归 → 交接。发现契约需扩大先记录阻塞，完成范围内安全工作。

验证：用标准安装环境执行 gateway/profile 测试，覆盖绑定模型正常使用、未绑定 override、另一租户绑定、错误 use_case、停用模型以及拒绝前无 provider 副作用。区分新增结果、已有失败、mock 与真实服务。不得用 PYTHONPATH 掩盖安装问题。

资源边界：允许本地代码与隔离测试；不读取凭据、调用真实收费模型、启动公网服务、执行迁移、删除持久数据、commit/push 或更改 OpenCode 配置。不得自行启动其他 agent。本次仅一个写者，共享契约先串行。

交接必须记录改动、命令/结果、环境、证据路径、剩余网关安全缺口、真实 provider/chat/embed/rerank/预算/秘密/出站所需条件、调用方影响及回滚。代码回滚不等于数据回滚；本任务不改变持久数据。

## 后续依赖顺序

1. 独立审查 S1-01A 并核实标准安装/关键回归。
2. 冻结真实 gateway provider、模型身份、秘密别名、预算、出站、超时/取消合同；资源未确认时不执行真实调用。
3. 契约冻结后，按独立路径派发真实入库 Adapter、冻结评测及最小 UI；单文件单写者。
4. PG + 独立 Qdrant + gateway + 浏览器真实旅程集成，更新项目推进记录；缺少真实证据不得判 S1 通过。
