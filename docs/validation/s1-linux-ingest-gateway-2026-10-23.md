# S1 Linux ingest worker 真实网关验收（2026-10-23）

状态：**PASS（仅限开发环境、单租户、合成纯文本、本机模型端点的验收）**。不表示 S1 阶段退出、生产可用、上传/UI 可用、PDF 解析完成或检索质量达标。

## 1. 结论

在 Linux `aarch64` 容器内，经真实 `process_one_task` → `ProcessRunner` → 子进程 `ragos.runtime.ingest_job` 路径，使用租户 `ModelRouter` 调用本机 Docker Model Runner 的 Qwen3 Embedding，把一份合成纯文本索引写入真实 PostgreSQL 17 与 Qdrant 1.19.1，并完成两侧读回对账。

| 事实 | 结果 |
|---|---|
| 任务 | `completed`，`attempts=1`，`error_code=null`，任务耗时 `1.459s` |
| manifest | `indexed`，`chunk_total=2`，`reconciled=true` |
| 两侧 digest | `pg_digest == vector_digest == 67de684091470aebc5a035f5e5cdf30817bb78b7df373ebd098a2a180b551fcd` |
| 文档 active 指针 | `document.active_index_id == manifest_id`（验收脚本断言） |
| Qdrant | 2 个 point，1024 维；collection 见 JSON |
| 真实模型证据 | 子进程写入的 child 向量与独立直接模型调用 cosine `0.99999979` |
| KeyPool | `key_pool_rows=0`；本机 provider 准入未读取/解密 `KeyPool` |
| 子进程平台 | `sys.platform=linux`，`machine=aarch64`，Python `3.12.15` |
| 总验收耗时 | `1.763s`（含建租户、冻结计划、worker 任务和 Qdrant 读回） |

准确结果、ID、源码哈希和局限以 [验收 JSON](s1-linux-ingest-gateway-2026-10-23.json) 为准。

## 2. 源码与身份

验收基于工作树，不是已提交 SHA：

- 基线提交：`d6e74f2d10d94f759a5436e1528f8106b798582b`
- 分支：`codex/s1-upload-journey-contract`
- 状态：未提交工作树；验收 JSON 的 `source_sha256` 冻结实际运行源码
- 合并时必须对最终提交重新跑 CI/验收，不能把工作树哈希当成提交哈希

JSON 记录的 4 个运行时/入口源码：

| 文件 | SHA-256 |
|---|---|
| `packages/runtime/src/ragos/runtime/worker_embedding.py` | `b6cc5ac9e9dc5134aa4ab3aa2272d0c1e528f1fe41b19f448ea4d9456697ebf7` |
| `packages/runtime/src/ragos/runtime/ingest_job.py` | `8cfbf0f4ae7c785f781ed726d5725193758588b8a7b13c77cb2028504493b734` |
| `apps/ingest-worker/src/ragos_ingest_worker/main.py` | `28016525130e8a7c2d7255b5cc053c8cefb349f376f04123b15d2be18685151d` |
| `packages/model-gateway/src/ragos/model_gateway/router.py` | `1cb406b27f030a24ec468aeec2a7e21f6fbdf636c215b988c961e43b33b7da68` |

同批但未写入 JSON 的 `config.py` 为 `28c8c2012791ff9c99384c7a80406cc3f0f2e3ae9b6e82758e26a85d4d594606`。测试源码哈希不冻结在验收 JSON 中，合并 CI 以最终提交为准。

模型身份：

- Embedding：`docker.io/ragos/qwen3-embedding:0.6b-q8`
- revision：`79898cf0ff54015bf309900ccc25a340dd0ab011bbc78486e50abbcfb4ae908e`
- dimension：`1024`
- profile：`qwen3_embedding`
- frozen version：`508b34078998e0aa_79898cf0ff54015bf309900ccc25a340dd0ab011bbc78486e50abbcfb4ae908e_1024`
- Chat（仅用于本地 admission 的完整性，不发起 chat 调用）：`docker.io/ai/qwen3:4b-instruct-2507-q4_K_M`
- revision 是算子在本机记录的模型标签身份，不是权重加密证明

## 3. 环境

| 项 | 值 |
|---|---|
| 主机 | macOS 26.7.2，arm64 |
| Docker Desktop / Engine | 29.8.2，Linux engine，arm64 |
| Docker Model Runner CLI | `v1.2.6`；模型由 Docker Desktop 管理 |
| 容器 worker | Linux `aarch64`，Python `3.12.15` |
| PostgreSQL | `postgres:17-alpine`，一次性实例 |
| Qdrant | `qdrant/qdrant:v1.19.1-unprivileged`，一次性命名卷 |
| 迁移 | `0013_chat_request_claims` |
| 验收镜像 | `ragos-s1-worker-acceptance:20261023`，digest `sha256:a67b0b9759710fbb707106e049127871cee451da323e71d5e4be5c12c2f08c4f` |
| 基础镜像 | `ragos-s1-worker-base:20261023`，digest `sha256:30572baaee6e3c51d8470c797efa27868cb896221872c29b088d3131db86d6b2` |

worker 容器使用私有 Docker network，PG/Qdrant 均未发布宿主端口。DMR 通过容器内 `http://model-runner.docker.internal/engines/v1` 访问；`LocalModelAdmission` 明确允许该主机名，但仍不允许任意远程 URL。Qdrant unprivileged 镜像写入 tmpfs 时权限不足，实际使用一次性命名卷；这不是产品存储方案。

## 4. 验收方法

验收 harness 是临时目录中的外部脚本，不属于产品代码：

- `src/acceptance/run_linux_ingest.py` SHA-256：`da0cb4dc1baad54fff9a25dcc9d82a23c1e5185a46ed20920068aa45e46cb0eb`
- `Dockerfile.acceptance` SHA-256：`fdb1da24fc9dc7835e0cf95958b447dfb131db6223f5cc8761e4244ccdd5e77b`
- 目录：`/private/tmp/ragos-s1-linux-worker-20261023/`
- 验收 JSON SHA-256：`645187dbb750f50451f67958548e23860c9a52b02edad7ab89e52293d8a5f9e6`

核心链路：

1. 构建锁定依赖、非 editable 的 Linux acceptance 镜像；
2. 启动一次性 PostgreSQL/Qdrant，执行 Alembic 到 `0013_chat_request_claims`；
3. harness 建立一个一次性租户、`local_openai` provider、embed profile 和租户 use-case 绑定；
4. 通过既有 `IndexPreparationService` 冻结合成纯文本表示、2 个 chunk（1 parent + 1 child）、manifest 和 `ingest_document` task；
5. 在 Linux 容器内调用真实 `process_one_task`，由 `ProcessRunner` 启动子进程 `ragos.runtime.ingest_job`；子进程只拿到显式 allowlist 环境；
6. 子进程经租户 `ModelRouter` 调用真实 DMR embedding，逐文本写入 Qdrant，完成 PG/vector 读回；
7. harness 断言 task、manifest、digest、active index、Qdrant 点数和维度，并把 child 向量与独立直接模型调用比对。

关键命令（完整 harness 见上述临时目录）：

```bash
BASE=/private/tmp/ragos-s1-linux-worker-20261023
NET=ragos-s1-linux-20261023
PG=ragos-s1-linux-20261023-pg
QDRANT=ragos-s1-linux-20261023-qdrant

docker build --progress=plain -t ragos-s1-worker-base:20261023 "$BASE/base"
docker build --progress=plain -f "$BASE/Dockerfile.acceptance" \
  -t ragos-s1-worker-acceptance:20261023 "$BASE"

docker network create "$NET"
docker run -d --name "$PG" --network "$NET" \
  -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=ragos \
  --tmpfs /var/lib/postgresql/data postgres:17-alpine
docker run -d --name "$QDRANT" --network "$NET" \
  -v ragos-s1-linux-20261023-qdrant-data:/qdrant/storage \
  qdrant/qdrant:v1.19.1-unprivileged

docker run --rm --network "$NET" \
  -e RAGOS_APP_ENV=dev \
  -e RAGOS_PG_DSN="postgresql://postgres:postgres@$PG:5432/ragos" \
  ragos-s1-worker-acceptance:20261023 \
  alembic -c alembic.ini upgrade head

docker run --rm --network "$NET" \
  -e RAGOS_APP_ENV=dev \
  -e RAGOS_PG_DSN="postgresql://postgres:postgres@$PG:5432/ragos" \
  -e RAGOS_QDRANT_URL="http://$QDRANT:6333" \
  -e RAGOS_STORAGE_ROOT=/work/storage \
  -e RAGOS_LOCAL_MODEL_BASE_URL=http://model-runner.docker.internal/engines/v1 \
  -e RAGOS_LOCAL_MODEL_EMBED=docker.io/ragos/qwen3-embedding:0.6b-q8 \
  -e RAGOS_LOCAL_MODEL_CHAT=docker.io/ai/qwen3:4b-instruct-2507-q4_K_M \
  -e RAGOS_LOCAL_MODEL_EMBED_REVISION=79898cf0ff54015bf309900ccc25a340dd0ab011bbc78486e50abbcfb4ae908e \
  -e RAGOS_LOCAL_MODEL_EMBED_DIMENSION=1024 \
  -e RAGOS_LOCAL_MODEL_PROVIDER_ID="<fresh-uuid>" \
  -e RAGOS_ACCEPTANCE_OUTPUT=/work/out/linux-ingest-acceptance.json \
  -e UNRELATED_SECRET=must-not-reach-child \
  -v "$BASE/evidence:/work/out" \
  ragos-s1-worker-acceptance:20261023 \
  python acceptance/run_linux_ingest.py
```

`UNRELATED_SECRET` 用于检查父进程环境不会被整体复制；子进程实际只接收产品代码构造的 allowlist。该断言由单测覆盖，验收进程本身不读取子进程 `/proc`。

集成测试的 localhost 守卫没有被放宽。容器内用 test-only TCP forwarder 把 `127.0.0.1:15432/16333` 转发到一次性 PG/Qdrant，再运行测试；该 forwarder 不进入 ingest 验收链路。

## 5. 测试与回归

全部在 Linux acceptance 镜像内执行：

| 范围 | 结果 |
|---|---|
| 定向（gateway/handler/local router） | **52 passed**，`0.93s` |
| Ruff（本切片 8 个源码/测试文件） | **All checks passed** |
| 真实 PG/Qdrant 集成（storage/PDF-index/recovery/task/vector/provider） | **170 passed**，`7.88s` |
| 全量 Linux pytest | **1115 passed**，`17.50s` |

集成组包含 `test_storage_integration.py`、`test_pdf_pg_index_integration.py`、`test_index_integration.py`、`test_qdrant_server_pg_reconnect.py`、`test_task_dispatcher.py`、`test_task_lifecycle.py`、`test_task_retry_policy.py`、`test_vector_writer.py`、`test_provider_admission.py`。PDF 相关测试仍使用合成 PDF 与 hash 向量做机械回归，不是模型质量证据。

阴性覆盖：

- 默认关闭、生产环境、非 Linux、缺少 admission 均在 DB/vector/model 之前拒绝；
- 错误 `embedding_profile`/`embedding_version`、错误 provider/model 绑定、错误 manifest 模型空间拒绝；
- child 环境不复制 `os.environ`、不继承 ambient secrets、不接受任务提供的 URL；
- writer 在异常路径关闭；
- task claim/attempt/cancel 仍由既有 `_claim_job`、`_execution_guard` 和 lease/CAS 约束，验收任务 `attempts=1`；
- hash test 路径保持原有 `sha256-hash-v1` 与 `test_hash` 行为，未把 hash 向量冒充真实模型结果。

## 6. 按计划审查的重点

| 重点 | 结论 |
|---|---|
| auth-before-provider | `worker.preflight()` 在构造 vector writer 前解析租户 binding、provider 和本地 admission；本机路径不读取 KeyPool |
| stale claim/cancel | 子进程重新 `lock_claim`，每次 checkpoint 前执行既有 `_execution_guard`；真实取消/M48 竞态由既有 task 测试覆盖，本次只验证正常完成 |
| no production activation | `ingest_local_model_enabled` 默认 `false`；`app_env != dev` 或非 Linux 在 DB/vector/model 前拒绝；生产不因本切片开启 |
| identity consistency | manifest 的 profile/version 与 `FrozenEmbeddingSpec` 必须一致；writer、embedding、manifest 使用同一冻结身份；两侧 digest 相等 |
| child env | 仅服务端 allowlist（DB/storage/vector/frozen flags/model names/loopback API host）；`Settings(_env_file=None)`；单测检查不读 ambient `.env` |
| resource cleanup | writer 在 `finally` 关闭；Qdrant client 显式关闭；session 均用上下文；子进程输出到 DEVNULL |
| long DB write lock | embedding HTTP 期间不持有 task/manifest 写锁；每个 embedding 调用使用独立租户绑定 session，发布仍由 checkpoint/CAS 完成 |

## 7. 局限与未验证

- 只验收了一个租户、一个 provider/profile/binding、一个 worker 任务和 2 个合成纯文本 chunk；不是并发、吞吐或延迟基准。
- 本切片明确不包含上传 API/UI、PDF 解析/版面保真、企业语料、生产鉴权/成员授权、计费、真实 Gold 或 GRPO；这些均未验收。
- 模型 revision 是本机标签身份，不是权重 attestation；没有语义质量、Recall/NDCG 或人工评审结论。
- Linux 指 worker 容器；DMR 在 macOS Docker Desktop 管理下运行，不能据此声称 Linux GPU serving 或生产模型服务已验收。
- 未做模型 HTTP 层故障注入、Qdrant 服务器重启/备份恢复或跨进程取消时延测试；这些仍是后续 S1 工作。
- 2026-10-24 交付复核：工作树源码与上述 Linux 验收快照逐字节一致；定向测试重新运行 52 passed，CI 范围 Ruff 通过。最终提交仍须通过手动 Full integration；历史模型验收不替代最终 HEAD 的 CI。

## 8. 复现、清理与回滚

复现：按第 4 节命令构建镜像、启动一次性服务、迁移并运行 harness；模型必须已安装且 revision/dimension 与配置完全一致，否则应停止而不是放宽校验。

清理（一次性资源，确认无其他使用后执行）：

```bash
docker stop ragos-s1-linux-20261023-pg ragos-s1-linux-20261023-qdrant
docker rm ragos-s1-linux-20261023-pg ragos-s1-linux-20261023-qdrant
docker network rm ragos-s1-linux-20261023
docker volume rm ragos-s1-linux-20261023-qdrant-data
```

回滚：本切片没有 schema、依赖或持久状态迁移；开发环境可设置 `RAGOS_INGEST_LOCAL_MODEL_ENABLED=false` 并重启 ingest worker，或回退本切片提交。生产路径始终拒绝，不需要生产数据回滚。不要删除共享 PG/Qdrant 数据或其他人创建的卷。

## 9. 关联

- 实施计划：[2026-10-23-linux-ingest-gateway.md](../superpowers/plans/2026-10-23-linux-ingest-gateway.md)
- 验收 JSON：[s1-linux-ingest-gateway-2026-10-23.json](s1-linux-ingest-gateway-2026-10-23.json)
- 既有本机模型验收：[s1-local-model-acceptance-2026-10-23.md](s1-local-model-acceptance-2026-10-23.md)
- 既有冻结 embedding 身份：[s1-frozen-qwen-embedding-2026-10-21.md](s1-frozen-qwen-embedding-2026-10-21.md)
