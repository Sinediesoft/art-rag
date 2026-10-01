# SSE 事件格式（共用層 §一，負責：B）

`POST /api/v1/chat` 回傳 `text/event-stream`，只有四種事件，依序出現：

| event | data（JSON） | 說明 |
|---|---|---|
| `sources` | `{"request_id", "artwork_id", "strategy", "sources": [{"ref", "chunk_id", "artwork_id", "artwork_title", "text", "source_url", "license", "score"}]}` | 檢索到的來源，`ref` 即回答中的 [編號] |
| `token` | `{"text"}` | 文字片段（已經過 OpenCC s2twp 轉換） |
| `done` | `{"request_id", "strategy_requested", "strategy_used", "model", "fallback", "fallback_reason", "prompt_version", "use_retrieval", "latency_ms": {"retrieval", "first_token", "generation", "total"}, "tokens": {"input", "output"}, "cost_twd", "egress": {"images", "chunks", "bytes"}}` | 完成；`fallback` 為 true 時前端顯示「本地備援模型」（`strategy_used` 為 `hybrid_fallback`）；`egress` 是送出本機的資料量，本地策略恆為 0，前端顯示「資料外送」標示 |
| `error` | `{"code", "message", "request_id"}` | 錯誤，之後不再有其他事件 |

`sources` 另帶 `part_id`、`identified`（有做以圖辨識時的辨識結果）與 `route`：只帶 `image_id`、
沒指定 `artwork_id`／`part_id` 時，後端先用領域路由判斷是畫作還是工廠圖紙（`{"domain", "margin", "art_score",
"mfg_score", "min_margin", "uncertain"}`，格式同 `/search/any` 的 `route`），判為圖紙就走下面的圖紙問答；沒經過路由時為 `null`。

`sources` 另帶 `rearrange`：開啟檢索段落篩選（MIRA 的 Rearrange，見 docs/adr/005；請求的 `rearrange`、
`.env` 的 `REARRANGE` 或 `models.yaml` 的 `rearrange.enabled`，預設關）時為
`{"candidates", "kept", "ms", "fallback"}`——候選幾段、模型留下幾段、篩選花幾毫秒、失敗原因（成功為 `null`，
失敗時 `sources` 是原本的全部段落）；沒有篩選時為 `null`。`sources` 永遠只列真正放進 prompt 的段落，
`ref` 從 1 重新編號。`done.latency_ms.retrieval` 包含篩選時間。

`part_id`（工廠圖紙問答）走同一組事件：`sources` 的每段改帶 `part_id`、`title`、`source_label`
（內部文件名稱），`source_url` 為 `null`；`done.prompt_version` 為 `drawing_v1`。圖紙屬機密，
雲端策略一律回 `error`（`CLOUD_CONFIDENTIAL_FORBIDDEN`）。

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

## 問答的 `strategy`

`strategy` 可用值：`hybrid`（主架構）、`lora`（選做）、`api_nokb`／`api_kb`（雲端對照組 A1／A2，
需後端 `ALLOW_CLOUD=true`，且不接受 `image_id`）、`mock`。備援只在本地之間：
`hybrid` → `hybrid_fallback`，`lora` → `hybrid` → `hybrid_fallback`；雲端不在任何備援鏈上。
