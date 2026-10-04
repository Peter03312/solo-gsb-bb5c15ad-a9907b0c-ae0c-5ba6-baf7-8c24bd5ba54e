# 事故复盘时间线（Retro Timeline）

供小组在本地协作的事故复盘时间线工具：记录事件的起始时间范围、持续分钟数与“先于”关系，
自动推算每个事件的可行时间窗并给出推算依据链。Python 标准库服务端 + SQLite 持久化 +
浏览器单页界面，无任何第三方依赖。

## 功能

- **事件建模**：每个事件包含起始时间范围（最早/最晚开始）、持续分钟数与备注。
- **“先于”约束**：A 先于 B 表示 A 必须在 B 开始前结束（A 开始 + A 持续 ≤ B 开始）。
  编辑关系时还可设置**前项结束 → 后项开始的允许间隔**：
  - `min_wait_minutes`（最短等待，非负整数分钟，默认 0）：B 开始 ≥ A 开始 + A 持续 + 最短等待；
  - `max_wait_minutes`（最长等待，非负整数分钟，留空表示不限）：B 开始 ≤ A 开始 + A 持续 + 最长等待；
  - 最长等待不得短于最短等待，非法输入（负数、小数、非整数、超长、上下界倒置）一律拒绝（HTTP 400）且**不改动任何记录**；
  - 未设置间隔的旧关系仍按旧语义“前项结束不晚于后项开始”推算；
  - 推算同时施加间隔上下界（前向/后向双向传播），可行时间窗与依据链中会体现间隔要求；
  - 保存、版本回看与恢复都会**保留每条关系当时的间隔设置**，互不覆盖。
- **时间窗推算**：前向/后向约束传播，给出每个事件的最早/最晚可行开始（即可行时间窗），
  并展示从输入到结论的完整依据链。
- **输入与结果分离保存**：原始输入存于 `events` / `relations` 表，每次保存的推算结果
  单独写入 `computations` 表（按复盘版本留存），互不覆盖。
- **版本回看与恢复**：每次保存/定稿成功时，除推算结果外另写一条**不可变输入快照**
  （标题、事件、“先于”关系）到 `snapshots` 表，与当次推算一一对应。网页和 HTTP API
  可列出各版本、回看当时输入与推算依据；把指定版本恢复时会**另存为当前复盘的新草稿
  版本**并重新推算，不覆盖任何旧版本。恢复须携带当前 `base_version`，版本过期沿用
  409 冲突语义。冲突或暂存输入允许恢复，但仍不得定稿。
- **旧库历史版本**：升级前只有推算结果、缺少完整输入快照的历史版本，会在版本列表中
  明确标记为「不可回看、不可恢复」，不会用当前输入冒充；老接口行为保持不变。
- **冲突处理**：约束冲突（窗口不可满足、关系成环、输入范围自相矛盾）时保留全部输入，
  指出冲突事件及双方依据链（成环时给出沿“先于”方向的**前向依据链**与后向回推的
  **相反依据链**），该版本**不得定稿**。
- **暂存事件**：缺少时间范围（或持续分钟数）的事件可暂存，其关联的“先于”关系
  **连同其间隔设置一并跳过推算**，并在页面上标明原因（“关联事件处于暂存状态，该‘先于’
  关系暂不参与推算”）；存在暂存事件时同样不得定稿。
- **多人协作并发控制**：基于 `reviews.version` 的乐观锁。携带旧版本号的提交会被
  拒绝（HTTP 409）并提示刷新，不会覆盖他人较新的记录。
- **定稿**：推算通过且无暂存事件时可定稿；定稿后再次修改会自动回到草稿状态。

## 容器启动

```bash
# 方式一：Docker Compose（推荐）
docker compose up --build
# 或旧版：docker-compose up --build

# 方式二：纯 Docker
docker build -t retro-timeline .
docker run -d -p 8000:8000 -v retro-timeline-data:/data --name retro-timeline retro-timeline
```

容器启动时自动初始化数据库（创建目录与表结构），无需手动迁移。

## 网页访问

启动后浏览器访问：**http://localhost:8000**（默认端口，见下方 `PORT` 配置）。

## 可配置项

| 环境变量 | 默认值（容器内） | 说明 |
|---|---|---|
| `PORT` | `8000` | 服务监听端口。Compose 中同时用于宿主机映射：`PORT=9000 docker compose up` 后访问 `http://localhost:9000` |
| `HOST` | `0.0.0.0` | 监听地址 |
| `DB_PATH` | `/data/timeline.db` | SQLite 数据库文件路径。容器内 `/data` 为挂载卷，删除容器数据不丢失 |

Compose 示例（`docker-compose.yml`）中已包含命名卷 `timeline-data` 用于持久化；
如改用宿主机目录，可将卷替换为 `./data:/data`。

## 本地开发（不用 Docker）

```bash
python3 app/server.py          # 默认监听 8000，数据库写入 ./data/timeline.db
PORT=9000 DB_PATH=/tmp/t.db python3 app/server.py
python3 tests/test_compute.py  # 推算引擎单元测试（含间隔上下界传播、成环相反依据链）
python3 tests/test_versions.py # 版本快照/回看/恢复、旧库兼容单元测试
python3 tests/test_http_api.py # HTTP API 集成测试（非法输入拒绝且不改记录、409 等）
```

## HTTP API 简介

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/reviews` | 复盘列表 |
| POST | `/api/reviews` | 新建复盘 `{title}` |
| GET | `/api/reviews/{id}` | 复盘快照：元信息 + 原始输入 + 最近一次推算结果 |
| PUT | `/api/reviews/{id}` | 整体保存并重新推算 `{base_version, title, events, relations}`；关系可带 `min_wait_minutes`/`max_wait_minutes` 间隔；非法间隔返回 400 且不改记录；版本过期返回 409 |
| POST | `/api/reviews/{id}/finalize` | 定稿 `{base_version}`；有冲突或暂存事件返回 400 |
| GET | `/api/reviews/{id}/versions` | 版本列表（含回看/恢复标记；旧库无快照版本 `reviewable=false`） |
| GET | `/api/reviews/{id}/versions/{v}` | 版本回看：当时标题/事件/关系输入 + 推算依据与状态；旧库无快照返回 422 |
| POST | `/api/reviews/{id}/versions/{v}/restore` | 恢复为新草稿版本 `{base_version}`；过期返回 409，旧库无快照返回 422 |
| DELETE | `/api/reviews/{id}` | 删除复盘 |

### 版本回看与恢复（HTTP 示例）

```bash
# 1) 列出可回看的版本
curl -s http://localhost:8000/api/reviews/1/versions
# {"current_version": 4, "versions": [
#   {"version": 4, "kind": "finalize", "state": "finalized",
#    "reviewable": true, "restorable": true, "status": "ok", ...},
#   {"version": 3, "kind": "save", "state": "draft", "reviewable": true, ...},
#   {"version": 2, "kind": null, "reviewable": false, "restorable": false,
#    "reason": "历史版本缺少完整输入快照，不可回看、不可恢复", ...} ]}

# 2) 回看某版本：返回不可变输入（title/events/relations）与当时推算结果
curl -s http://localhost:8000/api/reviews/1/versions/3
# {"version": 3, "state": "draft", "kind": "save", "title": "...",
#  "input": {"events": [...], "relations": [...]},
#  "result": {"status": "ok", "windows": {...}, "conflicts": [], "drafts": []}}

# 3) 把 v3 恢复为当前复盘的新草稿版本（必须带当前 base_version；旧版本不被覆盖）
curl -s -X POST http://localhost:8000/api/reviews/1/versions/3/restore \
  -H 'Content-Type: application/json' -d '{"base_version": 4}'
# 成功返回与 GET /api/reviews/1 相同的新快照（version=5, status="draft"）。
# base_version 过期 → 409 {"error": "...", "current_version": 5}
# 恢复冲突/暂存输入：允许成功，但该新草稿仍不可定稿（finalize 返回 400）。
# 恢复旧库无快照版本 → 422 {"error": "历史版本缺少完整输入快照，不可回看、不可恢复"}
```

页面下方「历史版本」面板提供同样的操作：每行可**回看**（弹窗展示当时输入与推算依据
链，可直接**恢复为新草稿版本**）；旧库无快照版本只显示不可回看、不可恢复的原因。
定稿产生的新版本会标注「已定稿」并沿用同一输入；由恢复产生的版本标注「恢复产生」并
记录来源版本。

### 关系间隔（最短/最长等待）HTTP 示例

`relations` 数组中每条关系可携带两个非负整数分钟字段；二者均可省略，
省略 `min_wait_minutes` 视为 0，省略或传 `null` 的 `max_wait_minutes` 表示不限。

```bash
# 保存一条带间隔的关系：告警结束后至少 10 分钟、至多 60 分钟内响应必须开始
curl -s -X PUT http://localhost:8000/api/reviews/1 \
  -H 'Content-Type: application/json' \
  -d '{
    "base_version": 1,
    "title": "10·04 事故复盘",
    "events": [
      {"key": "a", "name": "告警", "earliest_start": "2026-10-01T10:00",
       "latest_start": "2026-10-01T10:30", "duration_minutes": 15, "note": ""},
      {"key": "b", "name": "响应", "earliest_start": "2026-10-01T10:00",
       "latest_start": "2026-10-01T12:00", "duration_minutes": 30, "note": ""}
    ],
    "relations": [
      {"predecessor_key": "a", "successor_key": "b",
       "min_wait_minutes": 10, "max_wait_minutes": 60}
    ]
  }'
# 响应最早可行开始 = 10:00 + 15（告警持续）+ 10（最短等待）= 10:25；
# 其 earliest_chain / latest_chain 中会注明“（须等待 10～60 分钟）”。

# 只有最短等待（max_wait_minutes 省略/null = 不限）
#   {"predecessor_key":"a","successor_key":"b","min_wait_minutes":30}
# 只有最长等待（min 默认 0）
#   {"predecessor_key":"a","successor_key":"b","max_wait_minutes":120}
# 旧语义（均省略）：前项结束不晚于后项开始
#   {"predecessor_key":"a","successor_key":"b"}
```

非法输入（示例均返回 `400 {"error": "..."}`，且服务器记录保持不变）：

```bash
# 最长等待短于最短等待
# → 400 {"error": "「告警」先于「响应」的最长等待 10 分钟不得短于最短等待 50 分钟"}
# 负数 / 小数 / 非整数
# → 400 "...的最短等待必须是非负整数" 或 "...的最短等待超出合理范围（0～43200 分钟）"
```

成环冲突时，`result.conflicts` 中 `type="cycle"` 的条目会给出冲突事件及前向、
后向两条相反依据链：

```json
{
  "type": "cycle",
  "event_ids": [1, 2],
  "message": "“先于”关系构成循环：A → B → A，间隔与持续要求沿环累加后约束不可满足",
  "forward_chain": [
    {"text": "「A」先于「B」（至少等待 15 分钟）：B开始 − A开始 ≥ 45 分钟（持续 30 分钟、最短等待 15 分钟）"},
    {"text": "「B」先于「A」（至少等待 15 分钟）：A开始 − B开始 ≥ 45 分钟（持续 30 分钟、最短等待 15 分钟）"}
  ],
  "reverse_chain": [
    {"text": "沿后向回推关系「B」先于「A」（至少等待 15 分钟）：「B」最晚开始 ≤ 「A」最晚开始 − 45 分钟"},
    {"text": "沿后向回推关系「A」先于「B」（至少等待 15 分钟）：「A」最晚开始 ≤ 「B」最晚开始 − 45 分钟"}
  ]
}
```

版本回看的 `input.relations` 与恢复产生的新版本均逐条保留当时的
`min_wait_minutes` / `max_wait_minutes`；旧库升级后存量关系自动取 `0 / null`，
行为与升级前完全一致。

## 目录结构

```
app/
  server.py          HTTP 服务（标准库 http.server）
  db.py              SQLite 持久化、乐观锁、启动时建表
  compute.py         时间窗推算与依据链生成
  static/            浏览器单页界面（原生 HTML/JS/CSS）
tests/test_compute.py  推算引擎单元测试
Dockerfile / docker-compose.yml / .dockerignore
```
