# 共用層變更紀錄

| 日期 | 改了什麼 | 誰要跟著改 |
|---|---|---|
| 2026-09-29 | 建立 demo 版共用層：models.yaml、artwork.schema.json、answer_v1、error_codes、sse_events | 全員 |
| 2026-09-29 | models.yaml：以圖搜圖改為兩階段（CLIP 粗篩＋ORB 幾何驗證），新增 verify_top_n、verify_min_inliers；image_threshold 0.80→0.70。/search/image 回應新增 inliers、verified 欄位 | A（前端顯示）、C、D（校正門檻） |
| 2026-09-29 | 依新版共用層清單（地端＋隱私）：備援改為本地（`hybrid` → `hybrid_fallback`），不再改走雲端；新增 `HYBRID_FALLBACK_*`、`ALLOW_CLOUD`（預設 false）、`HF_OFFLINE`；strategy `api` 拆成 `api_nokb`（A1）與 `api_kb`（A2），雲端不接受 `image_id`；本地策略只准連本機／內網位址；models.yaml 新增 `hybrid_fallback`、`api_nokb`、`api_kb`、`min_chunk_score`、`relative_chunk_ratio`、`global_fill_keywords`（已指定畫作只取該畫段落）；SSE `done` 與 chat_logs 新增外送資料量 egress；新錯誤碼 `CLOUD_UPLOAD_FORBIDDEN`；評估 CSV 新增 type、artwork_id、source_lang、egress 欄位 | A（前端：本地備援標示、外送標示、比較頁 A1／A2）、B（.env 新變數）、C（備援模型、門檻校正）、D（qa.jsonl 補題目類型與新題型） |
| 2026-09-30 | 新增「工廠機械加工圖」領域：`shared/schemas/part.schema.json`、`shared/prompts/drawing_v1.md`；models.yaml 新增 `drawing_retrieval`（圖紙辨識門檻，含線條重合度 `verify_min_overlap`）、`cad`（Ortho2CAD prompt、`max_pixels`、沙箱逾時）、`prompt.drawing_version`、策略 `ortho2cad`；`.env` 新增 `ORTHO2CAD_*`；新 API `/parts*`、`/search/drawing`、`/search/parts`、`/cad/reconstruct`（SSE）、`/cad/jobs/*`、`/eval/cad-runs`；`ChatRequest` 新增 `part_id`；SSE `sources` 的段落新增 `title`（畫作仍保留 `artwork_title`）；新錯誤碼 `PART_NOT_FOUND`、`CLOUD_CONFIDENTIAL_FORBIDDEN`、`CAD_JOB_NOT_FOUND`；索引新增 `data/index/parts/` | A（圖紙頁面）、B（llama-server 部署）、C（Ortho2CAD、辨識門檻）、D（圖紙知識庫、實拍照） |
| 2026-09-30 | error_codes.md 補上 `FORBIDDEN`（403，`DEMO_CONTROLS=false` 時的 `/admin/outage`，程式早已使用）；依 demo 進度更新《共用層注意事項清單》：已實作條目打勾、未完成條目註記現況，新增〔圖紙〕條目 | 全員（對照清單認領未完成條目） |
