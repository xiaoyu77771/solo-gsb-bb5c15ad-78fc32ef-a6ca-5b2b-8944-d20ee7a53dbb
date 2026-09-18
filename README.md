# Feature Flag Decision API

供本地应用调用的功能开关判定服务（Python + FastAPI + SQLite）。

每个开关包含：

- **有序属性规则**：按 `priority` 升序判定，返回**首个命中**规则指定的变体；
- **多个加权变体**：无规则命中时，以 `flag_key` 与 `user_id` 做稳定哈希，映射到 `0-99` 桶，按权重区间选择变体；
- 变体**权重总和必须为 100**；
- 规则所需属性**缺失（或值为 `null`）一律视为未命中**，继续判定下一条；
- 同一用户在配置不变时判定结果**稳定**（哈希输入只有 `flag_key:user_id`）；
- 每次创建 / 更新在**同一事务**中保存一份**不可变配置版本**，历史版本可查询、可指定用于判定；
- 支持**版本化生效窗口（activation_window）**：按所选配置版本的 `[active_from, active_until)` 判定，窗口外跳过 override、规则与分桶，直接返回兜底变体（`decision_path="inactive"`）；窗口随当前配置与每个历史版本一同保存；
- 支持**按用户的临时强制变体（override）**：未指定 `config_version` 时优先于规则与分桶，可带过期时间，到期 / 变体被移除后自动失效；
- 判定响应包含**命中规则、分桶值、最终变体的完整解释链**；
- 每次判定落库为快照，可按 `request_id` 回查；支持 `request_id` **幂等重放**；
- 支持**多开关发布批次**：一次提交多个开关候选与共用预览用户生成待提交批次（含逐开关影响预览），确认后在单一事务中再次核对各自版本并原子发布，任一冲突整体 `409`、不部分写入，重复提交幂等。

## 一条命令启动

```bash
docker compose up --build
```

服务在 http://localhost:8000 启动，SQLite 数据保存在 Docker 卷 `flag-data` 中。

- Swagger 文档：http://localhost:8000/docs
- 健康检查：http://localhost:8000/healthz
- 首次启动自动写入演示开关 `new_checkout`（员工 / 白名单地区走规则，其余 90/10 分桶）。

不使用容器时（需 Python 3.11+）：

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

可通过环境变量 `DATABASE_URL` 覆盖数据库位置，默认 `./data/flags.db`。

## 快速体验

```bash
# 1) 无规则命中 -> 稳定哈希分桶（同一 user_id 多次调用结果一致）
curl -sS -X POST http://localhost:8000/api/v1/flags/new_checkout/evaluate \
  -H 'Content-Type: application/json' \
  -d '{"user_id":"user-1001","context":{}}'

# 2) priority=1 的 employee 规则命中（属性缺失时该规则视为未命中）
curl -sS -X POST http://localhost:8000/api/v1/flags/new_checkout/evaluate \
  -H 'Content-Type: application/json' \
  -d '{"user_id":"user-1001","context":{"employee":true,"region":"us"}}'

# 3) 用返回的 request_id 查询判定快照
curl -sS http://localhost:8000/api/v1/decisions/<request_id>

# 4) 给单个用户写入临时强制变体：再次判定直接返回 on（decision_path=override）
curl -sS -X PUT http://localhost:8000/api/v1/flags/new_checkout/overrides/user-1001 \
  -H 'Content-Type: application/json' \
  -d '{"variant_key":"on","reason":"客服补偿","expires_at":"2026-09-18T10:00:00Z"}'
curl -sS -X POST http://localhost:8000/api/v1/flags/new_checkout/evaluate \
  -H 'Content-Type: application/json' \
  -d '{"user_id":"user-1001","context":{}}'

# 撤销后该用户恢复规则 / 分桶判定
curl -sS -X DELETE http://localhost:8000/api/v1/flags/new_checkout/overrides/user-1001
```

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/v1/flags` | 创建 / 更新开关（已存在则 `version + 1`，同事务保存不可变版本） |
| `GET` | `/api/v1/flags` | 列出全部开关 |
| `GET` | `/api/v1/flags/{flag_key}` | 查看单个开关 |
| `DELETE` | `/api/v1/flags/{flag_key}` | 删除开关（版本历史与 override 一并删除） |
| `PUT` | `/api/v1/flags/{flag_key}/overrides/{user_id}` | 写入 / 更新按用户临时强制变体（variant_key、reason、可选 expires_at） |
| `DELETE` | `/api/v1/flags/{flag_key}/overrides/{user_id}` | 撤销按用户临时强制变体 |
| `GET` | `/api/v1/flags/{flag_key}/versions` | 列出全部配置版本（按版本号升序） |
| `GET` | `/api/v1/flags/{flag_key}/versions/{version}` | 查看指定版本的完整配置 |
| `POST` | `/api/v1/flags/{flag_key}/rollback` | 将指定历史版本复制为新当前版本（乐观版本检查，返回 `201`） |
| `POST` | `/api/v1/flags/{flag_key}/evaluate` | 判定（推荐，flag_key 在路径中，可指定 `config_version`） |
| `POST` | `/api/v1/flags/{flag_key}/preview-impact` | 发布前候选配置影响预览（只读，不写版本 / 快照） |
| `POST` | `/api/v1/releases` | 创建多开关发布批次（多候选配置 + 共用预览用户 + 各自 `expected_version`，只生成待提交批次） |
| `POST` | `/api/v1/releases/{release_id}/commit` | 提交批次（单事务再核版本、原子发布，任一冲突 `409` 不部分写入；重复提交幂等） |
| `GET` | `/api/v1/releases/{release_id}` | 查询批次状态、逐开关预览摘要与提交后的版本号 |
| `POST` | `/api/v1/evaluate` | 判定（flag_key 在请求体中） |
| `GET` | `/api/v1/decisions` | 判定快照审计列表（组合过滤 + 不透明游标分页） |
| `GET` | `/api/v1/decisions/{request_id}` | 按 request_id 查询单条判定快照 |
| `GET` | `/healthz` | 健康检查 |

### 创建开关

```bash
curl -sS -X POST http://localhost:8000/api/v1/flags \
  -H 'Content-Type: application/json' \
  -d '{
    "flag_key": "btn_color",
    "description": "按钮颜色实验",
    "variants": [
      {"key": "red",  "weight": 20},
      {"key": "blue", "weight": 80}
    ],
    "rules": [
      {"priority": 1, "attribute": "region",   "operator": "eq", "values": ["cn"], "variant_key": "red"},
      {"priority": 2, "attribute": "age",      "operator": "gte", "values": [65],  "variant_key": "red"},
      {"priority": 3, "attribute": "user_tier","operator": "in",  "values": ["vip","svip"], "variant_key": "blue"}
    ]
  }'
```

校验规则（不满足返回 `422`）：

- 变体权重之和必须恰好为 `100`，权重允许为 `0`（该变体永不被分桶选中）；变体 key 不可重复；
- 规则 `priority` 不可重复（数字越小越先判定）；
- 规则的 `variant_key` 必须是已配置的变体；
- 可选的 `activation_window`：`active_from` / `active_until` 至少提供一个，时间必须**带时区**（服务统一转 UTC 存储），两者同时提供时 `active_from` 必须**早于** `active_until`，`inactive_variant_key` 必须是已配置的变体（详见[版本化生效窗口](#版本化生效窗口)）。

### 版本化生效窗口

创建 / 更新开关时可提供可选的 `activation_window`，让配置只在指定时间窗口内生效：

```bash
curl -sS -X POST http://localhost:8000/api/v1/flags \
  -H 'Content-Type: application/json' \
  -d '{
    "flag_key": "promo_2026",
    "description": "双十一活动开关",
    "variants": [
      {"key": "on",  "weight": 100},
      {"key": "off", "weight": 0}
    ],
    "rules": [],
    "activation_window": {
      "active_from": "2026-11-11T00:00:00+08:00",
      "active_until": "2026-11-12T16:00:00Z",
      "inactive_variant_key": "off"
    }
  }'
```

字段与校验：

| 字段 | 说明 |
| --- | --- |
| `active_from` | 可选，生效起点（**含**），ISO 8601 且**带时区**（接受 `Z` 或 `+08:00` 等偏移，服务统一转 UTC） |
| `active_until` | 可选，生效终点（**不含**），要求同上；两者同时提供时必须满足 `active_from < active_until` |
| `inactive_variant_key` | 必填，窗口外返回的兜底变体，必须存在于 `variants` 中 |

- `active_from` / `active_until` **至少提供一个**，都不提供、时间不带时区、区间倒置或兜底变体不存在均返回 `422`；
- 生效区间为 `[active_from, active_until)`：起点含、终点不含，缺省的一侧表示该方向不设限；
- 窗口随**当前配置**与**每个不可变历史版本**一同保存（`GET /api/v1/flags/{flag_key}` 与 `GET .../versions/{version}` 均返回 `activation_window`）；更新时不提供 `activation_window` 即视为**清除窗口**（整体覆盖语义）；回滚会把目标历史版本的窗口一并复制为新版本；本功能上线前的老数据一律视为**无窗口**。

判定行为：

- 判定采用**所选配置版本**的窗口（指定 `config_version` 时用该历史版本的窗口）；
- **窗口外**：跳过 override、规则与分桶，直接返回 `inactive_variant_key`，响应 `decision_path="inactive"`、`matched_rule=null`、`bucket_value=null`，解释链为 `activation_window_loaded` → `activation_window_inactive`，并注明触发的边界（`active_from` 尚未到达 / `active_until` 已经越过）；
- **窗口内**：保持原有语义（override → 规则 → 分桶）；
- 相同 `request_id` 仍直接重放已存快照，**不按当前时间重算**：即使首次判定在窗口外、之后窗口已开启，重放依旧返回首次结果。

```bash
# 窗口外判定（当前时刻未到 active_from）
curl -sS -X POST http://localhost:8000/api/v1/flags/promo_2026/evaluate \
  -H 'Content-Type: application/json' \
  -d '{"user_id":"user-1001","context":{}}'
# -> {"variant_key":"off","decision_path":"inactive","matched_rule":null,"bucket_value":null,...}
```

### 配置版本（不可变历史）

每次创建 / 更新都会在**同一事务**中把完整配置追加为一条不可变版本记录（`flag_config_versions` 表），历史版本**不随后续更新改变**。

```bash
# 列出全部版本（按版本号升序）
curl -sS http://localhost:8000/api/v1/flags/btn_color/versions

# 查看某个历史版本的完整配置（版本不存在返回 404）
curl -sS http://localhost:8000/api/v1/flags/btn_color/versions/1
```

响应示例：

```json
{
  "flag_key": "btn_color",
  "version": 1,
  "description": "按钮颜色实验",
  "variants": [{"key": "red", "weight": 20, "description": ""}, {"key": "blue", "weight": 80, "description": ""}],
  "rules": [{"priority": 1, "attribute": "region", "operator": "eq", "values": ["cn"], "variant_key": "red"}],
  "activation_window": null,
  "created_at": "2026-09-17T08:00:00Z"
}
```

判定时可通过可选字段 `config_version` 钉住某个历史版本：使用该版本的**规则与权重**计算，并把该版本号写入判定快照；缺省使用最新版；版本不存在返回 `404`。

```bash
# 用 v1 的规则与权重判定（即使当前已发布到 v3）
curl -sS -X POST http://localhost:8000/api/v1/flags/btn_color/evaluate \
  -H 'Content-Type: application/json' \
  -d '{"user_id":"user-1001","context":{"region":"cn"},"config_version":1}'
```

删除开关会一并删除其版本历史；同名开关重建后版本号从 `1` 重新开始。

### 版本回滚（历史版本复制为新版本）

需要把线上配置恢复为某个历史版本时，调用 `POST /api/v1/flags/{flag_key}/rollback`。回滚**不会删除或覆盖任何历史版本**，而是把目标历史版本的完整配置复制一份，在**同一事务**中追加为新的当前版本（版本号在当前版本上 `+1`）：

```bash
# 当前为 v3，把 v1 的配置复制为新的 v4 并立即生效
curl -sS -X POST http://localhost:8000/api/v1/flags/btn_color/rollback \
  -H 'Content-Type: application/json' \
  -d '{"target_version": 1, "expected_current_version": 3}'
```

请求字段：

| 字段 | 说明 |
| --- | --- |
| `target_version` | 要复制的历史版本号，必须 `>= 1`；不存在返回 `404` |
| `expected_current_version` | 调用方认为的当前版本号（乐观并发检查），必须 `>= 1` |

行为约定：

- 成功返回 `201`，响应体与创建 / 更新接口的 `FlagResponse` 结构一致，`version` 为新版本号，配置内容（`description` / `variants` / `rules`）与 `target_version` 完全相同；
- 仅当开关存在**且**实际当前版本等于 `expected_current_version` 时才写入；期望版本过期（已被其他发布推进）返回 **`409 Conflict` 且不改动任何数据**（版本历史也不追加），调用方重新读取当前版本后可重试；
- 开关不存在返回 `404`；开关存在但 `target_version` 历史版本不存在同样返回 `404`；
- 字段缺失或版本号小于 `1` 返回 `422`；
- `target_version` 等于当前版本时也允许调用，效果是追加一份与当前内容相同的新版本快照；
- 回滚产生的新版本与普通更新产生的版本没有区别，之后继续更新会在其版本号上顺序递增；回滚前的判定快照仍记录各自的 `config_version`，可用 `config_version` 继续钉住任意历史版本判定。

### 按用户临时强制变体（override）

需要对**单个用户**临时钉死判定结果（客服补偿、人工止损、定向灰度）时，可不改开关配置，直接写入按用户的 override：

```bash
# 写入：user-1001 在 new_checkout 上被强制为 on，2 小时后自动失效
curl -sS -X PUT http://localhost:8000/api/v1/flags/new_checkout/overrides/user-1001 \
  -H 'Content-Type: application/json' \
  -d '{
    "variant_key": "on",
    "reason": "客服补偿：人工强制新版结账页",
    "expires_at": "2026-09-18T10:00:00Z"
  }'

# 撤销
curl -sS -X DELETE http://localhost:8000/api/v1/flags/new_checkout/overrides/user-1001
```

请求字段：

| 字段 | 说明 |
| --- | --- |
| `variant_key` | 必填，强制返回的变体；必须是开关**当前配置**中存在的变体，否则 `404` |
| `reason` | 必填非空字符串，强制原因会随判定快照一起固化 |
| `expires_at` | 可选，ISO 8601 **带时区的未来 UTC 时间**（如 `2026-09-18T10:00:00Z`，也接受 `+08:00` 偏移，服务统一转 UTC）；缺省表示**不过期**；不是未来时间或不带时区返回 `422` |

响应示例（`PUT` 成功返回 `200`；同一路径重复写入为整体覆盖）：

```json
{
  "flag_key": "new_checkout",
  "user_id": "user-1001",
  "variant_key": "on",
  "reason": "客服补偿：人工强制新版结账页",
  "expires_at": "2026-09-18T10:00:00Z",
  "created_at": "2026-09-18T08:00:00Z",
  "updated_at": "2026-09-18T08:00:00Z"
}
```

判定行为：

- **仅在未指定 `config_version` 时**读取 override；钉住历史版本（`config_version`）的判定完全不受 override 影响；
- 命中条件：记录**未过期**（`expires_at` 缺省视为永不过期）**且** `variant_key` 仍存在于当前配置。命中后**跳过规则与分桶**，响应 `decision_path="override"`、`matched_rule=null`、`bucket_value=null`，解释链为 `override_loaded` → `override_selected`；
- **到期**或**变体已被配置更新移除**的记录不生效，判定正常走规则与分桶；记录仍保留在库中（变体之后重新加回当前配置会自动重新生效），用 `DELETE` 可显式撤销；
- 开关不存在、override 的变体不存在返回 `404`；`DELETE` 时开关或 override 不存在也返回 `404`；删除开关会一并删除其全部 override；
- 判定快照会**固化 override 的变体、原因与路径**（含到期时间）：即使之后 override 被修改、到期或删除，同一 `request_id` 重放仍返回首次结果。

```bash
# 写入 override 后判定（即使 context 命中 off 规则，也被强制为 on）
curl -sS -X POST http://localhost:8000/api/v1/flags/new_checkout/evaluate \
  -H 'Content-Type: application/json' \
  -d '{"user_id":"user-1001","context":{"employee":false}}'
# -> {"variant_key":"on","decision_path":"override","matched_rule":null,"bucket_value":null,...}
```

### 规则操作符

| operator | 语义 | values 用法 |
| --- | --- | --- |
| `eq` / `ne` | 等于 / 不等于 | 取 `values[0]` |
| `in` / `not_in` | 属于 / 不属于集合 | 整个 `values` |
| `gt` / `gte` / `lt` / `lte` | 数值比较 | 取 `values[0]`，两侧非数字视为未命中 |
| `prefix` / `suffix` / `contains` | 字符串前缀 / 后缀 / 包含 | 取 `values[0]`，两侧非字符串视为未命中 |

属性不存在、值为 `null`、或类型不兼容时，该规则**记为未命中并附原因**，继续判定后续规则。

### 判定响应与解释链

```json
{
  "request_id": "7f7c0807-...",
  "flag_key": "btn_color",
  "user_id": "user-1001",
  "config_version": 1,
  "matched_rule": null,
  "bucket_value": 42,
  "variant_key": "blue",
  "decision_path": "bucket",
  "explanation": [
    {"step": "rules_loaded", "detail": {"rule_count": 3, "evaluation_order": ["..."]}},
    {"step": "rule_evaluated", "detail": {"rule_index": 0, "result": "miss", "reason": "缺少属性 'region'，视为未命中"}},
    {"step": "no_rule_matched", "detail": {"reason": "所有规则均未命中，进入哈希分桶"}},
    {"step": "bucket_computed", "detail": {"hash_input": "btn_color:user-1001", "algorithm": "sha256[:8] % 100", "bucket_value": 42}},
    {"step": "variant_selected", "detail": {"selected_variant": "blue", "selected_range": [20, 100], "variant_ranges": ["..."]}}
  ]
}
```

- `decision_path = "rule"` 时 `matched_rule` 为命中规则、`bucket_value` 为 `null`；
- `decision_path = "bucket"` 时 `matched_rule` 为 `null`，`bucket_value` 为 `0-99`；
- `decision_path = "override"` 时（按用户临时强制变体命中）`matched_rule` 与 `bucket_value` 均为 `null`，解释链只含 `override_loaded` 与 `override_selected` 两步；
- `decision_path = "inactive"` 时（配置在生效窗口之外）`matched_rule` 与 `bucket_value` 均为 `null`，`variant_key` 为窗口的兜底变体，解释链只含 `activation_window_loaded` 与 `activation_window_inactive` 两步（含边界说明）；
- 配置更新会使 `config_version` 自增；**同一版本配置下，同一 `flag_key + user_id` 永远落入同一桶**。

### 幂等

判定请求可携带自定义 `request_id`。同一 `request_id` 的重复请求直接返回**首次判定快照**（即使第二次 context、`config_version` 不同，首次生效的 override 事后被修改、到期、删除，或生效窗口状态已随时间变化），适合客户端超时重试。若同一 `request_id` 搭配了不同的 `flag_key/user_id`，返回 `409`。不携带时服务自动生成 UUID。

### 发布前影响预览（只读）

发布候选配置前，可用 `POST /api/v1/flags/{flag_key}/preview-impact` 评估影响：服务对请求中的每个用户分别用**当前线上版本**和**候选配置**做只读判定，仅返回结果发生变化的用户。

- 请求体的 `variants` / `rules`（及可选的 `activation_window`）与[创建开关](#创建开关)**同结构、同校验**（权重和为 100、`priority` 唯一、规则引用的变体必须存在），不满足返回 `422`；
- 两侧判定都应用各自配置的生效窗口（当前版本用已保存的窗口，候选用请求中的窗口，整个请求共用同一判定时刻），窗口外一侧的结果为 `decision_path="inactive"` + 兜底变体；
- `users` 为 1～500 个评估对象，每个包含 `user_id` 与 `context`，`user_id` **不可重复**，为空、重复或超过 500 返回 `422`；
- 开关不存在返回 `404`；
- 预览**不会**创建配置版本、**不会**写入 `DecisionSnapshot`（可通过版本列表与 `/healthz` 的快照计数验证）；
- 候选与当前判定复用同一套稳定哈希分桶与“首条命中规则”语义。

```bash
curl -sS -X POST http://localhost:8000/api/v1/flags/btn_color/preview-impact \
  -H 'Content-Type: application/json' \
  -d '{
    "variants": [
      {"key": "red",  "weight": 0},
      {"key": "blue", "weight": 100}
    ],
    "rules": [
      {"priority": 1, "attribute": "region", "operator": "eq", "values": ["cn"], "variant_key": "red"}
    ],
    "users": [
      {"user_id": "user-1001", "context": {}},
      {"user_id": "user-1002", "context": {"region": "cn"}}
    ]
  }'
```

响应（`changes` 按请求顺序排列，仅包含 `variant_key`、`decision_path`、`matched_rule_index` 任一发生变化的用户）：

```json
{
  "flag_key": "btn_color",
  "current_version": 1,
  "total_users": 2,
  "changed_users": 1,
  "changes": [
    {
      "user_id": "user-1001",
      "current":   {"variant_key": "red",  "decision_path": "bucket", "matched_rule_index": null},
      "candidate": {"variant_key": "blue", "decision_path": "bucket", "matched_rule_index": null}
    }
  ],
  "migration_counts": {"red": {"blue": 1}}
}
```

- `current_version` 为当前线上版本，预览不会使版本号自增；
- `migration_counts` 按“当前变体 → 候选变体”汇总迁移人数（仅统计变体发生变化的用户；变体不变、仅判定路径或命中规则变化的用户计入 `changed_users`，但不进入迁移计数）。

### 多开关发布批次（预览 + 乐观并发 + 原子提交）

需要把**多个开关**作为一个批次一起发布时，使用 `POST /api/v1/releases` 分两步完成：先创建**待提交批次**（只做校验与影响预览，不改动当前配置），确认无误后再 `commit` 在**单一数据库事务**中原子发布。

**第一步：创建批次**。请求体包含 1～20 个开关候选（`flags`，批次内 `flag_key` 不可重复）与 1～500 个互异预览用户（`users`，批次内各开关共用同一批用户）。每个候选除 `variants` / `rules` / 可选 `activation_window` / `description` 外，还必须带各自的 `expected_version`（调用方认为的当前版本号）：

```bash
curl -sS -X POST http://localhost:8000/api/v1/releases \
  -H 'Content-Type: application/json' \
  -d '{
    "idempotency_key": "checkout-release-2026-09-18",
    "flags": [
      {
        "flag_key": "new_checkout",
        "expected_version": 1,
        "description": "新版结账页全量",
        "variants": [
          {"key": "on",  "weight": 100},
          {"key": "off", "weight": 0}
        ],
        "rules": []
      },
      {
        "flag_key": "pay_promo",
        "expected_version": 3,
        "variants": [
          {"key": "show", "weight": 20},
          {"key": "hide", "weight": 80}
        ],
        "rules": [
          {"priority": 1, "attribute": "region", "operator": "in",
           "values": ["cn", "sg"], "variant_key": "show"}
        ]
      }
    ],
    "users": [
      {"user_id": "user-1001", "context": {"region": "us"}},
      {"user_id": "user-1002", "context": {"region": "cn"}}
    ]
  }'
```

创建行为约定：

- 候选配置与[创建开关](#创建开关)**同结构、同校验**（权重和为 100、`priority` 唯一、规则与窗口兜底变体必须存在），不满足返回 `422`；`flags` 为空 / 超 20 个、批次内 `flag_key` 重复、`users` 为空 / 超 500 / `user_id` 重复同样返回 `422`；
- 创建时逐开关核对：开关不存在返回 `404`；当前版本与 `expected_version` 不一致返回 `409`，两种情况都**不创建批次**；
- 复用[发布前影响预览](#发布前影响预览只读)的同源只读判定生成**逐开关**预览摘要（`changes` / `migration_counts`，语义与单开关预览完全一致），**不改动任何当前配置、不追加不可变版本、不写判定快照**，批次状态为 `pending`；
- 成功返回 `201`，响应中每个 `items[]` 含 `expected_version`、创建时的实际线上版本 `current_version_at_create`、预览摘要；此时 `committed_version` 为 `null`。

响应示例（节选）：

```json
{
  "release_id": "0c13bc0ca476442094955c973ef74ed6",
  "status": "pending",
  "total_flags": 2,
  "total_users": 2,
  "committed_at": null,
  "items": [
    {
      "flag_key": "new_checkout",
      "expected_version": 1,
      "current_version_at_create": 1,
      "total_users": 2,
      "changed_users": 2,
      "changes": [
        {"user_id": "user-1001",
         "current":   {"variant_key": "off", "decision_path": "bucket", "matched_rule_index": null},
         "candidate": {"variant_key": "on",  "decision_path": "bucket", "matched_rule_index": null}}
      ],
      "migration_counts": {"off": {"on": 2}},
      "committed_version": null
    }
  ]
}
```

**第二步：提交批次**。在**单一数据库事务**中再次核对全部开关的当前版本，全部一致才为每个开关追加不可变版本（各自 `version+1`）并更新当前配置，批次翻为 `committed`：

```bash
curl -sS -X POST http://localhost:8000/api/v1/releases/0c13bc0ca476442094955c973ef74ed6/commit
```

- 任一开关在创建后被其他发布推进（当前版本 ≠ `expected_version`）或已被删除，整体返回 **`409 Conflict`，事务回滚、不得部分写入**（不会有任何开关追加版本），`detail` 会列出全部冲突开关；批次保持 `pending`，待调用方按当前版本重新建批次；
- 成功返回 `200`，批次 `status="committed"`、`committed_at` 有值，各 item 的 `committed_version` 为该开关**提交后新追加的不可变版本号**（如上例 `new_checkout` 为 2、`pay_promo` 为 4）；此后缺省判定立即按新版本执行，历史版本列表中同样能查到这些新版本；
- 对**同一批次重复提交是幂等的**：已提交则直接返回首次提交结果，不会再次推进版本号；批次不存在返回 `404`。

**查询批次**：`GET /api/v1/releases/{release_id}` 返回批次状态、逐开关预览摘要与提交后的版本号（结构与创建 / 提交响应一致）；批次不存在返回 `404`。

```bash
curl -sS http://localhost:8000/api/v1/releases/0c13bc0ca476442094955c973ef74ed6
```

**创建幂等**：创建请求可携带 `idempotency_key`（1～64 个字母数字及 `_.:-`）。相同键 + **相同请求体**的重复创建直接返回首次创建结果（首次 `201`，重放 `200`，`release_id` 不变），适合客户端超时重试；相同键搭配**不同请求体**返回 `409`。不携带时每次创建都是独立的新批次。

### 判定快照审计列表（游标分页）

`GET /api/v1/decisions` 与按 `request_id` 的单条查询并存，用于审计 / 回溯历史判定，支持以下查询参数组合过滤：

| 参数 | 说明 |
| --- | --- |
| `flag_key` | 仅返回指定开关的快照 |
| `user_id` | 仅返回指定用户的快照 |
| `variant_key` | 仅返回命中指定变体的快照 |
| `decided_from` | 判定时间下界（含），ISO 8601，如 `2026-09-17T00:00:00Z` |
| `decided_to` | 判定时间上界（含），ISO 8601，支持 `+08:00` 偏移 |
| `limit` | 每页条数，`1～100`，默认 `20` |
| `cursor` | 上一页返回的不透明 `next_cursor` |

结果按 `decided_at`、`request_id` 双列倒序返回（同一时间戳由 `request_id` 裁决，保证不重不漏）；响应项结构与单条查询的 `SnapshotResponse` 完全一致，无更多数据时 `next_cursor` 为 `null`。

```bash
# 首页：查 btn_color 在 9 月对 alice 的判定，每页 50 条
curl -sS 'http://localhost:8000/api/v1/decisions?flag_key=btn_color&user_id=alice&decided_from=2026-09-01T00:00:00Z&decided_to=2026-10-01T00:00:00Z&limit=50'

# 翻页：原样回传 next_cursor（过滤条件必须与首页完全一致，limit 可调整）
curl -sS 'http://localhost:8000/api/v1/decisions?flag_key=btn_color&user_id=alice&decided_from=2026-09-01T00:00:00Z&decided_to=2026-10-01T00:00:00Z&limit=50&cursor=<next_cursor>'
```

响应示例：

```json
{
  "items": [
    {
      "request_id": "7f7c0807-...",
      "flag_key": "btn_color",
      "user_id": "alice",
      "request_context": {"region": "cn"},
      "config_version": 1,
      "bucket_value": null,
      "matched_rule_index": 0,
      "variant_key": "red",
      "decision_path": "rule",
      "explanation": [ {"step": "rules_loaded", "detail": {}} ],
      "decided_at": "2026-09-17T08:00:00.123456"
    }
  ],
  "next_cursor": "eyJ2IjoxLi4ufQ.<hmac签名>"
}
```

游标规则：

- 游标**不透明且带 HMAC 签名**，只记录上一页边界（`decided_at` + `request_id`）、过滤条件与上界高水位，客户端不应解析或构造；
- 翻页时过滤条件（`flag_key` / `user_id` / `variant_key` / `decided_from` / `decided_to`）必须与首页一致；**条件不一致、游标损坏或被篡改、时间区间倒置（from 晚于 to）一律返回 `422`**；
- 首页请求发起时冻结高水位（最大快照 id），**翻页期间新写入的快照不会进入后续页**，页与页之间是一个稳定快照视图；重新从首页开始则能看到最新数据；
- 服务重启后旧游标失效（按损坏处理，返回 `422`），重新取首页即可。

## 分桶算法

```
bucket = int.from_bytes(sha256(f"{flag_key}:{user_id}")[:8], "big") % 100
```

随后按变体配置顺序累加权重切分 `[0, 100)`：上例中桶 `0-19` → `red`（20%），`20-99` → `blue`（80%）。变体顺序或权重变化即视为配置发布（`version` 自增），此前的稳定性不再保证——这是刻意的设计：发布即重新分桶。

## 项目结构

```
.
├── app/
│   ├── main.py        # FastAPI 路由、错误处理、幂等、种子数据
│   ├── engine.py      # 规则判定 + 稳定哈希分桶 + 解释链（纯函数，易测）
│   ├── schemas.py     # Pydantic 请求/响应模型与配置校验
│   ├── cursor.py      # 审计列表的不透明签名游标（边界 + 过滤绑定 + 高水位）
│   ├── crud.py        # 配置、版本、override 与快照持久化（含分页查询与索引补齐）
│   ├── models.py      # SQLAlchemy ORM（flag_configs / flag_config_versions / flag_overrides / decision_snapshots / flag_releases / flag_release_items）
│   └── database.py    # 引擎与会话
├── tests/             # pytest：引擎单测 + API 集成测试（含 override、生效窗口与多开关发布批次用例）
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## 运行测试

```bash
pip install -r requirements.txt pytest httpx
python -m pytest -q
```

## 说明与边界

- 服务定位为**本地 / 单机**使用：SQLite + 单实例，未做多副本一致性；
- 判定快照只增不删（删除开关不影响历史快照回查）；配置版本与按用户 override 随开关一并删除；
- 快照保存判定时的 `config_version` 与 `decision_path`（含 override 的变体与原因）；幂等重放重建 `matched_rule` 详情时优先取该版本的历史配置，版本记录缺失（如开关被删除重建）时回退到当前版本配置；
- 本功能上线前已存在的开关，启动时会自动为当前版本补齐一条版本记录；升级到含 override 的版本时，老库会自动给快照表补 `decision_path` 列并回填 `rule` / `bucket`；升级到含生效窗口的版本时，老库会自动给 `flag_configs` / `flag_config_versions` 补 `activation_window_json` 列（老数据为 `NULL`，视为无窗口）。以上迁移均为幂等，重复启动无副作用。
