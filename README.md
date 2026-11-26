# 轮椅访客接驳车引路 API

用 FastAPI 实现的逐步引路服务：提交无障碍通道图、起点、出口接驳点和剩余分钟数后，服务会寻找一条**在至多一台电梯停运时仍保证准时到达**的路线；在所有满足保证的路线中，无停运时的用时最短。

## 启动

### Docker Compose

```bash
docker compose up --build
```

打开：

- Swagger UI：<http://localhost:8000/docs>
- ReDoc：<http://localhost:8000/redoc>
- 健康检查：<http://localhost:8000/health>

### 本地运行

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

运行测试：

```bash
pip install -r requirements-dev.txt
pytest -q

# 打印“最快路线来不及绕行”和“同梯分段搭乘”两个场景
python scripts/demo_scenarios.py
```

## 在 `/docs` 中提交图

调用 `POST /api/guidance/plan`。主要字段：

| 字段 | 说明 |
| --- | --- |
| `points` | 点位 ID 列表，可省略；服务会从路段和起终点汇总。去重后最多 40 个点位。 |
| `segments` | 路段列表。 |
| `start_point` | 起点。 |
| `destination_point` | 出口接驳点。 |
| `available_minutes` | 距离发车还剩的分钟数。 |

每个路段：

| 字段 | 说明 |
| --- | --- |
| `segment_id` | 可选路段编号；不填自动生成。 |
| `from_point` / `to_point` | 通行方向的起点和终点；JSON 输出中别名为 `from` / `to`。 |
| `direction` | `one_way`（仅 from→to）或 `two_way`（双向同用时）；也接受 `forward`、`both`、`单向`、`双向` 等常见写法。 |
| `minutes` | 正整数通行/乘梯分钟数。 |
| `elevator_id` | 普通通道留空；乘梯路段填写梯号，如 `"1"`。同一个梯号可出现在多个分段，连接多个楼层。 |

图中最多识别 8 个不同的非空 `elevator_id`。

示例请求见 [`examples/`](examples)。

## 引路操作

规划成功后会返回 `session_id` 和第一步。随后：

1. `POST /api/sessions/{session_id}/confirm`  
   确认走完当前展示的路段，扣除该段分钟数并移动到下一点。请求体可传 `{"edge_uid": "..."}` 防止旧页面重复提交。

2. `POST /api/sessions/{session_id}/outage`  
   仅当下一步是**尚未乘坐过**的电梯、且当前就在该梯口准备搭乘时调用。可以传 `{}`，也可传 `{"elevator_id": "1"}` 或当前 `edge_uid`。

3. `GET /api/sessions/{session_id}`  
   随时查看当前位置、下一段和剩余分钟数。

每一步的 `step` 都包含：

- `current_point`
- `next_segment`
- `remaining_minutes`
- `can_confirm_completion`
- `can_report_outage`
- 中文操作提示

命令行提交示例：

```bash
curl -sS http://localhost:8000/api/guidance/plan \
  -H 'Content-Type: application/json' \
  --data @examples/03-same-elevator-segmented.json
```

## 停运规则如何落地

- 规划状态为 `(当前点位, 已成功乘坐电梯集合)`。
- 第一次准备搭乘电梯 `E` 时，只有当：

  ```text
  已用分钟 + 从该梯口避开 E 的所有路段到接驳点的最短分钟 <= 可用分钟
  ```

  才允许选择该乘梯段。
- 同一台梯的后续分段会进入“已乘坐”集合；状态全程不变，因此不能再报告该梯停运，也不需要重复预留绕行。
- 报告停运时：
  - 访客留在当前 `from_point`；
  - 不扣除该电梯路段的 `minutes`；
  - 用当前剩余时间重新计算并切换为排除该 `elevator_id` 全部路段的最短路线。
- 一旦报告了一台梯停运，即使用完“一程至多一台梯停运”的条件，后续按其他电梯均可用处理。
- 如果不存在保证方案，接口返回 `guaranteed=false`、中文原因和 `session_id=null`，不会创建引路会话。

复杂度：点位 `V ≤ 40`、电梯 `K ≤ 8`，增强状态最多 `V·2^K = 10240` 个；每台梯预先做一次反向 Dijkstra 求到接驳点的避开距离。

## 已验证场景

自动化测试位于 `tests/test_guidance_api.py`，覆盖：

1. **最快路线来不及绕行**：5 分钟最快路线使用 1 号梯；梯口已用 2 分钟，但避开 1 号梯需 12 分钟。预算 8 分钟时返回 `NO_GUARANTEED_ROUTE` 且不创建会话；预算 10 分钟时改选 9 分钟无电梯安全路线。
2. **同梯分段搭乘**：1 号梯连续两个乘梯分段。第一次乘坐后，第二步的 `can_report_outage=false`；强行调用 outage 返回 `409 ELEVATOR_ALREADY_USED`，位置和剩余分钟不变；随后可继续第二个分段并到达。
3. **首次搭乘前停运**：报告后位置不变、乘梯分钟不扣，剩余 8 分钟全部用于 8 分钟绕行，最终准时到达。
4. 40 点位/8 电梯上限校验、未知会话 404、`/docs` 和 `/health` 可访问。

> 说明：会话保存在单进程内存中，Compose 默认只启动一个 Uvicorn 进程；服务重启后会话需要重新规划。
