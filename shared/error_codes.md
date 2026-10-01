# 錯誤碼清單（共用層 §一，負責：B）

所有錯誤回應格式：`{"error": {"code": "...", "message": "...", "request_id": "..."}}`

| code | HTTP | 說明 |
|---|---|---|
| `VALIDATION_ERROR` | 422 | 請求參數格式錯誤 |
| `IMAGE_TOO_LARGE` | 413 | 圖片超過 10 MB |
| `IMAGE_TYPE_NOT_ALLOWED` | 415 | 只收 JPEG、PNG、WebP |
| `IMAGE_NOT_FOUND` | 404 | `image_id` 不存在或已過期（7 天自動刪除） |
| `ARTWORK_NOT_FOUND` | 404 | 畫作 ID 不存在 |
| `NOT_IN_KB` | 200（SSE `error`） | 以圖辨識低於門檻：知識庫中沒有這幅畫（領域路由判為工廠圖紙時：知識庫中沒有這張圖紙） |
| `STRATEGY_UNAVAILABLE` | 200（SSE `error`） | 指定的生成端無法使用，本地備援模型也失敗（服務暫停，不改走雲端）；或雲端對照組未開啟（`ALLOW_CLOUD=false`） |
| `GENERATION_FAILED` | 200（SSE `error`） | 生成端逾時或錯誤，且本地備援也失敗 |
| `CLOUD_UPLOAD_FORBIDDEN` | 200（SSE `error`） | 雲端對照組不接受使用者上傳的照片（使用者資料不出站） |
| `PART_NOT_FOUND` | 404／SSE `error` | 圖紙 ID 不存在 |
| `CLOUD_CONFIDENTIAL_FORBIDDEN` | 200（SSE `error`） | 工廠圖紙屬機密，不送往任何雲端 API（包含對照組） |
| `CAD_JOB_NOT_FOUND` | 404 | 3D 重建結果不存在或已過期（與上傳照片同為 7 天） |
| `FORBIDDEN` | 403 | 展示控制已停用（`DEMO_CONTROLS=false` 時呼叫 `POST /api/v1/admin/outage`） |
| `INDEX_MISMATCH` | 503 | 索引 manifest 與 `shared/models.yaml`／`kb/VERSION` 不一致 |
| `INTERNAL_ERROR` | 500 | 其他未預期錯誤 |
