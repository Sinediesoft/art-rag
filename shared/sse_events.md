# SSE 事件格式（共用層 §一，負責：B）

`POST /api/v1/chat` 回傳 `text/event-stream`，只有四種事件，依序出現：

| event | data（JSON） | 說明 |
|---|---|---|
| `sources` | `{"request_id", "artwork_id", "strategy", "sources": [{"ref", "chunk_id", "artwork_id", "artwork_title", "text", "source_url", "license", "score"}]}` | 檢索到的來源，`ref` 即回答中的 [編號] |
| `token` | `{"text"}` | 文字片段（已經過 OpenCC s2twp 轉換） |
| `done` | `{"request_id", "strategy_requested", "strategy_used", "model", "fallback", "fallback_reason", "prompt_version", "use_retrieval", "image_sent", "latency_ms": {"retrieval", "first_token", "generation", "total"}, "tokens": {"input", "output"}, "cost_twd", "egress": {"images", "chunks", "bytes"}}` | 完成；`image_sent`＝這題有沒有附圖給生成模型（docs/adr/024：已辨識、檢索開著時可設定不送）；`fallback` 為 true 時前端顯示「本地備援模型」（`strategy_used` 為 `hybrid_fallback`）；`egress` 是送出本機的資料量，本地策略恆為 0，前端顯示「資料外送」標示 |
| `error` | `{"code", "message", "request_id"}` | 錯誤，之後不再有其他事件 |

`sources` 另帶 `part_id`、`identified`（有做以圖辨識時的辨識結果）與 `route`：只帶 `image_id`、
沒指定 `artwork_id`／`part_id` 時，後端先用領域路由判斷是畫作還是工廠圖紙（`{"domain", "margin", "art_score",
"mfg_score", "min_margin", "uncertain"}`，格式同 `/search/any` 的 `route`），判為圖紙就走下面的圖紙問答；沒經過路由時為 `null`。

`sources` 另帶 `rearrange`：開啟檢索段落篩選（MIRA 的 Rearrange，見 docs/adr/008；請求的 `rearrange`、
`.env` 的 `REARRANGE` 或 `models.yaml` 的 `rearrange.enabled`，2026-10-03 起預設開）時為
`{"candidates", "kept", "ms", "fallback"}`——候選幾段、模型留下幾段、篩選花幾毫秒、失敗原因（成功為 `null`，
失敗時 `sources` 是原本的全部段落）；沒開篩選（或關檢索）時為 `null`。候選只有 0～1 段時不呼叫模型，
`candidates` ≤ 1、`ms` 為 0，不算篩選過。篩選只問本地模型；問答策略是 `mock` 時篩選也用 mock。
`sources` 永遠只列真正放進 prompt 的段落，`ref` 從 1 重新編號。`done.latency_ms.retrieval` 包含篩選時間。

`sources` 另帶 `conflict_check`（docs/adr/028）：開啟參考資料矛盾檢查（請求的 `conflict_check`、`.env` 的
`CONFLICT_CHECK` 或 `models.yaml` 的 `conflict_check.enabled`）而且要放進 prompt 的段落有 2 段以上時為
`{"conflict", "refs", "ms", "fallback"}`——有沒有矛盾、互相矛盾的段落 `ref`、檢查花幾毫秒、失敗原因
（成功為 `null`；失敗時當成沒檢查）；沒開、不到 2 段、策略是 `mock` 時為 `null`。`conflict` 為 `true` 時，
送給模型的參考資料後面多一句「系統比對：參考資料 [1]、[2] 對這個問題的說法不一致…」，回答會明白指出說法不一致。
檢查只問本地模型、只送文字；時間也算在 `done.latency_ms.retrieval` 裡。

`sources` 另帶七段權限控管（docs/adr/015）的第 3～6 段：
- `filter`：Metadata Filter `{"domain", "domain_label", "clearance", "depts", "levels", "doc_id", "doc_label", "doc_level", "text"}`，
  只照 JWT 的 `clearance` 與 `depts` 產生（`text` 例：`domain = "工廠圖紙" AND clearance <= 1 AND dept IN ("公開", …) AND doc_id = "mfg-002"`）；
  看不到的文件 `doc_level` 為 `null`（不透露它的等級）
- `candidates`：第 3 段檢索出的候選段落數（第 4 段驗證前）
- `post_filter`：`{"mode", "engine", "candidates", "kept", "flagged", "dropped", "cloud", "local", "verify", "rerank", "gate",
  "rearrange", "egress_bytes", "ms"}`。`mode` 為 `jev`／`local`：第 4～6 段由誰判斷，**由伺服器決定**（交接票記的判斷者，
  沒有交接票時是 `jev`，叫不到 Jev 就地端判斷）。2026-10-06 起（docs/adr/030）**每一次問答都完整跑第 4～6 段**，
  沒有只掃描的 `scan` 模式，請求的 `post_filter` 欄位已移除（送了也會被忽略）。
  `flagged` 是第 4 段 security_leak_check 剔除的段落（`by`＝Jev／地端），`dropped` 是與提問無關、分數太低或超過 3 段的段落。
  `verify`（第 4 段 Jev Noul）、`rerank`（第 5 段 Jev Score）、`gate`（第 6 段生成閘門）都是
  `{"engine", "checks", "call", "fallback_reason", "ms"}`，`gate` 另有 `passed`、`message`；`call` 是那一次 Jev 請求的紀錄
  （送出的代號化內容、代號對照、回答、請求本文）。`cloud` 是送 Jev 的段落數（只有公開段落）。
  關檢索、或第 1、2 段就擋下時為 `null`。
- `pipeline`（`sources` 與 `done` 都有，docs/adr/030）：可觀測軌跡 `[{"stage", "name", "status", "by", "detail"}]`，
  依序列出執行過的段落；`status` 為 `pass`／`block`／`skip`。`sources` 只到第 6 段，`done` 含第 7 段。
  某一段 `block` 之後的段落不會執行（不讀圖、不檢索、不組 prompt、不呼叫 Jev 或生成端），也不會出現在清單裡。
每段多一個 `level`（畫作「公開」，圖紙「內部」或「機密」）。`done.egress` 多 `jev_bytes`（第 4～6 段送 Jev 的位元組合計），
`bytes`／`chunks` 也算進去；本地策略只有這一項外送。

**降級回應**（`done.degraded=true`，只送 `sources`（空）→ 一個 `token` → `done`，`tokens` 為 0、`latency_ms.first_token` 為 `null`）：
- 第 1 段：指定的圖紙看不到**或不存在**（兩者回應完全一樣，不透露有沒有這份文件），訊息「查無資料：…」，`model` 為「第 1 段（未呼叫 LLM）」
- 第 2 段：沒有有效交接票時 `/chat` 自己跑的 Jev Choice／地端硬性規則擋下，訊息為 `agent.yaml` 的 `guard.blocked_message`
- 第 6 段：生成閘門沒過（`post_filter.gate.passed=false`），或關閉檢索（評估模式的畫作對照組以外），`model` 為「生成閘門（未呼叫 LLM）」
- 第 7 段：生成後的輸出檢查沒過（含帳密／金鑰、個資、看不到的文件名稱或編號、內部資料數字、被剔除段落的內容、引用沒提供的編號），
  原輸出不送出，訊息為 `guard.output_blocked_message`，`model` 註明「第 7 段輸出檢查擋下」
其他情況 `done.degraded=false`。生成的回覆先在伺服器收齊、通過輸出檢查才送出 `token`，所以第一個 `token` 會在生成完成後才到。

請求另可帶 `route_ticket`（`/agent/route` 回的交接票）：有效（同帳號、同一句話、同一個對象、10 分鐘內）才沿用第 2 段的判斷；
沒帶、過期或不符時 `/chat` 自己重跑第 2 段。角色不能用工廠圖紙（訪客帶 `part_id`）在串流開始前回 403 `DATA_SCOPE_DENIED`。

`part_id`（工廠圖紙問答）走同一組事件：`sources` 的每段改帶 `part_id`、`title`、`source_label`
（內部文件名稱），`source_url` 為 `null`；`done.prompt_version` 為 `drawing_v1`。圖紙屬機密，
雲端策略一律回 `error`（`CLOUD_CONFIDENTIAL_FORBIDDEN`）。

**干擾段落注入**（評估專用，請求帶 `inject`、後端 `EVAL_INJECTION=true`，docs/adr/019）：被注入、而且通過洩密掃描與
段落篩選留下來的段落，在 `sources` 裡多帶 `injected: true`、`injected_kind`（`counterfactual`／`other`），
`chunk_id` 以 `inject:` 開頭，`source_label` 為「評估注入（干擾段落）」。前端不會送 `inject`，正常問答不會出現這些欄位。

畫作的「色彩分析」段落（`chunk_id` 為 `<id>#color`，建索引時由系統計算，見 `docs/adr/010`）同樣 `source_url` 為 `null`，
改帶 `source_label`（「系統計算：色彩分析（數位圖檔）」）；其他畫作段落不帶 `source_label`。

## `POST /api/v1/cad/reconstruct`（工廠圖紙 → 3D）

| event | data（JSON） | 說明 |
|---|---|---|
| `meta` | `{"request_id", "job_id", "strategy", "model", "image_id", "part", "identified", "layout", "input_url", "input_size", "scale_to", "scale_source"}` | 前處理完成：`layout` 為 `kb`（知識庫圖紙）／`rectified`（照片已拉正）／`upload`（未收錄）；`scale_to` 是等比縮放依據的外形尺寸（mm），未收錄圖紙由本地 Qwen3-VL 讀圖上標註，讀不到為 `null` |
| `token` | `{"text"}` | CadQuery 程式碼片段（不做 OpenCC 轉換） |
| `executing` | `{"generation_ms", "code_chars"}` | 程式碼產生完畢，開始沙箱執行 |
| `result` | `{"ok", "error", "code", "valid", "raw_dims", "dims", "scale", "volume", "faces", "iou", "iou_bbox", "files"}` | 執行結果；`iou`（論文評估法）與 `iou_bbox`（外框對齊）只有知識庫圖紙才有；`files` 為 STL／STEP／回投影三視圖／程式碼的網址 |
| `done` | `{"request_id", "job_id", "strategy", "model", "latency_ms": {"first_token", "generation", "exec", "total"}, "tokens", "egress"}` | 完成；`egress` 恆為 0（只連本機推論伺服器） |
| `error` | `{"code", "message", "request_id"}` | 錯誤，之後不再有其他事件 |

`strategy`：`ortho2cad`（主模型）或 `hybrid`（未微調的 Qwen3-VL，對照組）。沒有任何備援或雲端路徑。

## `POST /api/v1/inventory/ask`（工廠庫存 Text-to-SQL）

| event | data（JSON） | 說明 |
|---|---|---|
| `meta` | `{"request_id", "strategy", "prompt_version", "as_of"}` | 開始；`as_of` 是資料日期（模型把它當「今天」） |
| `attempt` | `{"n", "previous_error"}` | 第 n 次產生 SQL；`n > 1` 代表上一條失敗、已把錯誤訊息回饋給模型修正 |
| `sql_token` | `{"text"}` | 模型輸出的 SQL 片段（可能含 ```` ```sql ```` 標記，前端顯示時去掉；不做 OpenCC 轉換） |
| `sql` | `{"attempt", "sql", "ok", "error"}` | 這次嘗試的 SQL 與檢查／執行結果；`ok` 為 false 時接著出現下一個 `attempt` 或 `error` |
| `result` | `{"columns", "rows", "row_count", "truncated", "exec_ms"}` | 唯讀執行結果；最多 200 列，超過時 `truncated` 為 true |
| `token` | `{"text"}` | 依查詢結果產生的回答（已經過 OpenCC）；0 筆時固定為「查無符合條件的資料。」 |
| `done` | `{"request_id", "strategy_requested", "strategy_used", "model", "fallback", "fallback_reason", "prompt_version", "answer_prompt_version", "attempts", "latency_ms": {"first_token", "sql", "exec", "answer", "total"}, "tokens", "egress"}` | 完成；`egress` 恆為 0 |
| `error` | `{"code", "message", "request_id"}` | `SQL_REJECTED`（要求修改資料，直接拒絕）、`SQL_FAILED`（修正後仍失敗）、`STRATEGY_UNAVAILABLE`、`CLOUD_CONFIDENTIAL_FORBIDDEN`、`INVENTORY_UNAVAILABLE`；之後不再有其他事件 |

`strategy` 同問答（預設 `hybrid`，備援鏈相同）；雲端策略一律回 `CLOUD_CONFIDENTIAL_FORBIDDEN`。

## `POST /api/v1/schedule/solve`（生產排程，Timefold Solver）

| event | data（JSON） | 說明 |
|---|---|---|
| `meta` | `{"request_id", "engine", "engine_label", "engine_version", "fallback_reason", "seconds", "unimproved_seconds", "problem", "work_orders", "machines", "axis"}` | 開始；`engine` 為 `timefold` 或 `greedy`（簡易排程）。Timefold 排程服務連不上時自動改用 `greedy`，`fallback_reason` 說明原因；`problem` 是工單、工序、釘選數與排程起點 |
| `progress` | `{"elapsed_ms", "phase", "score", "hard", "medium", "soft", "structural", "feasible", "initial_score", "improvements", "operations", "kpis"}` | 找到更好的解（最多每 0.45 秒一次）：`operations` 是目前最佳解的每道工序（機台、起訖的工作分鐘與實際時間、換線、是否釘選；委外工序 `machine_id` 為 `null`），前端即時重畫甘特圖；`phase` 為「建構初始解」「局部搜尋最佳化」或「交期優先派工」 |
| `tick` | `{"elapsed_ms", "phase", "improvements"}` | 沒有更好的解時每秒一次的心跳（前端計時用） |
| `solution` | `{"run_id", "engine", "status", "score", "hard", "medium", "soft", "initial_score", "score_check", "analysis", "operations", "work_orders", "kpis", "axis"}` | 最終結果，已寫入 `production.sqlite3` 並同步到工廠資料庫（`schedule_ops`、`v_wo_plan`）；`status` 為 `done` 或 `stopped`（提前結束）；`analysis` 是各限制條件的分數與次數（後端依同一套規則計算），`score_check` 表示總分與 Timefold 一致 |
| `done` | `{"request_id", "run_id", "engine", "status", "score", "initial_score", "improvements", "latency_ms": {"build", "solve", "total"}, "egress", "memory"}` | 完成；`egress` 恆為 0；`memory` 是這次排程期間記憶體管理的釋放紀錄（沒有釋放為 `null`） |
| `error` | `{"code", "message", "request_id"}` | `NO_WORK_ORDERS`、`SCHEDULE_BUSY`、`SCHEDULER_UNAVAILABLE`、`SCHEDULE_FAILED`、`SCHEDULE_DATA_INVALID`；之後不再有其他事件 |

分數格式 `{hard}hard/{medium}medium/{soft}soft`（Timefold 的 HardMediumSoftScore），越接近 0 越好：
硬＝機型不符／排程循環，中＝交期延遲（工作分鐘 × 急件權重），軟＝換線準備＋各工單完工時間。
時間欄位：`*_min` 是工作分鐘（排程起點起算、只計上班時間），`*_at` 是台灣時間 `YYYY-MM-DD HH:MM`。
使用者離開頁面（串流中斷）時後端會停止 Timefold 的求解；`POST /api/v1/schedule/stop` 提前結束並採用目前最佳解。

## `POST /api/v1/intake`（照片建檔，docs/adr/013）

請求 `{"image_id", "domain"}`：`domain` 是從哪一邊進來（`mfg` 工廠圖紙、`art` 畫作），null 時交給領域路由判斷。

| event | data（JSON） | 說明 |
|---|---|---|
| `stage` | `{"stage", "label"}` | 進到哪一步。圖紙：`sharpness`（清晰度）→ `identify`（知識庫有沒有）→ `page`（拉正、對齊版面）→ `read`（Qwen3-VL 讀標題欄）→ `validate`（驗證）；畫作不讀照片：`sharpness` → `identify` → `validate` |
| `token` | `{"text"}` | 模型輸出的 JSON 片段（不做 OpenCC 轉換）；只有圖紙，`LLM_MODE=mock` 或模型無法使用時沒有 |
| `draft` | `IntakeDraft`（同 `GET /api/v1/intake/{draft_id}`） | 草稿：每個欄位的值、來源（Qwen3-VL／規則／人）、驗證結果、能不能收錄；畫作的欄位全由人在跳出的表單填 |
| `done` | `{"request_id", "draft_id", "latency_ms": {"read", "total"}, "egress"}` | 完成；`egress` 恆為 0 |
| `error` | `{"code", "message", "request_id"}` | `INTAKE_TOO_BLURRY`（另帶 `blur`）、`INTAKE_ALREADY_IN_KB`（另帶 `part` 或 `artwork`）、`INTAKE_WRONG_DOMAIN`（另帶 `route`）、`INTAKE_PAGE_NOT_FOUND`、`INTAKE_DOMAIN_UNSUPPORTED`、`IMAGE_NOT_FOUND`；之後不再有其他事件 |

圖紙的模型無法使用（主推論伺服器與本地備援都失敗）不算錯誤：照樣給 `draft`，欄位全空、`extraction.error` 說明原因，由人對照照片填。
收錄（`POST /api/v1/intake/{draft_id}/commit`，只有主管）不是 SSE：回傳時 `status` 為 `indexing`，背景重建索引完成後 `GET` 會變成 `done`（失敗為 `failed`，寫進去的檔案與版本已還原）。

## `POST /api/v1/batch/identify`（批次辨識，docs/adr/017）

請求 `{"image_ids": [...], "domain"}`：`domain` 為 `art`／`mfg` 時只跑該領域的辨識，null 時每張先交給領域路由。一批上限 `batch.max_images`（100）。

| event | data（JSON） | 說明 |
|---|---|---|
| `row` | `{"index", "image_id", "domain", "route", "blur", "status", "item", "closest", "score", "inliers", "overlap", "note", "latency_ms"}` | 每張照片一列，依序送出。`status`：`matched`（認得，`item` 是那幅畫或那張圖紙 `{kind, id, label, detail, url, level}`）、`not_in_kb`（`closest` 是最相近但沒通過驗證的那一件）、`blurry`（模糊程度高於 `intake.max_blur`，不辨識）、`hidden`（目前身分看不到：不能用工廠圖紙，或辨識出的是看不到的圖紙，不回 id）、`error`（例如照片過期） |
| `done` | `{"request_id", "total", "counts": {status: 張數}, "domains": {"art", "mfg"}, "latency_ms": {"total", "per_image"}, "egress"}` | 完成；`egress` 恆為 0 |

張數超過上限回 422；指定 `domain: mfg` 但身分不能用工廠圖紙回 403（串流開始之前）。

## `POST /api/v1/compare/summary`（兩件並排比較的差異摘要，docs/adr/017）

請求 `{"a", "b"}`：`artwork:<id>` 或 `part:<id>`，兩件要同一類。權限、找不到、類別不同在串流開始前就回 4xx。

| event | data（JSON） | 說明 |
|---|---|---|
| `sources` | `{"request_id", "kind", "sources": [{"ref", "chunk_id", "side", "title", "topic", "text", "source_url", "source_label", "license"}], "dropped"}` | 放進上下文的段落（兩邊各最多 `item_compare.chunks_per_item` 段，`side` 是「甲」或「乙」）；`dropped`＝被地端洩密規則剔除的段數 |
| `token` | `{"text"}` | 摘要的片段，引用段落的句子附 `[ref]` |
| `done` | `{"request_id", "model", "strategy_used", "fallback", "prompt_version", "latency_ms": {"first_token", "total"}, "tokens", "egress"}` | 只走本地（`hybrid` → `hybrid_fallback`），`egress` 恆為 0 |
| `error` | `{"code", "message", "request_id"}` | `STRATEGY_UNAVAILABLE`：本地模型都無法使用（表格仍可看） |

## 問答的 `strategy`

`strategy` 可用值：`hybrid`（主架構）、`lora`（選做）、`api_nokb`／`api_kb`（雲端對照組 A1／A2，
需後端 `ALLOW_CLOUD=true`，且不接受 `image_id`）、`mock`。備援只在本地之間：
`hybrid` → `hybrid_fallback`，`lora` → `hybrid` → `hybrid_fallback`；雲端不在任何備援鏈上。

評估用的對照組選項（docs/adr/030）只在後端 `EVAL_CONTROLS=true`、而且請求來自 `DEMO_TRUSTED_HOSTS` 時生效：
`strategy=mock`（否則照 `hybrid`，`done.strategy_requested` 仍記 `mock`）、`use_retrieval=false`
（否則第 6 段直接降級；工廠圖紙即使在評估模式也一律降級）、`/agent/route` 的 `engine=local`。
`api_nokb`（A1）等於關檢索，一樣要評估模式。這些欄位都不能降低七段權限控管的任何一段。
