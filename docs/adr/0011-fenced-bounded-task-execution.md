# ADR-0011: 短租约事务与可终止任务进程

- Status: Accepted
- Date: 2026-10-09
- Affects: Task、worker dispatch、isolated task process、ingest execution fence

领取与结果提交使用短事务；外部I/O不在领取事务或数据库锁内执行。领取产生不可变任务信封（权威tenant、task、kind、attempt），处理器不能修改这些身份。任务payload仅是资源引用，处理器仍验证其完整归属。

Task增加timeout_seconds、deadline_at、cancel_requested_at、finished_at、error_code。首次领取设置绝对deadline；重试/回收不延长总deadline。heartbeats只在同tenant/task/attempt、leased、未取消、未超时条件下续租，且不超过deadline。任何已过期lease都失效，不能等后继领取才失效。

合法状态：pending/retryable→leased→completed/failed/cancelled/timed_out。pending/retryable取消立即终态，leased记录取消意图，运行负责人终止执行并提交终态。超时和持续崩溃的任务有确定终态。只领取已注册kind；参数/权限/未知故障不重试，已分类瞬时故障在attempt和绝对时间预算内重试。

硬deadline/取消使用独立子进程，通过服务器注册的argv执行，不接受payload选择shell/命令。进程组被终止后等待退出，再上报terminal；不能把线程future.cancel当成任务停止。父进程短事务续租并观察取消/失效；子进程业务事务重复fence，旧执行者不能发布索引。进程运行环境和秘密由受控配置构建，不能继承未经筛选的任意请求或租户秘密。

本轮先修类型、存储和进程生命周期接口，再串行接线真实registered任务。默认workflow/train仍不可用；ingest只有gateway/profile/隔离/数据库准备满足时才启用。没有真实handler的角色不注册空成功。

验证至少覆盖：expired lease未回收也拒绝完成、旧attempt、总deadline不被重试延长、取消/超时/崩溃终态、无DB锁跨业务执行、真实本地子进程终止及资源释放。mock存储不证明PG并发/RLS；实际worker仍需独立临时库验收。
