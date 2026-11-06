# ADR 048：工廠資料全部改用 PostgreSQL（生產資料＋庫存 Text-to-SQL，比照 ADR 004 的正式版）

- 日期：2026-11-06
- 狀態：採用（分支 `工廠PostgreSQL`）；取代 ADR 009 決定 3 的「production.sqlite3、inventory.sqlite3 不搬，照舊是 SQLite」，
  實現 ADR 004「影響」寫的正式版（庫存放在獨立 schema，Text-to-SQL 用只有 SELECT 權限的資料庫角色＋`statement_timeout`）
- 編號：`dev` 最大是 047，接著用 048

## 背景
工廠的營運資料一直留在兩個 SQLite 檔：
- `data/production.sqlite3`（可寫入）：工單、排程結果、智慧助理的異動、待核准單、稽核、冪等標記
- `data/inventory.sqlite3`（唯讀）：Text-to-SQL 查的庫存資料庫，由知識庫 JSON 加上 production 的異動重建

留在 SQLite 是階段性的取捨（Q-2026-11-06-02）：Text-to-SQL 的三道防護靠 SQLite 的 `mode=ro`、authorizer、progress handler；
沒有 Docker 的電腦也要能用。代價是 ADR 041 那些繞法：生產資料和背景工作資料庫沒有共同交易、
API 與 worker 兩個行程搶同一個檔案（連線逾時調成 30 秒）、兩邊要看到同一個 `DATA_DIR`。
使用者決定「工廠的資料全部換成 PostgreSQL，比照正式版，不要用 SQLite」。

## 決定
1. **三個 schema，同一個資料庫**：`FACTORY_DATABASE_URL`（留空沿用 `DATABASE_URL`，和 `JOBS_DATABASE_URL` 同一種做法）。
   - `production`：可寫入的生產資料，資料表和 SQLite 版相同；稽核多一欄 `audit_id`（唯一索引，取代 `json_extract` 去重）
   - `inventory`：Text-to-SQL 查的庫存資料庫。任一來源變動時，在**一個交易裡** `DROP SCHEMA inventory CASCADE` 後重建
     （等同 SQLite 版「寫到暫存檔再原子換上」：查詢看到的是舊的或新的，不會看到一半）
   - `factory`：系統自己的中繼資料（庫存資料庫的建庫狀態、唯讀角色的密碼），`REVOKE ALL FROM PUBLIC`
2. **不保留 SQLite 版**：兩個 URL 都空時工廠功能停用，回 503 `FACTORY_DB_UNAVAILABLE`（新錯誤碼），不退回 SQLite。
   切換身分、登入、狀態頁照常（待核准數當 0、庫存 `ok: false`）
3. **寫入鎖**：每個寫入交易先拿 `pg_advisory_xact_lock`（`factory_db.PRODUCTION_WRITE`），取代 SQLite 的 `BEGIN IMMEDIATE`：
   比對資料版本、冪等標記、去重的稽核都不會被另一個行程插隊；`committed_run()` 在同一把鎖上等寫入中的交易
   （恢復迴圈的「已寫入了嗎」不會先回答「沒有」）。重建庫存另一把鎖（`INVENTORY_BUILD`）
4. **Text-to-SQL 的唯讀角色（取代 authorizer）**：
   - 每個機密等級組合一個 LOGIN 角色（`artrag_sql_<資料庫>_<組合代號>`，NOSUPERUSER／NOCREATEDB／NOCREATEROLE／NOINHERIT）
     ＋一個專屬 schema `inventory_ro_<組合代號>`，裡面只有和 inventory 同名的檢視表：
     零件依機密等級過濾（`security_barrier`），庫存、異動、工單、訂單跟著零件過濾，排程跟著工單過濾——
     和 SQLite 版「刪掉看不到的零件與相關資料的記憶體複本」結果相同。`v_part_stock`、`v_wo_plan` 建在過濾後的檢視表上
   - 角色只拿得到自己 schema 的 USAGE 與這些檢視表的 SELECT；`inventory`（原始資料表）、`production`、`factory`
     一律 permission denied。角色層級預設 `search_path`＝自己的 schema、`default_transaction_read_only`＝on
   - 每次查詢一條新連線，連線參數再帶 `statement_timeout`（`exec_timeout_ms`）與唯讀交易；結果列數上限照舊
   - 密碼由系統產生（`secrets.token_urlsafe`），存在 `factory.sql_readers`；角色存在但沒有密碼紀錄時重設
   - 建庫時先準備「不限」與 `access.yaml` 各角色的組合；之後才出現的組合，第一次查詢時補建
   - 主帳號要能建立角色（Docker 版、離線包的 `artrag` 都是超級使用者）
5. **靜態檢查補 PostgreSQL**（`text2sql.check_sql`）：寫入與管理指令加上 TRUNCATE、COPY、GRANT、SET、DO、CALL…；
   另外擋系統目錄與設定函式（`pg_*`、`information_schema`、`current_setting`、`set_config`、`version`…）。
   唯讀角色讀不到別的資料，但 `pg_class` 這類目錄對所有角色開放、看得到其他 schema 的名稱，所以先在靜態檢查擋
6. **試算改成「一定回滾的交易」**（`inventory_repo.scratch()`）：取代「複製到記憶體、套用、丟掉」；
   `data_changes` 改用 `inventory_repo.Db`（`rows`／`one`／`exec`），影響列數由 `exec()` 累計，取代 SQLite 的 `total_changes`
7. **欄位型別**：日期時間照舊是 `'YYYY-MM-DD'` 文字（排序、比較的結果和 SQLite 版相同，模型的範例不用改）；
   `std_cost_twd` 改 NUMERIC（PostgreSQL 的 `ROUND(x, n)` 只接受 numeric），讀出時轉 float；不建外鍵
   （SQLite 版本來就沒強制，一致性由知識庫驗證負責；`REFERENCES` 只留在給模型看的 schema）
8. **prompt `sql_v4`**：sql_v3 加第 9 條 PostgreSQL 寫法（非彙總欄位要進 GROUP BY、英文代碼用 ILIKE、算天數先轉 date）。
   11 個範例和 v3 相同（都能直接在 PostgreSQL 執行）。檢視表的 GROUP BY 也補齊所有非彙總欄位
   （唯讀角色查的是檢視表，PostgreSQL 不會從檢視表推得主鍵）
9. **舊資料**：`pipelines/import_factory_sqlite.py` 把 `data/production.sqlite3` 搬進 production schema
   （只搬到空的 schema，`--replace` 才覆蓋）；`inventory.sqlite3` 不用搬，後端會重建
10. **測試**：和背景工作的測試一樣，在暫存資料夾 `initdb` 一個隔離的 PostgreSQL（`tests/conftest.py` 的 `_factory_db`）；
    找不到 initdb 時用 `TEST_DATABASE_URL`。`jobs_support.find_bin` 多找 Windows 的 `C:\Program Files\PostgreSQL\*\bin`；
    `PgServer.start` 不再接 pg_ctl 的輸出（Windows 上 postgres 會繼承管線，`capture_output` 會一直等）

## 考慮過但沒做
- **保留 SQLite 版、用 `DATABASE_URL` 切換**（像使用紀錄的 `logs_repo`／`pg_logs_repo`）：沒有 Docker 的電腦（Mac 組員）不受影響，
  但工廠的 SQL 要維護兩份、兩邊都要測，安全防護也是兩套。使用者決定不保留（Q-2026-11-06-12）
- **`SET ROLE` 切到唯讀角色**：模型的 SQL 可以用 `set_config('role', …)`／`RESET ROLE` 切回連線帳號，不是安全邊界；改用獨立登入的角色
- **row-level security 依連線變數過濾機密等級**：變數可以被模型的 SQL 用 `set_config` 改掉；改用每個組合一個角色＋只授權檢視表
- **撤銷 `pg_sleep` 等內建函式的 PUBLIC 執行權**：會動到同一個資料庫的其他角色；逾時已經由 `statement_timeout` 限制
- **日期欄位改成 DATE**：API 回傳、比較的程式都假設是字串，模型範例也要改；先維持文字，算天數時由模型轉型
- **生產資料和背景工作放同一個交易**（ADR 041 的繞法）：兩者可能在不同資料庫（`JOBS_DATABASE_URL`），先維持「排程已提交是事實來源、結案可重做」

## 影響
- 沒有 PostgreSQL 的電腦（一鍵安裝時沒有 Docker）：工廠功能（工單、排程、修改資料、核准、庫存查詢）停用；畫作、圖紙問答照常。
  離線包（ADR 046）的 WSL 環境本來就有 PostgreSQL，不受影響
- `.env`：新增 `FACTORY_DATABASE_URL`（通常留空）；`make inventory`、`make demo-reset` 要連得到資料庫
- 錯誤碼：新增 `FACTORY_DB_UNAVAILABLE`（503）
- Text-to-SQL：prompt 改 `sql_v4`，SQL 語法是 PostgreSQL；**還沒用真模型重跑 `make eval-sql`**（這台沒有跑本地模型），
  答對率要在有模型的電腦上重測再補進這份 ADR
- 舊的 `data/production.sqlite3`、`data/inventory.sqlite3`、`inventory.manifest.json` 不再使用；搬完資料後可以刪
