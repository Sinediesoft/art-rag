# 錯誤碼清單（共用層 §一，負責：B）

所有錯誤回應格式：`{"error": {"code": "...", "message": "...", "request_id": "..."}}`

| code | HTTP | 說明 |
|---|---|---|
| `VALIDATION_ERROR` | 422 | 請求參數格式錯誤 |
| `IMAGE_TOO_LARGE` | 413 | 圖片超過 10 MB |
| `IMAGE_TYPE_NOT_ALLOWED` | 415 | 只收 JPEG、PNG、WebP |
| `IMAGE_NOT_FOUND` | 404 | `image_id` 不存在或已過期（7 天自動刪除） |
| `ARTWORK_NOT_FOUND` | 404 | 畫作 ID 不存在 |
| `ALIGN_FAILED` | 422 | 影像對位：照片和指定的知識庫原圖、圖紙或另一張照片（兩張照片互比）對不上（對應的特徵點不夠，或算出來的位置不合理），不硬畫位置框（docs/adr/012） |
| `NOT_IN_KB` | 200（SSE `error`） | 以圖辨識低於門檻：知識庫中沒有這幅畫（領域路由判為工廠圖紙時：知識庫中沒有這張圖紙） |
| `STRATEGY_UNAVAILABLE` | 200（SSE `error`） | 指定的生成端無法使用，本地備援模型也失敗（服務暫停，不改走雲端）；或雲端對照組未開啟（`ALLOW_CLOUD=false`） |
| `GENERATION_FAILED` | 200（SSE `error`） | 生成端逾時或錯誤，且本地備援也失敗 |
| `CLOUD_UPLOAD_FORBIDDEN` | 200（SSE `error`） | 雲端對照組不接受使用者上傳的照片（使用者資料不出站） |
| `PART_NOT_FOUND` | 404／SSE `error` | 圖紙 ID 不存在（`/inventory/parts/{id}`：庫存資料庫中沒有這張圖紙） |
| `CLOUD_CONFIDENTIAL_FORBIDDEN` | 200（SSE `error`） | 工廠圖紙與庫存資料屬機密，不送往任何雲端 API（包含對照組） |
| `CAD_JOB_NOT_FOUND` | 404 | 3D 重建結果不存在或已過期（與上傳照片同為 7 天） |
| `PART_MODEL_NOT_FOUND` | 404 | 這張圖紙沒有標準 3D 模型（照片建檔的零件，`/parts/{id}/model.stl`、`model.step`；docs/adr/013） |
| `INTAKE_TOO_BLURRY` | 200（SSE `error`） | 照片建檔：照片太模糊（模糊程度超過 `intake.max_blur`，圖紙與畫作共用），不送模型、不建檔——模型看不清楚時不會留空而是猜，糊的原圖之後也認不出來（docs/adr/013） |
| `INTAKE_ALREADY_IN_KB` | 200（SSE `error`） | 照片建檔：知識庫已經有這張圖紙（`part`）或這幅畫（`artwork`），不重複建檔 |
| `INTAKE_WRONG_DOMAIN` | 200（SSE `error`） | 照片建檔：從圖紙頁進來，領域路由卻很確定是畫作（或反過來）；`route` 附上路由結果，前端帶著同一張照片轉到另一邊 |
| `INTAKE_DOMAIN_UNSUPPORTED` | 200（SSE `error`）／422 | 照片建檔：`shared/models.yaml` 沒有這個領域的 `intake` 設定 |
| `INTAKE_PAGE_NOT_FOUND` | 200（SSE `error`） | 照片建檔：找不到整張圖紙的四個角或完整的標題欄外框（沒拍完整，或不是知識庫圖紙的版面） |
| `INTAKE_DRAFT_NOT_FOUND` | 404 | 建檔草稿不存在或已過期（與上傳照片同為 7 天） |
| `INTAKE_CLOSED` | 409 | 建檔草稿已經收錄或正在收錄，不能修改、捨棄或再收錄 |
| `INTAKE_INVALID` | 422 | 收錄時還有欄位沒通過驗證，或組出來的零件 JSON 不符 `part.schema.json` |
| `INTAKE_BUSY` | 409 | 另一張圖紙正在收錄、重建索引（一次只收錄一張） |
| `SQL_REJECTED` | 200（SSE `error`） | 庫存 Text-to-SQL：模型產生的 SQL 含寫入或管理指令（使用者要求修改資料），執行前攔下、不進修正迴圈；沒有任何資料被修改 |
| `SQL_FAILED` | 200（SSE `error`） | 庫存 Text-to-SQL：修正 2 次後仍無法產生可執行的 SQL |
| `INVENTORY_UNAVAILABLE` | 200（SSE `error`） | 庫存資料庫無法建立（`kb/inventory/` 資料有誤，細節見 `/health` 的 `inventory.problems`） |
| `SCHEDULE_DATA_INVALID` | 503／SSE `error` | 生產排程資料有誤（`kb/production/` 的機台、行事曆或途程不符 schema，細節在 message） |
| `ROUTING_NOT_FOUND` | 422 | 開立工單時，這張圖紙還沒有製程途程（`kb/production/routings/<part_id>.json`） |
| `WORK_ORDER_NOT_FOUND` | 404 | 取消工單時找不到（只能取消圖紙頁開立、尚未取消的工單；`kb/inventory` 的既有工單不能取消） |
| `NO_WORK_ORDERS` | 200（SSE `error`） | 生產排程：沒有需要排程的工單 |
| `SCHEDULE_BUSY` | 200（SSE `error`） | 生產排程：已有排程正在計算（同一時間只跑一個） |
| `SCHEDULER_UNAVAILABLE` | 200（SSE `error`） | Timefold 排程服務在求解途中斷線（一開始就連不上時不會報錯，而是改用簡易排程並在 `meta.fallback_reason` 說明） |
| `SCHEDULE_FAILED` | 200（SSE `error`） | Timefold 求解失敗（排程服務回報例外） |
| `SCHEDULE_RUN_NOT_FOUND` | 404 | 排程結果 `run_id` 不存在 |
| `PERMISSION_DENIED` | 403 | 目前身分沒有這個權限（角色、資料範圍、只能取消自己開的工單、不能核准自己的申請、確認卡不是目前身分建立的）；圖紙頁開立工單、取消工單、開始排程、展示還原、照片建檔收錄（只有主管，ADR 013）也會回這個（ADR 011） |
| `APPROVAL_REQUIRED` | 409 | 超過額度（例如急件工單、報廢超過 10 件），要送主管核准，不能直接寫入 |
| `APPROVAL_NOT_NEEDED` | 409 | 額度內的修改不需要送主管核准，直接確認即可 |
| `APPROVAL_NOT_FOUND` | 404 | 待核准單號不存在 |
| `APPROVAL_CLOSED` | 409 | 待核准單已經核准、退回或失效 |
| `CHANGE_NOT_FOUND` | 404 | 確認卡已過期（15 分鐘）或已使用，請重新輸入 |
| `CHANGE_STALE` | 409 | 從試算到按確認之間，受影響的資料已被修改（指紋不同），沒有寫入 |
| `ACCOUNT_NOT_FOUND` | 404 | 切換身分時沒有這個展示帳號 |
| `FORBIDDEN` | 403 | 展示控制已停用（`DEMO_CONTROLS=false` 時呼叫 `POST /api/v1/admin/outage`、`/admin/memory/release`、`/admin/production/reset`、`/auth/switch`） |
| `INDEX_MISMATCH` | 503 | 索引 manifest 與 `shared/models.yaml`／`kb/VERSION` 不一致 |
| `INTERNAL_ERROR` | 500 | 其他未預期錯誤 |
