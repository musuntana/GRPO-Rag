# S1 本机真实模型验收（2026-10-23）

状态：本机合成 PDF 链路 **PASS**；真实企业语料与 S1 阶段退出 **未验收**。准确版本、运行结果与源码 SHA256 见同名 JSON。基于 `5510f6b`，验收时变更位于 `codex/s1-local-model-acceptance` 的工作树，准确源码由 JSON 哈希冻结；最终提交的 CI 另行核对，不能套用旧 CI SHA。

## 问题与修复

OpenCode 实施，Codex 复核及真实复验：

1. frozen embedding CLI 使用 `with QdrantClient(...)`，但锁定的 1.19.1 不支持该协议。改为显式 `try/finally close`。真实内存 Qdrant 回归先 2 failed，修复后 2 passed，覆盖成功和模型请求失败时释放资源。
2. 本机 Qwen 返回合法嵌套 `usage.prompt_tokens_details`，原网关把所有 usage 值当整数而拒绝 HTTP 200。现在验证标准详情为 null 或非负严格整数对象，再转换为内部平面整数计数；不传透原始嵌套数据、不修改输入，未知非整数、bool、负数和非法嵌套仍拒绝。用量回归先 5 failed/10 passed，修复后 provider 测试 32 passed。

不变更依赖版本、数据库 schema、embedding 身份算法、租户准入或计费契约。此为既有 operator smoke 的兼容性修复，不新增生产绕过入口。

## 实际环境与命令

Docker Desktop 29.8.2，Python 3.12.15，Poppler 26.10.0，锁定 Qdrant client 1.19.1；16 GiB arm64 Mac。原 Chat 调用因 Metal OOM 失败。将两个已安装模型的上下文限制为 2048；Chat mode=completion、Embedding mode=embedding，keep-alive=5m。keep-alive=0 的中间实验导致模型立即卸载，已改回 5m。保留当前可运行配置；修改前无显式 model runtime overrides。无需下载模型、无付费训练。

```bash
uv sync --frozen --no-editable --extra dev --python 3.12 --reinstall-package ragos
export RAGOS_PROBE_BASE_URL=http://127.0.0.1:12434/engines/v1
export RAGOS_PROBE_EMBED_MODEL=docker.io/ragos/qwen3-embedding:0.6b-q8
export RAGOS_PROBE_EMBED_REVISION=79898cf0ff54015bf309900ccc25a340dd0ab011bbc78486e50abbcfb4ae908e
export RAGOS_PROBE_EMBED_DIM=1024
export RAGOS_PROBE_CHAT_MODEL=docker.io/ai/qwen3:4b-instruct-2507-q4_K_M
.venv/bin/python scripts/check_frozen_embedding.py
.venv/bin/python scripts/check_local_pdf_rag.py --output /private/tmp/ragos-local-pdf-smoke.json
```

revision 是本机 model runner inspect 返回的身份声明，未做密码学权重加载证明。脚本仅消费生成的合成文本/PDF，内存 Qdrant；没有读取客户文件、访问持久业务索引或消耗外部模型预算。

## 结果与验证边界

- 真实 Embedding：有效 1024 维，3 个候选及非空 projection digest。
- 真实 Chat：非空回答，内部平面 usage 正常。耗时约 4.805 秒，仅单次 smoke，不是延迟基准。
- 合成 PDF：3 页经真实 Poppler 解析；检索第 1、2 页，单题 Hit@2=1，结构引用 verdict=`ok`；CLI exit=0，约 4.906 秒。**不能外推为真实语料的检索准确率**。
- Codex 独立作用域 Ruff PASS；4 个相关测试文件 **46 passed, 3 skipped**。跳过项依赖可销毁 PostgreSQL/Qdrant HTTP 服务，未降低门槛。初次沙箱运行因缓存写入/回环绑定 EPERM 失败，在允许本机测试的环境复验通过。
- OpenCode 另执行本地 suite：**1041 passed, 41 skipped**；含未配置服务/平台条件导致的 skip，不是完整真实集成验收。没有触发 GitHub Actions。

OpenCode 首个 CLI 任务成功；Chat 任务原免费模型经历 503 与 429，改用已有免费模型后完成。原用户脏工作区未改。本机验收阶段未 commit/push、部署或训练；后续发布与合并状态以对应 PR 为准。

## 仍未验收及下一步

暂无用户授权的真实 PDF、人工 Gold/dev/holdout；语义引用未检查、生产租户 ModelRouter/预算未验证、没有发布 PG/Qdrant 真实语料、没有 SFT/GRPO。此 smoke 不关闭 Issue #22、不代表最小上传/问答/引用 UI 旅程完成。

下一步先取得授权语料与人工页级 Gold，再冻结发布索引、捕获真实 traces 做 Dense/Reranker 对比。最小受控用户旅程另按领域授权和浏览器验收纵向交付。

回滚：仅撤销本分支这两个实现文件及对应回归测试的补丁；不要覆盖原用户工作区。无持久数据库变更。Poppler 是新增本机依赖，可保留；模型运行配置变更已记录，可按需要通过 Docker Model Runner 重新配置，避免恢复会导致 OOM 的大上下文。
