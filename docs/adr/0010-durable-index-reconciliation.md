# ADR-0010: 持久计划、独立提交与读回发布

- Status: Accepted
- Date: 2026-10-09
- Affects: ChunkManifest、source artifacts、ingest runtime、PG/vector readback

索引manifest复用唯一状态机，不增加平行IndexRun。原文件、冻结表示和完整chunk计划先存范围隔离、内容寻址的文件产物；计划ref/digest及完整source/profile链先提交PG pending，才执行两侧写入。

每个chunk的ChunkProjection包含完整owner/version/index身份、原文SHA-256、搜索字段摘要、全局/页内坐标与表示边界。PG和vector各自幂等写入并读回同一冻结projection，返回非空、无重复的摘要。vector还校验实际point namespace和非空finite、维度匹配的向量；这证明持久索引对应计划，不证明embedding语义效果。

只有两侧摘要均等于计划预期、当前执行代次仍有效，才能在短PG事务里写pg_digest/vector_digest/reconciled_at并将manifest设indexed、CAS切换Document.active_index_id。外部I/O不持DB行锁。失败保留计划和已完成阶段；重试同index的原文/身份不变。旧executor无法提交发布，部分vector写入不自动可检索。

ArtifactStore实际校验原文件及表示字节摘要，计划逐块验证实际原文slice、页内/全局坐标和parent关系。PG只存摘要/引用。Noop不能产生读回事实，也不能indexed。API在准备/恢复未接通时明确不可用；测试hash向量没有模型质量含义。

回滚保留原文、计划和旧active索引指针。0011存在读回事实/已发布新索引时拒绝删除证明字段；空测试库可回退。已有vector不原地混用或删除，用新profile/影子索引验收后切换。全协议仍需真实数据库、vector和进程故障验证，不能凭接口或mock宣称完成。
