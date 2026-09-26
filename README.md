# 实训学时合规与冻结服务

该服务汇聚学员签到、导师确认和请假修正事件，按培养方案与时区重放学时状态，并保存可追溯的学期冻结快照。项目还提供导师分配、证明材料、豁免复核、规则版本、名额、通知和数据留存等领域模块，供后续业务扩展时复用统一的状态与审计约束。

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

测试覆盖事件幂等导入、跨时区与跨日学时合并、实习确认、负向修正、冻结快照和差异查询；运行过程中不需要单独的数据库或网络服务。

## 规则版本与生效区间

学时口径（`seconds_per_lesson` 学时换算、`confirmation_required_types` 需导师确认的活动类型、`daily_cap_seconds` 每日计入上限）以规则版本形式发布，已发生的事件不会被规则升级整体重算：

- **生效区间**：每个已发布版本自 `effective_from`（UTC 绝对瞬间，接受任意时区偏移并按绝对时间比较）起生效，直到下一版本生效。未覆盖的时段回落到内置基线口径 `baseline`（45 分钟/学时、internship 需确认、无每日上限）。
- **导入定版**：导入事件时按事件发生时刻（签到取 `check_in_at`）解析适用版本并固定在事件上（`events.rule_id`），导入响应的 `pinned` 字段返回每条事件的定版结果；迟到事件因此始终按发生时的口径结算。
- **生命周期**：`draft`（可改参数）→ `in_review`（提交审核）→ `approved`（两名不同审核人批准，拟稿人不能自审）→ `scheduled`（定时生效，不允许追溯生效）→ 到期自动 `active`；进程重启时由启动恢复扫描补登到期激活。
- **紧急撤回**：`withdraw` 只影响尚未结算的区间——最近一次冻结截止点之前的事件保持原定版，之后的事件按撤回后的时间线重新定版到回退版本；历史冻结快照仍保留该版本的参数与生效区间，可解释每一条记录的规则来源。
- **并发发布**：同一规则的状态迁移用条件更新串行化，同一生效瞬间由 `(plan_version, effective_from)` 唯一约束保证只有一个版本占用；并发重复发布幂等返回已发布状态。
- **重放结算**：重放按事件定版分组，各版本区间独立合并、套用每日上限与学时换算后汇总（分段结果见学员 `rule_segments`），因此不同口径不会互相侵蚀。

### 规则接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/plans/{pv}/rules` | 新建草稿 |
| GET | `/api/plans/{pv}/rules` | 版本列表 |
| GET | `/api/plans/{pv}/rules/applicable?at=` | 查询某时刻适用版本 |
| GET/PATCH | `/api/plans/{pv}/rules/{rule_id}` | 详情（含审计轨迹）/ 草稿期修改 |
| POST | `/api/plans/{pv}/rules/{rule_id}/submit` | 提交审核 |
| POST | `/api/plans/{pv}/rules/{rule_id}/approve` | 审核（两人通过自动生效待发布） |
| POST | `/api/plans/{pv}/rules/{rule_id}/schedule` | 定时生效发布 |
| POST | `/api/plans/{pv}/rules/{rule_id}/withdraw` | 紧急撤回（仅影响未结算区间） |
| GET | `/api/plans/{pv}/rules/{rule_id}/diff/{other}` | 参数级规则差异（支持 `baseline`） |
| POST | `/api/plans/{pv}/rules/{rule_id}/preview` | 影响预览（只读，对比假设生效后的学员差异） |
| POST | `/api/plans/{pv}/rules/{rule_id}/rollback` | 以历史版本或基线口径创建回滚草稿 |
