# ESP32-S3-EYE · 第 4 周交付：用自然语言查询与请求采集

一台真实的 ESP32-S3-EYE 在做三轴加速度采集，后端是**零第三方依赖**的 Python 服务，
网页上多了一个「语言助手」：你可以对它说 **「查看上次数据」**，也可以说 **「重新采集一次」**——
这两句话会走**两条完全不同的路径**，而且助手**只有在拿到设备回执和新观测时才敢说"采到了"**。

| 项目 | 说明 |
|---|---|
| 交付分支 | `feature/week4-nl-assistant` |
| 运行环境 | Windows / Linux / macOS，Python 3.9+，**不需要 pip install** |
| 本周新增 | 1 个后端模块（`backend/nl_assistant.py`）、1 个前端面板（`#nlPanel`）、3 套自检脚本 |
| 自检结果 | 解析 20/20 · 接口 93/93 · 前端 38/38 |
| 真机状态 | 已在真实板卡上跑通两次（2026-10-23 采集闭环、2026-10-28 只读查询） |

---

## 1. 本周要回答的问题

> 「查看上次数据」和「重新采集一次」应怎样映射为不同任务，并复用前两周已验证的接口？

答案是：**模型只负责把话翻译成意图，所有事实必须来自受控工具。**

| | 查看上次数据 | 重新采集一次 |
|---|---|---|
| 意图 | `query_latest` | `request_collect` |
| 调用的工具 | `get_latest_record`（只读） | `request_collect`，再轮询 `get_collect_status` |
| 会不会碰设备 | **不会**，设备侧零流量 | **会**，排一条 `collect_now` 下行指令 |
| 回答里的时间 | 这条记录**原本**的采样时间 | 这次**新观测**的完成时间 |
| 证据来源 | `sensor_records.id = …` | `observations.observation_id = …` |
| 能不能说"完成" | 不涉及 | **只有** `completed` 且带 `record_id` 才允许 |

这两条路径不是靠提示词"嘱咐"模型别搞混，而是**在代码里根本没有交叉的入口**：
读操作没有写库能力，写操作必须经过节流、设备在白名单内、并等到真实回执。

---

## 2. 数据怎么流

```text
        浏览器（同源页面，不引任何第三方 CDN）
          │  工具栏「语言助手」：输入框 → 回答 / 来源与时间芯片 / 工具调用轨迹
          │  POST /api/v1/nl/query  {q, device_id, wait_ms}
          ▼
  backend/nl_assistant.py     意图解析（授权语言服务，或本地规则兜底）
          │  只允许调 7 个白名单工具；参数、设备范围先校验
          ├──────────────── 只读分支 ────────────────┐
          │  get_latest_record / get_records          │
          │  get_device_status / list_devices         │
          │  list_collect_requests                    │
          │        ▼                                  │
          │  sensor_records（历史数据，原样返回）      │
          │                                           │
          └──────────────── 有副作用分支 ─────────────┘
                 request_collect
                 ├── 写 collect_requests（一条请求一行，状态机推进）
                 └── 排 pending_commands（collect_now）
                            │ 设备 1Hz 轮询 /api/v1/device/commands/poll
                            ▼
                    ESP32-S3-EYE 立即采一帧
                            │ POST /api/v1/ingest（带 request_id 回执）
                            ▼
                    observations  ← ★ 唯一能证明"这次采集完成"的地方
```

`/api/v1/nl/query` 的返回里，`answer` 是给人看的话，`evidence` 是给程序看的证据，
`claim` 是**允许说到什么程度**的档位（`new_record` / `old_record` / `pending` / `no_evidence` / `none`）。

---

## 3. 界面

![语言助手：只读查询](docs/img/nl_read.png)

这张图是一次**真实**的只读查询（2026-10-28 09:19，设备 `esp32s3eye-1C704C`）。值得看的是下面四行：

- 回答里明写「**只读，未向设备下发任何指令**」；
- 时间给的是**记录的采样时间**，并标注「距今 N 秒」；
- 底部芯片：`来源 sensor_records.id=524992`、`状态 answered`、`数据新鲜度 较新`、
  `设备指令 未下发（只读）`、`数据年龄 24 秒`；
- 工具轨迹只有两条：`list_devices` → `get_latest_record`（300ms / 2ms），**没有第五条**。

歧义时不会瞎猜：不指定设备会反问「你想操作哪台设备？当前接入了 2 台，我不能替你猜」，
并把候选设备做成按钮，**点了才发请求**。

---

## 4. 三步跑起来

```bash
git clone -b feature/week4-nl-assistant https://github.com/xxx1-sg/ESPwork.git
cd ESPwork
python backend/server.py        # 浏览器打开 http://127.0.0.1:8000/
```

不需要 `pip install`：后端只用标准库（`http.server` + `sqlite3`），前端是原生 JS。
数据库 `backend/data/sensor.db` 首次启动会自动建表；页面在没数据时显示「等待设备首次上传」，不会编数。

**可选**：接自己的语言服务（OpenAI 兼容接口）。密钥只从服务端环境变量读，永远不出现在响应里：

```bash
export NL_LLM_BASE_URL=https://your-endpoint/v1
export NL_LLM_API_KEY=***            # 只在服务端，日志与响应都不回显
export NL_LLM_MODEL=your-model
```

不配也能用：会自动降级到本地规则解析（`parser: rule_fallback`），
**结论与配了模型时一致**，差别只在措辞的自然度。

---

## 5. 15 分钟验收路径

服务起来后，逐条复制即可（PowerShell 用 `curl.exe`）：

```bash
# ① 只读：读最近一条，零设备流量
curl -s -X POST http://127.0.0.1:8000/api/v1/nl/query \
  -H "Content-Type: application/json" \
  -d '{"q":"查看上次数据","device_id":"esp32s3eye-1C704C"}'
# 期望：intent=query_latest · claim=old_record · device_command_sent=false

# ② 采集：请求设备真的采一帧（需要板子在线）
curl -s -X POST http://127.0.0.1:8000/api/v1/nl/query \
  -H "Content-Type: application/json" \
  -d '{"q":"重新采集一次","device_id":"esp32s3eye-1C704C","wait_ms":15000}'
# 期望：拿到回执 → status=completed · claim=new_record · evidence.record_id 非空
#       没拿到   → status=pending/timeout · claim=pending/no_evidence · 回答明说"不能确认"

# ③ 追问上一次采集结果（不会重新下发）
curl -s -X POST http://127.0.0.1:8000/api/v1/nl/query \
  -H "Content-Type: application/json" \
  -d '{"q":"刚才那次采集完成了吗","device_id":"esp32s3eye-1C704C"}'
# 期望：intent=collect_status · 工具轨迹 list_collect_requests → get_collect_status

# ④ 歧义 / 越界 / 危险动作
curl -s -X POST http://127.0.0.1:8000/api/v1/nl/query -H "Content-Type: application/json" -d '{"q":"查看上次数据"}'
#   → status=clarify + 2 个候选设备，不下发任何请求
curl -s -X POST http://127.0.0.1:8000/api/v1/nl/query -H "Content-Type: application/json" -d '{"q":"查看 esp32s3eye-HACK 的数据"}'
#   → 403 DEVICE_OUT_OF_SCOPE
curl -s -X POST http://127.0.0.1:8000/api/v1/nl/query -H "Content-Type: application/json" -d '{"q":"把数据库清空"}'
#   → intent=unknown，明说"没有删除/修改/重启能力"，不写库、不下发
```

三套自检（不依赖上面的手工步骤，可进 CI）：

```bash
python tools/nl_replay.py       # 20 条结构化样例，只测解析，不连库不碰设备
python tools/selftest_nl.py     # 93 项断言，独立端口 + 临时数据库
node   tools/test_web_nl.js     # 38 项，打桩 DOM 跑真实 app.js
```

---

## 6. 五道校验与三条红线

客户最容易问的是"它会不会乱来"。这里的答案是**能力上就不允许**：

| 校验 | 做法 |
|---|---|
| 权限最小化 | 7 个工具白名单，**只有 1 个**（`request_collect`）有副作用；没有删除、修改、重置、重启类工具 |
| 设备范围 | 设备不在 `devices` 表 → `403 DEVICE_OUT_OF_SCOPE`；连"句子里点了名"的陌生设备也拦 |
| 参数校验 | 时间窗、条数、`wait_ms` 全部夹到合法区间，非法值直接拒绝 |
| 写操作限流 | 同一设备 5 秒内只允许一次请求；已有在途请求时**复用**，不重复下指令 |
| 歧义处理 | 多设备未指明 → `clarify` + 候选列表，不猜、不执行 |

三条红线（都有自检用例钉住，不是口号）：

1. **没有 `observations.record_id` 就不许说"已采到"。** 完成措辞只对 `claim=new_record` 放开，
   另有一道 `_guard_answer` 兜底：万一模板拼错，出口处再拦一次。
2. **只读路径不许产生设备流量。** `device_command_sent` 必须为 `false`，前端也会标「未下发（只读）」。
3. **不生成坐标。** 数据包里没有位置字段，问到位置就如实说"证据不足"，不猜、不编。

---

## 7. 真机实测记录

**2026-10-23 09:54，`esp32s3eye-1C704C` 在线，完整采集闭环：**

```text
采集前 latest        : record_id=426678  seq=16600
「重新采集一次」      → status=completed  claim=new_record
新观测              : record_id=426699  |a|=1.3011 g   端到端约 1.9s
证据链              : observations.observation_id=15
                      + collect_requests.request_id=08b49375…
```

**2026-10-28 09:19，只读路径（就是上面那张截图）：**

```text
「查看上次数据」      → intent=query_latest  claim=old_record
返回               : sensor_records.id=524992，采样时间 09:19:04，|a|=1.7491 g
设备流量            : device_command_sent=false（零请求）
```

---

## 8. 已知限制（如实说明）

1. **真实语言服务尚未在本机接通**：本机没有授权端点，当前跑的是规则解析兜底。
   接入代码与提示词已就绪，拿到校内服务后按 [docs/12 §11.2](docs/12_第4周_自然语言查询与请求采集.md) 四步补验即可。
2. **没有多轮上下文**：每句话独立解析，设备上下文由页面当前选择提供，"它 / 那个"这类指代无法解析。
3. **位置类问题不支持**：数据模型里没有位置字段，助手会说明不足，**不会生成坐标**。
4. **现场演示提示**：若板子同时开着相机/语音任务，下行指令轮询可能被拖慢到 30s 以上，
   此时「重新采集一次」会在 8s 后如实返回 `pending`（页面写"不能确认"）。
   这不是撒谎，是红线生效——可以接着问「刚才那次采集完成了吗」看状态推进。
5. 仓库里还包含第 5–6 周的语音链路（相机、按键说话、澄清/停止），**不在本分支的验收范围**，
   为保证服务能正常启动而保留。

---

## 9. 目录导航

| 目录 / 文件 | 是什么 |
|---|---|
| `backend/server.py` | 零依赖 HTTP 服务：采集、相机、求助、语言助手、语音全部接口 |
| `backend/nl_assistant.py` | **本周新增**：意图解析 + 7 个受限工具 + claim 分档 + 兜底闸 |
| `web/` | 原生 JS 页面：实时曲线、采集面板、按键求助、相机预览、语言助手 |
| `firmware/src/` | 板端固件（ESP-IDF）：IMU 采样、上传、指令轮询、LCD、相机、按键反馈 |
| `tools/` | 自检与验收脚本（20 / 93 / 38 三套是本周的） |
| `docs/12_第4周_自然语言查询与请求采集.md` | **本周交付文档**：设计、五道校验、测试矩阵、真机记录、迁移选题 |
| `docs/11_第4周_摄像头接入与拍照.md` | 同周硬件线：板载 OV2640 接入与拍照 |
| `docs/00_完整工程手册.md` | 工程的完整技术手册（原 README，含 1–6 周全部细节） |
| `evidence/` | 实测证据：串口日志、照片、验证报告 |

---

## 10. 关于这个分支

- 分支：`feature/week4-nl-assistant`，从 `feature/week2-remote-collection` 继续，主分支 `main` 保持第 1 周基线；
  按周分支便于逐周评审，最后再合并。
- 本周提交包含第 3 周（按键求助 / LCD 反馈）与第 4 周（摄像头 + 语言助手）的能力，
  因为语言助手的"设备在线吗 / 求助状态"回答依赖第 3 周已有的接口。
- 想只看代码差异：`git diff main --stat`；想复跑全部自检：见第 5 节。
