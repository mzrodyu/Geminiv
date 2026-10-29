## 2026-10-29 流式收尾保护 + Gemini 4.x 工具调用加固（修 JSONDecodeError / 工具 Schema 400）

### 1. 流式收尾保护：不再让客户端拿到「断头流」
- 线上症状：`json.decoder.JSONDecodeError: Expecting value: line 1 column 1 (char 0) <- builtins.StopIteration: 0`。根因是上游（尤其 4-argon 长思考 + 工具调用）在**没有 `finish_reason`、也没有 `data: [DONE]`** 的情况下结束，客户端迭代结束时对残留空串 `json.loads("")`。
- `api_helpers._terminate_stream_safely()` 统一包住流式响应：
  - 上游正常结束但缺 `finish_reason` → 自动补一帧 `finish_reason=stop`；
  - 缺 `data: [DONE]` → 必补；
  - 整个流一个字节都没产出 → 返回可读的 502 诊断帧（提示重试 / 调低思考档位 / 调大重试次数），而不是让客户端自己崩；
  - 生成过程中抛任何未处理异常（`JSONDecodeError` / `StopIteration` / `KeyError` / `TypeError`…）→ 转成标准 OpenAI 错误帧 + `[DONE]`，不再泄漏 traceback。
- Studio（Cookie）通道分支修正：`execute_once` 返回 `JSONResponse`（Cookie 失效 / 项目不可用）时不再走 `.body_iterator` 抛 `AttributeError`，改为解析错误体并直接下发错误帧 + `[DONE]`；流式分支结束后同样由收尾保护兜底。

### 2. 工具调用：JSON Schema 净化（4.x 必踩的 400）
- 客户端（Pydantic / strict OpenAI schema）常带 `$schema`、`$defs`、`$ref`、`patternProperties`、`additionalProperties`、`prefixItems`、`const`、`if/then/else`、`oneOf/allOf`…
  Gemini 的 `function_declarations.parameters` **不接受**这些关键字，原样透传就会 400 INVALID_ARGUMENT。
- 新增 `schema_validation.sanitize_tool_schema()`：本地 `$ref` 就地展开、`allOf` 合并、`oneOf/anyOf` 收敛（`{"type":"null"}` 分支转 `nullable`）、`const → enum`、`type: ["string","null"] → type+nullable`、对象缺 `type` 自动补 `object`，其余不支持的关键字全部安全丢弃。
- `create_generation_config()` 改为净化后再下发（`debug_outbound` 开启时会打印净化数量）；`tool_choice="none"` 时不再下发 tools。

### 3. 其它健壮性
- 工具流式分片：空/缺失 `arguments` 归一为 `"{}"`，缺失 `id` 自动补 `call_N`，不可序列化的 proto 参数退化为 `{}`，避免整条流 500。
- 新增 `tests/smoke_stream_and_tools.py`（34 项断言）：断头流补帧、异常转错误帧、空流诊断、Cookie 通道 JSON 响应、Schema 净化、空参工具回放。

---

## 2026-10-29 前端一键配置：首次启动向导 + 系统设置页（彻底不碰环境变量）

### 1. 首次启动 3 步向导（app/main.py · DASHBOARD_HTML）
- 控制台新增 `#wizard` 弹窗向导，`setup_completed` 未标记时自动弹出（每个浏览器只自动弹一次，可「稍后再说」，也可在系统设置页随时重开）：
  ① **访问口令**：可「⚡ 一键随机口令并保存」，也可手填两遍；默认口令 123456 未改时红字提醒。
  ② **通道与凭证**：选主通道 + 多行粘贴 Express Key（自动分行保存并后台探测 Project ID）+ Cookie / Project ID。
  ③ **完成**：展示 OpenAI 兼容地址 `origin + /v1`。
- 登录页新增 `.firstrun` 提示条：首次启动未完成、或口令仍为默认 123456 时，直接在登录卡片里说明「登录后会自动弹向导 / 去系统设置改口令」。

### 2. 新增「系统设置」标签页（第 5 个 tab）
- **控制台访问口令（API_KEY）**：手动改 / ⚡ 一键生成并保存 / 先预览再保存；保存后旧会话全部失效、当前页面自动续期，新口令只显示一次并可一键复制。
- **上游代理（PROXY_URL）**：支持 `http(s)://`、`socks5(h)://`、`socks4(a)://`；「测试连通性」用该代理请求 `https://www.google.com/generate_204` 并回传状态码与耗时；清空即直连。
- **模型列表来源（MODELS_CONFIG_URL）**：改成自建 JSON 地址后「保存并刷新」立即重建缓存并回显模型数量；留空恢复官方默认。
- **状态持久化与备份**：显示 `STATE_DIR` / `web_state.json` 实际路径与大小、当前主通道与凭证就绪状态，支持「下载配置备份」与「重开向导」。

### 3. 后端：运行态可写的系统级配置（app/runtime_state.py、app/auth.py）
- `AppState` 新增 `runtime` 覆盖层（`api_key` / `proxy_url` / `models_config_url`）与 `setup_completed` 标记，落盘 `web_state.json`（权限 0600），启动时 `apply_runtime_config()` 写回 `config` 模块。
- 优先级：**控制台保存值 > 环境变量 > 代码默认值**；`PROXY_URL` / `MODELS_CONFIG_URL` 在各调用点都是属性读取，改完下一次请求立即生效。
- `auth.py` 改为动态读取 `config.API_KEY`（原来是 `from config import API_KEY` 的导入期快照），并改用 `secrets.compare_digest`，因此前端改口令后 `/v1` 鉴权即时切换。
- 新增接口：`GET /api/system`、`POST /api/system/api-key`、`POST /api/system/proxy`（+ `/proxy/test`）、`POST /api/system/models-source`、`POST /api/system/setup-complete`、`GET /api/system/state-backup`。

### 4. 部署与测试
- `docker-compose.yml`：除 `STATE_DIR` 外全部环境变量改为注释示例 —— 零配置起容器，登录后在控制台配完即可用。
- 新增 `tests/smoke_console_system.py`：覆盖登录页提示、改口令（旧会话失效 / 旧口令拒登）、代理校验与保存、模型源切换、状态备份、`setup_completed`、以及 `/v1` 动态 Bearer 鉴权，共 29 项断言。
- 页面细节：向导主通道改用 `.wiz-chan`（避免继承 `.chan-card` 的 0.78 透明度过淡）、弹窗卡片改为不透明 `var(--inp-bg)`、去掉整屏 `backdrop-filter`（部分浏览器截图/合成下会把整页糊掉）。

---

## 2026-10-23 多账号轮询容灾、Key ↔ Cookie 跨通道互备与 500 智能重试体系

### 1. 多 Express Key 轮换与 Project ID 动态感知
- 单次请求重试多 Key 轮替：当请求遇到 429 限流、500 内部错误或网络闪断时，退避重试自动按轮换顺序切换至下一个健康 Key，绝不死磕同一个账号。
- 项目绑定隔离：重试切换 Key 时，动态拼装各 Key 专属的 `projects/{key_pid}/locations/...` 资源路径，防止跨账号 Project ID 污染引发 403 权限拒绝。
- 坏凭证自动隔离与日志红色警示：遇 `401 API_KEY_INVALID` 或项目欠费（`requires billing`）时，自动在该请求周期内隔离坏 Key 并跳过，同时在运行日志打印红色高亮警示（精准定位 Key 序号 `#1/#2` 与项目 ID），提醒管理员及时删除或更换。

### 2. 跨通道主备兜底重试（Key ↔ Cookie 互备）
- 控制台「重试与退避」新增「允许跨通道兜底重试」开关：
  - 主通道为 Express 时，所有 Key 试完一轮后自动无缝切至 Cookie 通道兜底；
  - 主通道为 Cookie 时，遇到限流或 Cookie 会话过期，自动无缝切至 Express Key 列表接力完成回复；
  - 控制台「通道与凭证」页实现双凭证卡片常驻显示，无须切换主通道即可随时查看并维护备用 Cookie。

### 3. 精准 500 错误分类与退避重试优化
- 彻底区分「明文安全拦截（PROHIBITED_CONTENT / SAFETY）」与「服务繁忙 500 / 网络断连」：
  - 提示词拦截、静默空候选或长思考跑飞截断直接归入 400/502 并明确诊断提示，坚决不重试；
  - Google 上游真实的 `500 INTERNAL`、`502`、`503`、`504` 与本地网络 TransportError（`RemoteProtocolError` / `ConnectError`）严格按用户设置的最大重试次数执行退避重试。
- 修复非流式报错在指标监控中被双倍累加计数的 Bug。

### 4. 彻底清理已废弃代码与 Gemini 2.5 残留
- 彻底移除已于 2026-10-16 停用的 Gemini 2.5 系列模型及 `thinking_budget` 分支逻辑；
- 移除未使用的 `StreamingReasoningProcessor`、`extract_reasoning_by_tags`、旧单 Key 获取函数等近 180 行遗留死代码。

---

# vertex2openai 变更与更新日志（CHANGES.md）

基于原版 vertex2openai-main（2026-09-01），持续演进优化。

---

## 2026-10-18 架构级全面升级与十大核心问题修复

### 1. 弱网续传与前端手动取消止损（app/resumable.py、app/routes/chat_api.py）
- 智能断连感知（DetachedRequest）：区分「首包产出前的等待/退避重试期」与「已产出模型正文的生成期」。
  - 当上游尚未产出任何有效内容（处于首包等待或 429/503 退避重试）且前端手动点击停止/取消时，立即返回断连信号并经 1.5 秒防抖取消后台泵任务，彻底解决「前端手动取消后后台仍死磕重试直到次数耗尽」的问题。
  - 当上游已开始产出有效内容时，前端短暂断网不中断后台接收，完整保留弱网续传能力。
- 修复 Last-Event-ID 续传起点 Off-by-One Bug：按 W3C SSE 规范对 Last-Event-ID: k 从 k + 1 开始续传，消除断线重连后重复吐出最后一个 Token 的问题。
- 错误缓冲自动销毁：新增 is_real_output_frame 与 is_error_frame 校验，过滤仅含角色声明的空壳首帧；当流或非流式请求以报错结束且无有效模型输出时，自动从 ResumableStore 移除，防止 30 分钟内重试一直命中死报错缓存。
- 心跳帧隔离：pump_stream 自动过滤 : keep-alive 注释帧与 chatcmpl-keepalive 假流式保活帧，保持事件序号纯净；_evict_locked 与 sweep 淘汰缓冲时同步取消关联后台任务，杜绝孤儿协程泄漏。

### 2. 长思考模型（gemini-4-argon 等）防超时断连与双通道假流式保活（app/http_options.py、app/resumable.py、app/upstreams/cookie_proxy.py）
- SDK 超时扩容：在 app/http_options.py 中显式配置 Google GenAI SDK 的 HttpOptions(timeout=600_000)（10 分钟）与 httpx.Timeout(read=600.0)，解决 gemini-4-argon 在假流式（底层调用非流式 generate_content）长思考超过 60 秒被默认超时掐断的 Bug。
- 续传缓冲等待期主动心跳：sse_from_buffer 在等待慢思考模型输出期间，按配置自动向当前连接发送不占序号的保活帧（开启假流式时发送 chatcmpl-keepalive 数据帧，未开启时每 3 秒发送 : keep-alive 注释帧）。
- Cookie 通道假流式与剥离思考保活：
  - Cookie 通道新增文本模型假流式保活模式（studio_fake_stream），在后台汇总 batchGraphql 响应期间持续向前端发送心跳，并支持随时断连取消；
  - 真流式在开启「关闭原生思考 / 压制思考」（strip_thoughts=True）剥离漫长思考块期间，每 2 秒自动补发 SSE 心跳，防止前端因长思考静默期超时断连。

### 3. 错误诊断体系重构与 Cookie 失效 / 项目计费精准分流（app/upstreams/cookie_proxy.py、app/api_helpers.py）
- 修复 Cookie 失效被误报为项目计费问题的 Bug：
  - 移除旧版 _is_project_error 中因模型路径固定含 projects/xxx 而恒真的判断条件及过宽的 invalid argument 关键词；
  - 精准区分「GCP 项目未开计费（requires billing / service_disabled）」、「Cookie 会话过期（401 / unauthenticated）」与「aiplatform.endpoints.predict denied（明确提示 90% 以上为 Cookie 登录态失效，优先重取 Cookie）」。
- 完整错误详情提取（不再截断或丢弃 details）：
  - 修复 extract_upstream_error 正则遇到内嵌单引号提前截断的 Bug，新增对 APIError.details / fieldViolations 及 GraphQL extensions 的深层提取；
  - 将日志与响应错误长度上限放宽至 2000 字符，并将 traceback 接入控制台日志流；
  - 修复 Cookie 通道在假流式与非流式下遇到 GraphQL errors 时将错误信息拼入正文并误记为成功响应的 Bug。

### 4. 预填充兼容引擎升级与去重强化（app/message_processing.py、app/api_helpers.py、app/upstreams/cookie_proxy.py）
- 思维链守卫（cot_guard）位置优化：在 smart 模式下将守卫指令前置融合至续写引导语中（置于 prefill 之前），确保整条消息严格以用户的 prefill 原文断点收尾，消除因末尾变成规则说明而诱发模型重开标签的问题。
- 多模态 user 消息合并：支持最后一条 user 消息为分段数组（list）时的无损合并。
- 去重引擎升级（strip_prefill_overlap & PrefillDeduper）：
  - 支持短于 8 字符的短标签（如 <cot>、<think>）去重与前导空白/换行容错；
  - 自动识别并剥离模型在输出开头重复开启的未闭合标签，彻底解决 keep_turn 与 smart 模式下的双开标签问题；
  - 修复 PrefillDeduper 在首块仅收到换行符时提前退出的 Bug，并确保真流式下 prefill_text 严格排在原生 reasoning_content 思考块之后发出。

### 5. 流式工具调用参数分片聚合（streamFunctionCallArguments 加固）（app/message_processing.py、app/api_helpers.py、app/upstreams/cookie_proxy.py）
- 新增 FunctionCallStreamAssembler：完整支持 Gemini 3 / Gemini 4 的 partialArgs（RFC 9535 JSONPath）与 willContinue 分片参数流式拼接，完整保留首块 id 与 thought_signature，过滤流末尾空收尾标记块，防止分片函数调用丢失参数或分裂成多个空调用。

### 6. 生图模型搜索后缀与 Nano Banana 2.1 修复（app/routes/models_api.py、app/upstreams/cookie_proxy.py）
- 修复 Cookie 模式下调用 gemini-nano-banana-2.1 报 mediaResolution 枚举值非法的问题（修正为 MEDIA_RESOLUTION_HIGH）。
- 放开所有支持联网搜索的生图模型（gemini-3.1-flash-image、gemini-3-pro-image、gemini-nano-banana-2.1）在 /v1/models 中的 -search 别名生成。

### 7. 控制台交互修复、参数分区重构与马卡龙日夜双模视觉升级（app/main.py、app/config.py、app/express_key_probe.py）
- Express Key 即添即探：热添加新 Key 后直接点击「探测」或「探测全部项目」时，自动同步保存当前 Key 列表并探测归属项目，解决未先点保存就点探测导致新 Key 消失的逻辑错误。
- 模型参数分区严格隔离：将预填充兼容模式（prefill_mode）、预填充压制思考（prefill_suppress_thinking）、思维链守卫（prefill_cot_guard）统一归入「① 按模型参数」区并纳入 PER_MODEL_KEYS，与「② 全局基础设施设置」彻底分离、互不覆盖；修复 refreshModelSelStars 将「＊ 全局默认」覆盖为 __global__ 的显示 Bug。
- 马卡龙配色与柔和夜间护眼模式：
  - 全局升级为柔和马卡龙配色，并新增「🌙 夜间护眼 / ☀️ 日间马卡龙」一键切换（自动记忆、首屏防闪白）；
  - 修复服务健康度轮盘将「成功响应」显示为纯黑色的 Bug，新增实时成功率百分比徽章与动态比例 Token 进度条；
  - 运行日志支持点击任意单条日志直接复制该条内容。

---

## 历史迭代记录（2026-09 ~ 2026-10）

- 2026-10-04（签名轮换修复）：更新 app/cookie_auth.py 中的 STREAM_GENERATE_QUERY_SIGNATURE，修复 QUERY_SIGNATURE_NOT_FOUND 空回复问题。
- 2026-10-02（审计修复）：_resumable_stream 增加 CancelledError 分支清理占位缓冲；修复 resumable_poll 越界游标误标交付问题。
- 2026-09-30（并发竞态补丁）：ResumableStore.create_if_absent 持锁原子占位；轮询读完自动 mark_delivered。
- 第一轮 ~ 第三轮重做：Express Key 多账号管理与 Project ID 自动探测、弱网可靠交付缓冲、惩罚参数默认剥离。