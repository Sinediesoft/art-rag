# 錯誤碼清單（共用層 §一，負責：B）

所有錯誤回應格式：`{"error": {"code": "...", "message": "...", "request_id": "..."}}`

| code | HTTP | 說明 |
|---|---|---|
| `VALIDATION_ERROR` | 422 | 請求參數格式錯誤 |
| `IMAGE_TOO_LARGE` | 413 | 圖片超過 10 MB |
| `IMAGE_TYPE_NOT_ALLOWED` | 415 | 只收 JPEG、PNG、WebP |
| `IMAGE_NOT_FOUND` | 404 | `image_id` 不存在、已過期（7 天自動刪除），**或不是目前身分可以用的照片**（只有上傳的帳號與擁有者用 `PUT /images/{id}/acl` 明確授權的帳號可以用；舊資料沒有擁有者一律拒絕）。找不到和沒有權限回同一個回應，不透露別人的照片存不存在（ADR 041） |
| `ARTWORK_NOT_FOUND` | 404 | 畫作 ID 不存在 |
| `ALIGN_FAILED` | 422 | 影像對位：照片和指定的知識庫原圖、圖紙或另一張照片（兩張照片互比）對不上（對應的特徵點不夠，或算出來的位置不合理），不硬畫位置框（docs/adr/012） |
| `NOT_IN_KB` | 200（SSE `error`） | 以圖辨識低於門檻：知識庫中沒有這幅畫（領域路由判為工廠圖紙時：知識庫中沒有這張圖紙） |
| `STRATEGY_UNAVAILABLE` | 200（SSE `error`） | 指定的生成端無法使用，本地備援模型也失敗（服務暫停，不改走雲端）；或雲端對照組未開啟（`ALLOW_CLOUD=false`） |
| `GENERATION_FAILED` | 200（SSE `error`） | 生成端逾時或錯誤，且本地備援也失敗 |
| `CLOUD_UPLOAD_FORBIDDEN` | 200（SSE `error`） | 雲端對照組不接受使用者上傳的照片（使用者資料不出站） |
| `PART_NOT_FOUND` | 404／SSE `error` | 圖紙 ID 不存在（`/inventory/parts/{id}`：庫存資料庫中沒有這張圖紙） |
| `CLOUD_CONFIDENTIAL_FORBIDDEN` | 200（SSE `error`） | 工廠圖紙與庫存資料屬機密，不送往任何雲端 API（包含對照組） |
| `CAD_JOB_NOT_FOUND` | 404 | 3D 重建結果不存在、已過期（與上傳照片同為 7 天），**或目前身分沒有權限讀**（工作從建立起就有擁有者與來源資源：知識庫圖紙來源要看得到那張圖紙、照片來源要是那張照片的擁有者或被授權者；未完成、失敗、完成都一樣）。回應和不存在相同。領域不符（一般員工）在查工作之前就回 403 `DATA_SCOPE_DENIED` |
| `PART_MODEL_NOT_FOUND` | 404 | 這張圖紙沒有標準 3D 模型（照片建檔的零件，`/parts/{id}/model.stl`、`model.step`；docs/adr/013） |
| `INTAKE_TOO_BLURRY` | 200（SSE `error`） | 照片建檔：照片太模糊（模糊程度超過 `intake.max_blur`，圖紙與畫作共用），不送模型、不建檔——模型看不清楚時不會留空而是猜，糊的原圖之後也認不出來（docs/adr/013） |
| `INTAKE_ALREADY_IN_KB` | 200（SSE `error`）／409 | 照片建檔：知識庫已經有這張圖紙（`part`）或這幅畫（`artwork`），不重複建檔。圖紙只和建檔人看得到的圖紙比（ADR 030），所以主管收錄（`POST /intake/{draft_id}/commit`）時會用主管的範圍再查一次，有重複回 409 |
| `INTAKE_WRONG_DOMAIN` | 200（SSE `error`） | 照片建檔：從圖紙頁進來，領域路由卻很確定是畫作（或反過來）；`route` 附上路由結果，前端帶著同一張照片轉到另一邊 |
| `INTAKE_DOMAIN_UNSUPPORTED` | 200（SSE `error`）／422 | 照片建檔：`shared/models.yaml` 沒有這個領域的 `intake` 設定 |
| `INTAKE_PAGE_NOT_FOUND` | 200（SSE `error`） | 照片建檔：找不到整張圖紙的四個角或完整的標題欄外框（沒拍完整，或不是知識庫圖紙的版面） |
| `INTAKE_DRAFT_NOT_FOUND` | 404 | 建檔草稿不存在或已過期（與上傳照片同為 7 天） |
| `INTAKE_CLOSED` | 409 | 建檔草稿已經收錄或正在收錄，不能修改、捨棄或再收錄 |
| `INTAKE_INVALID` | 422 | 收錄時還有欄位沒通過驗證，或組出來的零件 JSON 不符 `part.schema.json` |
| `INTAKE_BUSY` | 409 | 另一筆資料正在收錄、重建索引（照片建檔、區域解說一次只收錄一筆） |
| `REGION_DRAFT_NOT_FOUND` | 404 | 區域解說草稿不存在，或已收錄／退回超過保存期限（docs/adr/029） |
| `REGION_DRAFT_CLOSED` | 409 | 區域解說草稿已經收錄、退回或正在收錄，不能再收錄、退回或撤回 |
| `REGION_REJECTED` | 422 | 區域解說沒有通過檢查：輸入防護的地端規則（像在對 AI 下指令、要內部資料）、合進畫作 JSON 後不符 schema 或區域規則、畫作的圖在送出後換過 |
| `SQL_REJECTED` | 200（SSE `error`） | 庫存 Text-to-SQL：模型產生的 SQL 含寫入或管理指令（使用者要求修改資料），執行前攔下、不進修正迴圈；沒有任何資料被修改 |
| `SQL_FAILED` | 200（SSE `error`） | 庫存 Text-to-SQL：修正 2 次後仍無法產生可執行的 SQL |
| `INVENTORY_UNAVAILABLE` | 200（SSE `error`） | 庫存資料庫無法建立（`kb/inventory/` 資料有誤，細節見 `/health` 的 `inventory.problems`） |
| `SCHEDULE_DATA_INVALID` | 503／SSE `error` | 生產排程資料有誤（`kb/production/` 的機台、行事曆或途程不符 schema，細節在 message） |
| `ROUTING_NOT_FOUND` | 422 | 開立工單時，這張圖紙還沒有製程途程（`kb/production/routings/<part_id>.json`） |
| `WORK_ORDER_NOT_FOUND` | 404 | 取消工單時找不到（只能取消圖紙頁開立、尚未取消的工單；`kb/inventory` 的既有工單不能取消） |
| `NO_WORK_ORDERS` | 200（SSE `error`） | 生產排程：沒有需要排程的工單 |
| `SCHEDULE_BUSY` | 200（SSE `error`）／409 | 生產排程：已有排程正在計算（同一時間只跑一個，跨所有 API 與 worker 行程）。`POST /schedule/jobs` 回 409 |
| `SCHEDULER_UNAVAILABLE` | 200（SSE `error`） | Timefold 排程服務在求解途中斷線（一開始就連不上時不會報錯，而是改用簡易排程並在 `meta.fallback_reason` 說明） |
| `SCHEDULE_FAILED` | 200（SSE `error`） | Timefold 求解失敗（排程服務回報例外） |
| `SCHEDULE_RUN_NOT_FOUND` | 404 | 排程結果 `run_id` 不存在 |
| `JOBS_UNAVAILABLE` | 503 | 背景工作系統目前無法使用（沒有啟用 `REDIS_URL`／工作資料庫，或工作資料庫連不上）。可恢復：稍後重試；工作不會改在 API 行程執行（ADR 041）。Redis 單獨不通時不是這個錯誤：工作照樣建立（202），回應 `delayed_dispatch: true`，留在 outbox 等 Redis 恢復後投遞 |
| `JOB_NOT_FOUND` | 404 | 背景工作不存在、已過期，或目前身分沒有權限讀（`GET /jobs/{id}`、`/events`、`/cancel`、`/artifacts/{name}` 回應相同） |
| `JOB_NOT_FINISHED` | 409 | 產物要等工作完成（成功或失敗）才發布；工作還在排隊或執行 |
| `JOB_ARTIFACT_NOT_FOUND` | 404 | 產物名稱不在白名單（`input.png`、`code.py`、`model.stl`、`model.step`、`reproj.png`）或沒有發布 |
| `RATE_LIMITED` | 429 | 同一個帳號短時間內下載機密零件的原尺寸圖紙、STEP／STL 模型太多次（`shared/access.yaml` 的 `read_limits`，預設 10 分鐘 12 次）；這次不給檔案，事件記進安全紀錄 |
| `JOBS_LIMIT_REACHED` | 429 | 這個帳號同時排隊或執行中的 3D 重建已達上限（`JOBS_MAX_ACTIVE_PER_OWNER`，預設 5），等完成或取消幾個再建立 |
| `JOB_COMMITTING` | 409 | 取消：工作已進入寫回階段，不能取消（會完成並寫入結果） |
| `JOB_ALREADY_FINISHED` | 409 | 取消：工作已經成功或失敗 |
| `IDEMPOTENCY_KEY_REUSED` | 409 | `Idempotency-Key` 已用在參數不同的請求（建立工作、開／取消工單、確認異動、送核准、核准） |
| `IDEMPOTENCY_IN_PROGRESS` | 409 | 相同帳號、操作與 key 的請求還在處理中，稍後重送會拿到同一個結果 |
| `USE_JOBS_API` | 409 | 這台伺服器的 3D 重建與排程改由背景 worker 執行（`LEGACY_SYNC_JOBS=off`，或 `auto` 且已啟用工作系統），舊的 SSE 入口（`POST /cad/reconstruct`、`/schedule/solve`）停用；請改用 `POST /cad/jobs`、`POST /schedule/jobs` |
| `SCHEDULE_INPUT_CHANGED` | 409／工作 `error.code` | 排程輸入（工單、途程、機台、行事曆）和工作建立時不同，或求解期間有人改了：舊解不寫回、不覆蓋新資料，請重新排程 |
| `SCHEDULE_INVALID_SOLUTION` | 422／工作 `error.code` | 後端重算發現排程結果違反硬限制或缺工序，沒有寫回 |
| `SCHEDULE_SCORE_MISMATCH` | 422／工作 `error.code` | Timefold 回的分數和後端用同一套規則重算的不一致，沒有寫回 |
| `CAD_EXEC_FAILED` | 工作 `error.code` | 3D 重建的沙箱執行失敗（`message` 只留第一行；程式碼、輸入圖在受保護的產物端點給授權的人看） |
| `CAD_NO_RESULT` | 工作 `error.code` | 3D 重建沒有產出結果事件 |
| `CAD_PUBLISH_CONFLICT` | 工作 `error.code` | 這一次嘗試的產物資料夾（`<job>/a<attempt>`）已經存在：不刪、不覆蓋，工作失敗（正常流程不會發生） |
| `LEASE_EXPIRED` | 工作 `error.code` | worker 連續多次中斷（租約過期），已停止重試 |
| `PERMISSION_REVOKED` | 403／工作 `error.code` | 提交工作的帳號已不存在或被撤銷；其他撤權（操作、領域、圖紙、照片）依原因回 `PERMISSION_DENIED`／`DATA_SCOPE_DENIED`／`IMAGE_NOT_FOUND`，工作在領取後、執行前、寫回前各驗一次，失敗就不讀圖、不呼叫模型、不寫入 |
| `INPUT_BLOCKED` | 403 | 工具入口（`POST /inventory/ask`）的共用輸入防護：命中第 2 段的地端 hard-block 規則，不呼叫模型、不查資料，記一筆拒絕並記錄（ADR 041） |
| `UNAUTHENTICATED` | 401 | 沒有身分憑證（JWT）。七段權限控管第 1 段（ADR 015）：所有 `/api/v1` 請求都要帶，只有 `/health`、`/auth/accounts` 例外（2026-10-06 起 `/auth/switch` 也要憑證，ADR 030）；前端會向 `/auth/accounts` 取得一般員工的憑證後重送 |
| `TOKEN_INVALID` | 401 | 憑證的簽章不符、格式不對、`alg` 不是 HS256、簽發者不對（被竄改或偽造）。寫進拒絕並記錄（第 1 段「憑證無效」），前端不重送 |
| `TOKEN_EXPIRED` | 401 | 憑證過期（預設 8 小時，`JWT_TTL_MIN`）；前端自動重新取得 |
| `TOKEN_STALE` | 401 | 憑證是舊的簽章金鑰簽發的（`JWT_SECRET` 留空時後端每次啟動換金鑰），或簽章相符但帳號已經從 `access.yaml` 拿掉（2026-10-13 起，ADR 036）；前端自動重新取得、不記錄 |
| `PERMISSION_DENIED` | 403 | 目前身分沒有這個權限（角色、資料範圍、只能取消自己開的工單（主管例外，ADR 036）、不能核准自己的申請、確認卡不是目前身分建立的）；圖紙頁開立工單、取消工單、開始排程、展示還原、照片建檔收錄（只有主管，ADR 013）也會回這個（ADR 011）；管理與診斷畫面（`/admin/diagnostics`、`/security/logs`、`/audit`）沒有 `access.yaml` 的 `views` 權限時也是這個（ADR 030） |
| `DATA_SCOPE_DENIED` | 403／SSE `error` | 目前身分的資料範圍不含這份資料（ADR 014）：一般員工不能使用工廠圖紙與工廠資料庫、業務看不到機密圖紙。所有讀取 API 都檢查（含影像比對 `/images/{image_id}/align`、`align.png` 指定 `part:` 時）；`/cad/reconstruct` 用照片辨識出看不到的圖紙時改在串流裡回 `error`。2026-10-06 起（ADR 030）看不看得到同時看機密等級與部門（`identity.can_view_part`），訊息不寫圖紙名稱；`/chat` 只有角色不能用工廠圖紙時回這個，指定了看不到或不存在的圖紙一律在串流裡降級「查無資料」（不透露文件存在） |
| `APPROVAL_REQUIRED` | 409 | 超過額度（例如急件工單、報廢超過 10 件），要送主管核准，不能直接寫入；主管自己的不會回這個，直接生效（ADR 036） |
| `APPROVAL_NOT_NEEDED` | 409 | 額度內的修改不需要送主管核准，直接確認即可；主管自己超過額度也回這個（主管身分直接生效，ADR 036） |
| `APPROVAL_NOT_FOUND` | 404 | 待核准單號不存在 |
| `APPROVAL_CLOSED` | 409 | 待核准單已經核准、退回或失效 |
| `CHANGE_NOT_FOUND` | 404 | 確認卡已過期（15 分鐘）或已使用，請重新輸入 |
| `CHANGE_STALE` | 409 | 從試算到按確認之間，受影響的資料已被修改（指紋不同），沒有寫入 |
| `ACCOUNT_NOT_FOUND` | 404 | 切換身分時沒有這個展示帳號 |
| `FORBIDDEN` | 403 | 展示模式沒開或不是受信任來源的請求（`DEMO_CONTROLS=false`（預設），或請求不是來自 `DEMO_TRUSTED_HOSTS` 時呼叫 `POST /api/v1/admin/outage`、`/admin/memory/release`、`/admin/production/reset`、`/auth/switch`；ADR 030）；或 `/chat` 帶 `inject` 但未同時開啟 `EVAL_INJECTION=true`、`EVAL_CONTROLS=true` 且來自受信任來源（干擾段落注入只給評估用，ADR 019） |
| `COMPARE_KIND_MISMATCH` | 422 | 兩件並排比較（`/compare/items`、`/compare/summary`）的兩件不同類：一幅畫和一張圖紙（docs/adr/017） |
| `INDEX_MISMATCH` | 503 | 索引 manifest 與 `shared/models.yaml`／`kb/VERSION` 不一致 |
| `INTERNAL_ERROR` | 500 | 其他未預期錯誤；訊息固定為「伺服器內部錯誤」，例外內容只寫伺服器 log（用 `request_id` 對照） |
| `DATABASE_UNAVAILABLE` | 503／SSE `error` | 資料庫暫時連不上（Docker 停了、連線池拿不到連線）；串流途中出錯時以 SSE `error` 事件送出，之後串流結束 |
| `NOT_FOUND` | 404 | 沒有這個 API（`/api/...` 底下不存在的路徑） |
| `METHOD_NOT_ALLOWED` | 405 | 這個 API 不接受這種 HTTP 方法 |
| `HTTP_ERROR` | 其他 4xx | 框架層的其他 HTTP 錯誤 |
