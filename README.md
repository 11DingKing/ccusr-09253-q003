# 实训学时合规与冻结服务

该服务汇聚学员签到、导师确认和请假修正事件，按培养方案与时区重放学时状态，并保存可追溯的学期冻结快照。项目还提供导师分配、证明材料、豁免复核、规则版本、名额、通知和数据留存等领域模块，供后续业务扩展时复用统一的状态与审计约束。

## 学时口径规则版本（Rule Versions）

学期中可以发布新的学时口径（达标线、每学时秒数、每日封顶、实习是否需要导师确认），核心原则是 **"事件按当时有效的规则计算，规则升级不整体重算"**：

- **导入即固化**：事件导入时按其业务发生时间（签到取 `check_in_at`，其他事件取 `occurred_at` 或接收时间）解析当时处于适用窗口 `[effective_at, end_at)` 的规则版本，id 固化写入事件行；重放只使用该绑定版本的口径快照。
- **生命周期**：`draft`（起草）→ 两名与起草人不同的审核人审批（`pending_approval`）→ `scheduled`（定时生效）/ `active`（立即生效）→ 被新版本取代为 `superseded`，或紧急 `withdrawn`。
- **定时生效**：全部以 UTC 存储与比较（请求可携带任意时区偏移）；启动时扫描补激活（重启恢复），运行期由守护线程按 `RULE_SCHEDULER_INTERVAL_SECONDS`（默认 5 秒）轮询；迟到激活时若生效时刻之后已发布更新口径（如紧急回滚），定时版本自动作废而不回溯覆盖。
- **紧急撤回**：只把窗口上界截断到撤回时刻——撤回后的新事件不再适用该版本，但撤回前窗口内的历史与**迟到事件**仍按该版本绑定；已冻结快照永不改变。
- **回滚**：`POST .../rules/{id}/rollback` 截断当前版本并以目标版本口径立即生成一个新的 active 版本（`rollback_of` 留痕）。
- **并发发布**：每个培养方案至多一个 active、同一时刻至多一个 scheduled（数据库部分唯一索引 + 条件更新），竞争落败返回 409。
- **可解释性**：快照内嵌 `rule_catalog`，学生明细包含 `seconds_by_rule`、`adjustments_by_rule`、每日 `seconds_by_rule` 及每条签到的 `rule_version_id`/`rule_spec`，历史冻结快照可自解释每一条规则来源。

### 规则相关接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/plans/{p}/rules` | 创建草稿（口径、生效时间、起草人） |
| POST | `/api/plans/{p}/rules/{id}/approve` | 双人审核（不同审核人，且非起草人） |
| POST | `/api/plans/{p}/rules/{id}/withdraw` | 紧急撤回（仅影响未结算区间） |
| POST | `/api/plans/{p}/rules/{id}/rollback` | 回滚到指定版本口径，立即发布新版本 |
| GET | `/api/plans/{p}/rules` / `.../{id}` | 版本列表 / 详情 |
| GET | `/api/plans/{p}/rules/{id}/audit` / `.../approvals` | 审计链 / 审批记录 |
| GET | `/api/plans/{p}/rules/{a}/diff/{b}` | 字段级规则差异 |
| POST | `/api/plans/{p}/rules-preview` | 影响预览（候选口径自指定时刻起重放，不落库） |

事件导入响应新增 `bindings`（event_id → 固化的规则版本 id，`null` 为基线口径）。

## 运行方式

默认数据保存在项目目录的 SQLite 文件中。安装依赖后执行 `uvicorn app.main:app --host 127.0.0.1 --port 8000`，健康检查地址为 `/health`，业务接口位于 `/api`。

## 测试

```bash
python3 -m pytest -q
```

## 编译检查

```bash
python3 -m compileall -q app tests
```

测试覆盖事件幂等导入、跨时区与跨日学时合并、实习确认、负向修正、冻结快照和差异查询；规则版本部分额外覆盖双人审核、定时生效与重启恢复、跨时区生效边界、版本分叉、紧急撤回后的迟到事件绑定、影响预览、回滚与并发发布竞争。运行过程中不需要单独的数据库或网络服务。
