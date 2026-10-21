# GitHub Actions / Repository Hygiene 策略

更新：2026-10-21。目标是**少花 Actions 分钟数，同时不降低合并时的安全门槛**。源仓库仅在需要验证时运行 CI。昂贵集成检查不因每次提交自动运行。

## 工作流触发

| Workflow | 唯一触发方式 | 覆盖 | 推荐时机 |
|---|---|---|---|
| `Quick checks (manual)` | Actions → Run workflow → 选择待审查分支 | 冻结安装、作用域 Ruff、离线检索指标及模型 HTTP 合同单测；**不启动 DB** | 完成本地改动、准备请求 review 时 |
| `Full integration (manual)` | Actions → Run workflow → 选择待审查分支 | PostgreSQL + Qdrant + Poppler、全量 pytest、Ruff、Alembic、进程检查 | 索引/身份/模型/数据库修改的 PR **最终提交**，或发布/故障恢复验收 |
| `Prune merged branches (manual)` | Actions → Run workflow → 先 dry-run，确认后 delete-merged | 检查 merged PR、没有打开的依赖 PR、没有超出 main 的提交及保护状态 | PR 已合并后批量维护一次 |

**没有 `push`、`pull_request`、`schedule` 自动入口。** 普通 push、PR 评论、synchronize、merge 不会自动启动以上工作流。各验证工作流按分支设置并发取消，避免手动重跑产生重叠付费。完整任务最长 20 分钟，快速检查最长 8 分钟。

## 人工合并门槛（必须执行）

1. 在本地执行 `uv sync --frozen --no-editable --extra dev --python 3.12` 与修改范围相关的 Ruff/pytest。代码未改动之前可以只执行本地测试，**不必先消耗 Actions**。
2. 冻结 PR 最终 HEAD；在 GitHub Actions 选择该 PR 的**源分支**，按需求启动 Quick 或 Full。涉及租户身份、资金/凭据、迁移、索引/向量版本、上传数据或生产配置，必须 Full；仅纯文档勘误可免于完整 CI。
3. 在合并前确认选中运行的 `head_sha` 等于 PR **最新** `head.sha`；旧提交绿灯不能代表新提交。检查测试统计、skip 原因及运行时数据库服务。遇到失败先本地修好再**只运行一次最终验证**。
4. 审查 PR 改动、权限边界、回滚方案；特别重要的修改需要独立审查。手动 `workflow_dispatch` 不构成自动 merge gate：如果组织依赖 GitHub required status checks，应核对或调整 branch protection，避免要求不再自动出现的旧检查名。若需要 GitHub 强制阻止未经验证合并，应配置匹配手动运行的必需检查或保持有审查保护。
5. 合并后一般不再跑全套。**只在发布候选或出现集成风险时**手动做一次 main 的 Full。每次手动运行都记下 SHA、run URL、真实/模拟边界，避免重复提交产生两个重型作业。

### 本地节省时间的命令

```bash
uv sync --frozen --no-editable --extra dev --python 3.12
.venv/bin/ruff check apps packages tests scripts
.venv/bin/pytest -q tests/test_page_eval_baseline.py tests/test_retrieval_metrics.py
# 涉及数据库的变更，在独立可销毁的 PG/Qdrant 测试环境中执行目标测试
```

Quick 不负责迁移和生产环境恢复；Full 的 PostgreSQL/Qdrant 是可销毁 CI 服务，不能证明真实 Qwen、Qdrant 服务器重启恢复或线上可用。

## 已合并分支清理（明确破坏性操作）

GitHub 连接当前没有直接删除 Git ref 的接口。手动工作流委托 GitHub Actions 受限 token 进行清理，**默认只列清单，不删除**：

1. Actions → **Prune merged branches (manual)** → Run workflow → `mode=dry-run`，查看候选。
2. 复核所列分支在 main 的 `ahead_by=0`、关联 PR 已 merged、不是保护分支，且不作为任何未关闭 PR 的 head/base。
3. 如确实要删除，再运行 `mode=delete-merged`，并在 `confirm_repo` 填入 `musuntana/GRPO-Rag`。该工作流只允许 `feat/`、`fix/`、`test/` 前缀；其他命名分支需独立审查。
4. 在 GitHub Settings → General → Pull Requests 中按需开启 **Automatically delete head branches**（需要仓库管理权限）。这只处理未来已合并 PR，不替代对未合并工作的保护。

本地等价入口：`python scripts/cleanup_merged_branches.py --repo musuntana/GRPO-Rag`（dry-run）；带 `--apply --confirm-repo musuntana/GRPO-Rag` 才执行 GitHub 删除。执行前需 `gh auth status`，token 需有删分支权限。**分支删除只删引用，不清除提交审计历史，也不会删除主分支或本地未提交文件。**

## 历史数据与垃圾清理

- **不要删除** `docs/validation/` 中已经提交的审计日志、source digests、S0/S1 验证证据；它们是历史证据，只是**不代表最新 HEAD**。文档归档可在后续专门 PR 中审查，不应当作垃圾清空。
- `.gitignore` 忽略新的本地缓存、临时数据库、覆盖率、未提交日志及 OS 杂项；已被 Git 跟踪的历史资产不会因 ignore 被移除。
- Actions 的历史运行日志仍保留。降低未来触发频率不会自动删除已产生的 Actions runs，清空历史并不等同于补回已经使用的计算额度。不要删除安全审计证据以追求页面整洁。
- 当前能力状态只有 [CURRENT_BASELINE](CURRENT_BASELINE.md) 一个入口；旧 implementation、旧 progress 日志仍可检索但不再决定当前进度。
