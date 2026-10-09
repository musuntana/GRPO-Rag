# ADR-0009: 不可变来源链与索引发布

- Status: Accepted
- Date: 2026-10-09
- Affects: 0010 migration，KB/search/vector/retrieval/citation contracts

## 原因

S0 现有 chunk/doc/page 字段不能区分同文档的内容版本、渲染版本与索引版本。按 doc_id 记录页数或只比 chunk UUID 会让错误版本的引用获得结构通过。没有真实来源时不能虚构默认版本。

## 决策

Document 是持续业务对象。DocumentVersion 保存原文件 SHA-256、范围内不可变引用、media_type，并绑定 tenant/KB/document。DocumentRepresentation 绑定该内容版本，保存实际表示 SHA-256、范围内引用、parser/render版本、字符数、分页策略及真实页数；DOCX没有固定渲染时只能 token_window。复用 ChunkManifest 的 UUID 作为 index_id，绑定表示与 chunker/embedding配置。不得新增平行索引状态机。

企业 stable/hot chunk 的 document_version_id、representation_id、index_id同批贯通PG、vector payload、候选、Evidence、Citation与Gold。引用的确定范围由表示字符数和实际送入generator的原文跨度决定。来源身份与坐标不从 enriched/refined 文本推断；语义未运行明确 not_checked。

DocumentRecord.active_index_id指向已对账、同文档的发布manifest。默认检索只返回该索引的完整来源身份。发布采用比较并交换，失败保留上一可用指针。个人记忆不参与企业来源链。

新记录使用包含tenant/KB/document的复合FK并在仓储再次授权。版本与表示内容字段不可变；重解析/重切块创建新identity。企业chunk同id不能改来源或原文/坐标。PG保存控制信息及搜索文本，原文件/表示/完整计划存范围隔离文件产物。

## 迁移与回滚

旧行的版本字段保持NULL，不建立假的v1/digest；旧manifest标为unversioned，默认不可检索。原数据不删除，后续从实际来源重建并对账才能发布。0010降级在存在新版本数据时拒绝，避免丢失来源关联；清空的隔离测试库可验证schema回退。已有向量以新profile/影子集合重建，不能混用无版本payload。应用回退恢复上一完整版本的读路径与active指针，不能让旧写者修改新来源。

代价：旧来源缺失时保持不可用；需要重新索引，并增加版本治理。接受此代价，以免用兼容默认值产生无法核验的引用。当前S0只修契约和恢复；真实解析/provider与质量baseline仍属于S1。
