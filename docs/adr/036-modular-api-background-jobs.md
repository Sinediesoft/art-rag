# ADR 036：模組化 API、共用安全上下文與背景工作（Redis＋Celery＋PostgreSQL）

- 日期：2026-10-13
- 狀態：採用（TASK-20261013-001）
- 參考：`ArtRAG-API-架構工作台.html` 的「方案 B：模組化後端＋獨立背景工作」。HTML 是目標架構，這份 ADR 記錄實際做了什麼、沒做什麼
- 相關：ADR 030（七段修補，七段規則以它為準）、011、015、004、005、035、006／021

## 背景
- 路由全在一個 `routes.py`；3D 重建與排程用 SSE，求解的生命週期綁在 API 行程與連線上（離開頁面就停止求解）。
- 隔離環境（假檔案、有效訪客 JWT、mock solver）重現三項授權缺口：訪客可以 `POST /schedule/stop`；知道 `image_id` 就能讀別人上傳的照片；沒有 `summary.json` 的 3D 重建產物略過文件授權。
- 交接票、排程忙碌鎖、使用中模型登記都是行程內的字典與鎖，多開一個行程就失效。

## 決定

### 1. 一個 `/api/v1` 入口，依領域拆成 APIRouter（AC-01）
不拆微服務。`app/api/routes.py` 只負責總成，`app/api/routers/` 依領域分檔，Router → Service → Repository／地端工具：

| 模組 | 範圍 |
|---|---|
| `artworks` | 畫作圖紙：公開畫作清單、`/search/image`、`/search/text`、色彩、藝術家區域註記 |
| `parts` | 工廠圖紙：`/parts`、`/search/drawing`、`/search/parts`、原圖／模型 |
| `inventory` | 工廠資料庫：`/inventory/*`（固定查詢＋唯讀 Text-to-SQL） |
| `cad` | 工廠 3D 重建：舊 SSE 入口與結果相容讀取 |
| `production` | 工單、舊 SSE 求解入口、`/schedule/stop`、`/schedule/runs/{id}` |
| `jobs` | 背景工作：`POST /cad/jobs`、`POST /schedule/jobs`、`/jobs*` |
| 共用 | `auth`、`images`（上傳與衍生資料）、`chat`（七段管線，畫作與圖紙共用，不複製）、`agent`、`changes`（異動＋核准）、`tools`（批次／比較／匯出／建檔）、`system` |

問答（`/chat`）畫作與圖紙共用同一條七段管線，依後端授權的領域分流，各功能模組不各寫一份防護。

### 2. 共用安全上下文（AC-04、AC-05）
`services/security.py`：`SecurityContext`（帳號、操作、領域、`request_id`）只能由伺服器建立——`for_request()`（HTTP，帳號來自閘道驗過的 JWT 再對回 `access.yaml`，不看 JWT 裡的角色宣告）與 `for_account()`（背景 worker 依 `owner_id` 對回「目前的」`access.yaml`）。請求內容沒有任何欄位可以指定角色、機密等級、部門或放寬範圍。直接 API、助理入口、worker 與下載端點呼叫同一套政策函式。

- `access.yaml` 依檔案修改時間重讀（`core/config.get_access_config`）：撤權不必重啟任何行程。
- 工具入口：`POST /inventory/ask` 先過第 2 段的地端 hard-block（`guard.tool_input_check`，命中回 403 `INPUT_BLOCKED`、記一筆拒絕並記錄、不呼叫模型）；CAD 與固定查詢的 id 欄位用結構化驗證（pattern）。工具沒有檢索段落，所以不假裝經過 RAG 第 3～6 段；SQL 的 SELECT 白名單與唯讀、CAD 的 AST／平台沙箱／孔位整體回退（ADR 035）、Timefold 限制與後端重算分數原樣保留。
- 七段順序與規則不變；`route_ticket` 最多沿用第 2 段，第 1、3～7 段仍由後端執行。有 `REDIS_URL` 時交接票存在 Redis（任一 API 行程簽發、任一行程核銷，帳號／問句／對象任一不符就不認；Redis 不通＝沒票＝`/chat` 重跑第 2 段），沒有時維持行程內。

### 3. 先閉合三項已重現的缺口（AC-06～AC-08）
| 缺口 | 修補 |
|---|---|
| `/schedule/stop` | 要 `schedule_run`（只有生管）＋能讀工廠資料庫；訪客、業務、倉管、藝術家、主管都是 403，擋下後 `stop()` 沒被呼叫 |
| 上傳照片 | `uploads` 記錄 `owner_id` 與 `acl`（擁有者用 `PUT /images/{id}/acl` 明確授權）。原圖、色彩、色塊圖、畫作卡、對位、辨識（圖／圖紙／不指定領域）、問答、助理路由、建檔、批次、3D 重建都先驗；結果以 `image_id` 快取的服務，授權在快取之外每次都驗。找不到和沒有權限同一個 404 `IMAGE_NOT_FOUND`。舊資料沒有擁有者＝沒有人能用；管理者用 `python -m app.jobs.admin adopt-uploads --owner <帳號>` 遷移。照片建檔草稿（含照片副本）同樣只有建檔人與能收錄的身分（主管）讀得到 |
| 3D 重建產物 | 工作從建立起就有存取紀錄（背景工作＝jobs 列；舊 SSE 入口＝資料夾裡第一個寫入的 `access.json`）。未完成、失敗、完成的狀態、事件、摘要、程式碼、輸入圖、預覽與模型都走 `cad_can_read`：知識庫圖紙來源要看得到該圖紙；照片來源要是照片的擁有者或被授權者；舊資料夾沒有存取紀錄：知識庫圖紙來源照圖紙規則可讀（相容讀取），照片來源或完全沒有紀錄的拒絕，`adopt-cad` 補紀錄 |

### 4. 背景工作（AC-09～AC-14）
```
 瀏覽器 ──202 job_id──▶ API ──同一交易──▶ PostgreSQL: jobs + job_events + job_outbox
   ▲ SSE（Last-Event-ID）/輪詢        │                       ▲  ▲
   └────────── 查 jobs／job_events ────┘                       │  │ 租約、心跳、fenced 寫入
                       dispatcher（API／worker 內建執行緒，SKIP LOCKED）                │  │
                              │ Celery send_task（只帶 job_id）                          │  │
                              ▼                                                          │  │
                          Redis ──▶ worker(gpu 佇列：3D 重建) ───────────────────────────┘  │
                                └─▶ worker(schedule 佇列：排程求解) ─▶ Timefold／生產 SQLite ┘
```
- **資料**（`jobs/migrations/0001_jobs.up.sql`）：`jobs`（狀態、擁有者、範圍、參數、資源、輸入版本、嘗試次數、租約、取消／停止旗標、進度、結果、錯誤）、`job_events`（同一工作內連續遞增的 `seq`，`Last-Event-ID`）、`job_outbox`、`job_artifacts`、`resource_leases`。版本化 migration（`schema_migrations` 記雜湊，已套用的檔案被改過就拒絕）＋`.down.sql`。
- **建立**：先授權（失敗不留工作），再 `jobs` 列＋`created` 事件＋outbox 同一個交易。`Idempotency-Key`：相同帳號／操作／key 的相同請求拿到同一個工作（`idempotent_replay: true`），參數不同 409 `IDEMPOTENCY_KEY_REUSED`；排程範圍唯一的部分索引讓同一時間只有一個排隊或執行中的排程工作（409 `SCHEDULE_BUSY`）。
- **投遞**：dispatcher 以 `FOR UPDATE SKIP LOCKED` 取 outbox 送 Celery，失敗指數退讓、留在 outbox；排隊中卻很久沒被領走的工作（broker 重啟、訊息遺失）會補投遞。至少一次投遞，重複由 `claim` 擋掉。Redis 單獨不通：工作仍建立（202，`delayed_dispatch: true`，維持 `queued`），恢復後投遞；工作資料庫不通：503 `JOBS_UNAVAILABLE`。**不會**退回 API 行程執行。
- **領取與互斥**：`claim` 在一個交易裡鎖列、取得資源租約（排程範圍 `schedule:plant`，或 GPU 名額 `gpu:0…`，`JOBS_GPU_SLOTS` 預設 1）才轉成 `running`。租約有期限、過期的直接接手並遞增 fencing。舊的 SSE 排程入口（`LEGACY_SYNC_JOBS=on`）也拿同一個範圍租約，所以和背景工作互斥。
- **狀態機**：`queued → running → succeeded／failed／cancelled`，記錄時間與 `attempt`。每個寫入都是帶條件的單一 UPDATE（`attempt`＋`lease_owner`＋`status='running'`，而且工作**仍持有自己的資源租約**——`resource_leases` 的 holder 還是這個工作、attempt 一致），租約被接手後的舊 worker 一律 `LeaseLost`，什麼都寫不進去。
- **心跳與恢復**：worker 每 `JOBS_HEARTBEAT_S` 續約並讀回取消／停止旗標；超過 `JOBS_LEASE_S` 沒續約，恢復迴圈把工作放回佇列（用完 `JOBS_MAX_ATTEMPTS` 就失敗為 `LEASE_EXPIRED`，要求取消的直接 `cancelled`）。**這是整個重新執行，不是從 LLM token 中段續跑**，文件與事件都不宣稱續跑。
- **寫回與競態**：進入寫回前先 `reserve_commit`（資料庫列上的原子條件：沒被要求取消才成功）。取消在前 → worker 看到旗標，不寫回、標 `cancelled`；寫回在前 → 取消回 409 `JOB_COMMITTING`，工作會完成。寫回期間持有工作專用的 advisory lock，恢復迴圈遇到就跳過，所以租約剛好過期的舊 worker 不會和接手的新 worker 同時寫回。
  - **資源租約的 fencing**：GPU 名額或排程範圍的租約過期後可能被別的工作接手，而原工作的 `jobs` 列在恢復迴圈收回之前仍是 `running`。所以續約（`heartbeat`）要同時更新工作列與資源租約，資源租約的 holder 已經不是自己就丟 `LeaseLost` 並回滾（不替它延長工作租約）；`reserve_commit` 與所有帶 fencing 的寫入（事件、結束）同樣要求仍持有資源；拿到寫回鎖之後、寫入之前再用 `verify_commit` 確認一次（仍是現行嘗試、已取得寫回資格、仍持有資源，並延長租約）。過期但沒有被接手的租約仍可續約。測試：`test_jobs_store`（`test_old_worker_cannot_keep_working_after_its_gpu_slot_was_taken` 等）、`test_jobs_runner`（寫回前資源被接手就放棄、不寫入、不發布）。
- **取消與提前停止是兩件事**：`POST /jobs/{id}/cancel` 放棄、不寫回；`POST /schedule/stop`（可帶 `{job_id}`）請 Timefold 提前結束，採用目前最佳的合法解，照常驗證、寫回，`result.run_status` 為 `stopped`。兩者都驗授權（排程要 `schedule_run`；3D 重建取消只有建立的人）。
- **事件**：`GET /jobs/{id}/events` 輪詢資料庫送出持久化事件，每一輪重驗目前權限（被撤權就送 `error: PERMISSION_REVOKED` 並結束），終態後結束；輪詢備援 `GET /jobs/{id}`。進度事件**不含模型輸出**：3D 重建的生成進度只有字元數，程式碼放在受保護的 `code.py` 產物；失敗訊息只留第一行。
- **產物**：worker 寫到暫存資料夾，成功寫回時整個資料夾一次改名發布（原子），讀取端不會看到半成品。`GET /jobs/{id}/artifacts/{name}` 白名單（`input.png`、`code.py`、`model.stl`、`model.step`、`reproj.png`），工作完成前 409，每次請求重驗目前授權，`no-store`＋`nosniff`。舊網址 `GET /cad/jobs/{id}/{name}` 走同一套授權。
- **權限重驗**：worker 在領取後、執行前、寫回前（取得寫回資格後、發布／寫入前）各依「目前的」`access.yaml` 重驗一次（領域、操作、圖紙範圍、上傳照片擁有者）；失敗＝不讀圖、不呼叫模型或 solver、不寫入，工作 `failed`（`PERMISSION_DENIED`／`DATA_SCOPE_DENIED`／`IMAGE_NOT_FOUND`／`PERMISSION_REVOKED`）。佇列訊息只有 `job_id`，資料庫不存 JWT。

### 5. 排程輸入版本與寫回（AC-16、AC-17）
- 建立時保存 `sv1:` 輸入版本（工單、途程、機台、行事曆各一個雜湊，`GET /production/overview` 也回 `input_version`；`POST /schedule/jobs` 可帶 `input_version`，過期 409）。開始求解與寫回各比對，不同＝`SCHEDULE_INPUT_CHANGED`，舊解不寫回、不覆蓋新工單。
- 寫回在生產資料庫的單一 `BEGIN IMMEDIATE` 交易裡先比對 revision；revision 變了但輸入雜湊沒變（例如盤點異動）才用新 revision 重試。`run_id` 由 `job_id` 決定，重送、重試、舊 worker 延遲寫回都是同一列（`INSERT` 前先查，不覆蓋）。
- 寫回前驗證：硬限制（後端用同一套規則重算）與分數和 Timefold 一致；不通過＝`SCHEDULE_INVALID_SOLUTION`／`SCHEDULE_SCORE_MISMATCH`，不寫回。舊的 SSE 入口也套用這道驗證。
- 工廠資料庫（Text-to-SQL 查的衍生檢視）在寫回後重建；中途中斷時下一次讀取依 revision 自動重建（`ensure_built`），不需要跨資料庫交易。生產資料（工單、排程結果、異動、核准、稽核）仍是 SQLite——**全面搬到 PostgreSQL 留待之後獨立執行**，這次只提供 repository 邊界與同步策略，沒有改寫或重置任何正式資料。
- 異動與核准（`change_service`）語意不變：角色 → 範圍 → 欄位 → 硬上限 → 試算 → 額度 → 確認／核准 → 寫前重驗 → 交易 → 讀回／稽核；確認卡 15 分鐘、核准 24 小時、禁止自核、資料指紋衝突、硬上限不能被核准放寬。新增 `Idempotency-Key`（開／取消工單、確認異動、送核准、核准；記錄存在生產資料庫，跨行程共用），並且在寫入鎖內再確認確認卡／待核准單還在，同一張單同時送兩次只會寫一次。
- **行程中斷也不重複寫入**：業務寫入（開／取消工單、寫入異動、送核准、核准）都在生產資料庫的**單一 SQLite 交易**裡完成，並且在同一個交易把 `idempotency` 記錄從 `running` 標成 `applied`（記下單號，以及重送時重建回應要用的資料；核准開立／取消工單時，待核准單的狀態更新也在這個交易內）。所以「寫入已提交、冪等回應還沒記錄」這個窗口只會留下 `applied`，不會留下「沒有標記的已寫入」。同一個 key 重送時：`done` 回原本的回應；`running`（業務寫入還沒提交）逾時 120 秒後才重新執行；`applied` 絕不再執行寫入，改呼叫各端點的 `resume` 補做**可重做的收尾**（重建工廠資料庫、讀回、補稽核——稽核用「動作＋操作＋單號」去重）並回覆原本的結果，標頭 `Idempotent-Replayed: true`。收尾出錯（不是行程當掉）時 key 立刻標成可接手，不必等 120 秒；還在 120 秒內的 `applied` 回 409 `IDEMPOTENCY_IN_PROGRESS`。遲到的第一個執行者（key 已被接手）的寫入點會因標記不是 `running` 而整個回滾（`IdempotencyLost` → 409）。確認卡本身仍在 API 行程記憶體：行程重啟後卡片沒了，但帶同一個 key 重送仍會從標記拿到原本的結果。測試：`test_idempotency.py` 的行程中斷組（五個端點各一個）。

### 6. 多行程資源協調
| 資源 | 策略 |
|---|---|
| 排程範圍／活動工作 | PostgreSQL：部分唯一索引＋資源租約（取代行程內的 `_busy` 與 `_active` 當唯一依據；API 行程內的 `_active` 只剩舊入口的 `stop()`） |
| GPU 重工作 | 資源租約 `gpu:0…`，`JOBS_GPU_SLOTS`（預設 1）跨所有 worker 行程；排程與 GPU 工作分佇列、分租約 |
| 使用中模型 | `memory_guard` 的引用計數照舊，另外把「使用中的模型」以短效 key 登記在 Redis（每個行程一個，心跳續約）；`busy()` 是本行程與其他行程的聯集，所以 API 行程判斷記憶體吃緊也不會卸載 worker 正在用的 Ortho2CAD，反之亦然。Redis 不通時退回只看本行程 |
| 交接票 | 見上：Redis 共用；失效就重跑第 2 段，不放寬任何關卡 |
| 確認卡 | 仍在 API 行程記憶體（15 分鐘）；多個 API 行程時別的行程找不到＝要求重新輸入，不會重複寫入 |

### 7. 前端（AC-03）
- `src/api/` 依功能拆模組（`artworks`／`drawings`／`inventory`／`cad`／`schedule`／`jobs`／`common`），底層共用 `http.ts`（憑證、401 自動重新取得、統一錯誤、`Idempotency-Key`）與 `sse.ts`（含事件串流），`client.ts` 是統一入口。測試保證：頁面與元件沒有直接 `fetch`、沒有任何模型／資料庫／Redis／solver 位址。
- 3D 重建與排程：建立工作 → 訂閱事件（斷線用 `Last-Event-ID` 重連，連不上改輪詢）→ 取結果。狀態新增「排隊中」「已取消」，失敗與取消可重試；排程有「提前結束」與「取消（不寫回）」兩個按鈕。離開頁面、切換身分、元件卸載只停止訂閱；排程頁／3D 重建頁重新整理時接回進行中的工作（`overview.active_job`、`GET /jobs?active=true`），不重複建立。
- 切換身分：訂閱中止、`Idempotency-Key` 清掉、身分相關的查詢快取鍵帶身分 id（舊身分晚到的回應只寫進舊身分的鍵）、上傳照片的辨識／色彩／對位不再視為身分無關。

## 與舊端點的相容對照（AC-02）
| 舊端點 | 狀態 | 差異 |
|---|---|---|
| `POST /cad/reconstruct`（SSE） | 保留。`LEGACY_SYNC_JOBS=auto`：啟用工作系統時關閉（409 `USE_JOBS_API`），沒啟用時（單機開發）照舊 | 在 API 行程執行、連線中斷就停止；`token` 事件帶模型產生的程式碼；補上存取紀錄（`access.json`）與上傳照片擁有者檢查 |
| `GET /cad/jobs/{id}`、`/cad/jobs/{id}/{name}` | 保留（結果相容讀取） | 授權改依存取紀錄；沒權限回 404（之前看不到的圖紙是 403） |
| `POST /schedule/solve`（SSE） | 同上 | 求解前驗硬限制與分數、寫回用輸入版本與冪等 `run_id`；和背景工作共用排程範圍租約 |
| `POST /schedule/stop` | 保留，語意不變（提前結束採用最佳解） | **要 `schedule_run`**（修補）；可帶 `job_id`；同時作用在背景工作與舊入口 |
| `GET /schedule/runs/{id}`、`/production/*` | 保留 | `overview` 新增 `input_version`、`active_job`；id 路徑參數加格式驗證 |
| `POST /images`、`GET /images/{id}`… | 保留 | 記錄擁有者；非擁有者／未被授權 404；新增 `PUT /images/{id}/acl` |
| `POST /changes/*`、`/approvals/*`、`/production/work-orders` | 保留 | 可帶 `Idempotency-Key`；同一張單同時送兩次只寫一次 |
| `POST /inventory/ask` | 保留 | 先過共用輸入防護（403 `INPUT_BLOCKED`） |
| 新增 | `POST /cad/jobs`、`POST /schedule/jobs`（202）、`GET /jobs`、`GET /jobs/{id}`、`/events`、`POST /jobs/{id}/cancel`、`GET /jobs/{id}/artifacts/{name}` | 見 `shared/sse_events.md`、`shared/error_codes.md`、`shared/openapi.json` |

## 部署與回復
- 本機開發／單機部署：`make jobs-up`（Docker：PostgreSQL＋Redis，只綁 127.0.0.1）→ `make jobs-migrate` → `make worker-gpu`、`make worker-schedule`（各一個終端機；worker 需要 CadQuery、Ortho2CAD／Timefold 的連線，跟 API 一樣在本機執行）→ `make demo`。`.env` 設 `REDIS_URL` 與 `DATABASE_URL`（或 `JOBS_DATABASE_URL`）；兩個都有才啟用背景工作，沒有就是原本的單機模式。
- 健康：`GET /status` 的 `jobs`（啟用、資料庫、broker、待投遞數、各狀態工作數）；`python -m app.jobs.admin status`。
- 保存期限：`JOBS_EVENT_RETENTION_DAYS`（預設 7 天，與上傳照片一致），啟動時清理結束的工作與它們的產物。
- **回復**：`python -m app.jobs.admin rollback --steps 1` 只刪背景工作系統自己的資料表（先 `pg_dump -t jobs -t job_events -t job_outbox -t job_artifacts -t resource_leases`）；關掉背景工作只要清空 `REDIS_URL`（舊入口自動重新開啟），不會碰上傳、紀錄、知識庫、庫存與生產資料庫。程式回復到 `71d707f` 時，`uploads` 多出的 `owner_id`／`acl` 欄位無害（舊程式不讀）。
- **資料遷移**：舊上傳（沒有擁有者）→ `adopt-uploads`；舊 3D 重建資料夾 → `adopt-cad`。兩者都是管理者手動、可重複執行，預設不做。

### 多行程部署的注意事項
- API 與 worker 要看到**同一個 `DATA_DIR`**（同一台主機，或共用磁碟）：3D 重建產物由 worker 發布、API 提供下載；生產 SQLite、庫存 SQLite、上傳照片也在那裡。這次沒有做跨主機的物件儲存。
- 開多個 API 行程（`uvicorn --workers N`）時 `JWT_SECRET` 要填同一組（否則各行程的憑證互相不認，這是原本就有的限制）；每個 API 與 worker 行程都內建維護執行緒（outbox 投遞、租約恢復、補投遞），用 `SKIP LOCKED` 互相避讓，不需要另外的排程器。
- SQLite 連線逾時調成 30 秒（API 與 worker 兩個行程寫同一個檔案時互相等候）；庫存資料庫重建的暫存檔每次獨立，兩個行程同時重建不會互相覆蓋。
- `GET /status` 的 `jobs` 與管理頁的「背景工作」卡片：PostgreSQL、Redis、在線 worker（行程自己定期登記的短效 key）、待投遞數、各狀態工作數。沒有在線的 worker 時前端排隊畫面會提示。
- 稽核：建立、取消、提前結束與排程結果寫回都記一筆 `audit_log`（誰、哪個工作或結果、request_id；不記參數與模型輸出）。

## 隱私與外送
原圖、工廠內部／機密段落、SQL schema、結果與 CAD 輸入全部留地端。Jev 維持既有的代號化／公開內容邊界與外送量紀錄（啟用 Jev 時**不是**零外送）；背景工作系統不增加任何外送：worker 不連 Jev、不連雲端，進度與錯誤事件不含未檢查的 prompt、模型程式碼、帳密或敏感原文。

## 沒有做（刻意）
- 公司 SSO／IdP：授權來源仍是 `access.yaml`＋展示用 JWT；`SecurityContext.for_account()` 與 `identity` 是預留的介面，沒有宣稱展示登入就是正式 SSO。
- 生產 SQLite、庫存的全面 PostgreSQL 遷移與正式資料搬遷。
- 從 token 中段續跑；LLM、Ortho2CAD、Timefold 的品質或速度調整；辨識門檻、孔位演算法、排程限制、提示詞。
- 上傳擁有者的粒度是帳號 id（展示環境只有一個訪客帳號，所有訪客共用）；有個別使用者身分之後擁有者自然細到個人。
- 確認卡仍是行程內記憶體（見上表）。

## 驗證
`make ci`（lint、型別、知識庫檢查、後端單元＋整合測試、前端測試與建置、OpenAPI 同步）。背景工作的整合測試用自己建立的 PostgreSQL 17 與 Redis、獨立的 `celery worker` 子行程與假的 Timefold 服務，涵蓋端到端 3D 重建與排程、`kill -9` 後恢復並重試、Redis 中斷與訊息遺失、資料庫中斷、重複投遞只執行一次、兩個 worker 的 GPU 名額與排程範圍互斥、取消／提前停止、跨行程撤權。細節與結果見 Implementation Report。
