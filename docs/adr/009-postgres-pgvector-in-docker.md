# ADR 009：資料庫改用 PostgreSQL 17 + pgvector，跑在 Docker；檔案模式保留給沒有 Docker 的電腦

- 日期：2026-10-01
- 狀態：採用（取代 ADR 001 的 demo 暫行做法；檔案模式保留）

## 背景
企劃書 §六、§七：PostgreSQL 17 + pgvector 一個服務同時管畫作資料與向量；
開發時每位組員本機用 Docker 跑 PostgreSQL，後端與前端在本機熱重載；Ollama 原生安裝在主機上，其餘服務進 Docker。
demo 期間以檔案索引＋SQLite 暫代（ADR 001），存取已封裝在 `backend/app/repositories/`。

## 決定
1. **Docker**：`deploy/docker-compose.yml` 目前只有 `db` 服務——`pgvector/pgvector:0.8.6-pg17`（版本固定）、
   只綁 `127.0.0.1`、資料放 named volume（`artrag_pgdata`）、`mem_limit: 512m`。Nginx 與後端容器之後加在同一份檔案。
2. **開關**：`.env` 的 `DATABASE_URL` 有值 → PostgreSQL；留空 → 檔案索引＋SQLite。
3. **資料表**照企劃書 §六：`artworks`／`chunks`、`parts`／`part_chunks`、`index_manifest`，
   以及使用紀錄 `uploads`、`chat_logs`、`cad_logs`、`feedback`。
   - 每筆完整內容存 `doc JSONB`；企劃書列的主要欄位（標題、畫家、授權、料號…）是由 `doc` 產生的欄位
     （`GENERATED ... STORED`），直接下 SQL 查得到，又不會跟 `doc` 對不上。程式一律讀 `doc`。
   - 向量 `vector(512)`（Chinese-CLIP）、`vector(1024)`（bge-m3），HNSW 索引（`vector_cosine_ops`）。
   - 使用紀錄欄位與 SQLite 版相同，只有時間改 `TIMESTAMPTZ`；查詢語句兩邊共用。
4. **寫入**：`make index` 照舊先建 `data/index/`（縮圖、標準模型的 STL／STEP 仍是檔案），
   再在**同一個交易**裡刪表、重建、寫入；寫不進資料庫就不換上新目錄，兩邊維持同一版。
   `build_index.py --db-only`（`make index-db`）把現有的 `data/index/` 寫進資料庫、不重算向量。
5. **讀取**：後端啟動時在同一個快照（REPEATABLE READ）讀 manifest 與全部資料進記憶體；
   以文搜圖（RRF）與領域路由（原型）照舊在記憶體算，以圖搜圖、圖紙辨識、問答段落檢索交給
   pgvector 的 `ORDER BY <=>`。每個 `/api` 請求查一次 `index_manifest.published_at`，變了就重載——
   `make index` 後不必重啟後端，一致性檢查（模型版本、kb_version…）與檔案版相同。

## 考慮過但沒做
- **直接拿掉檔案模式**：這台 Windows 筆電與 Mac 備用機都還沒有 Docker，CI 大部分測試也不需要資料庫。
  保留的代價是兩套實作，由 `backend/tests/test_postgres.py` 確認兩邊結果一致。
- **所有查詢都改 SQL**（以文搜圖的 RRF、領域路由的原型平均）：要改 `services/`，違反「換資料庫只改 repositories/」；
  資料只有幾十筆，記憶體運算不到 1 ms。資料到數千筆以上再搬。
- **內積 `<#>`**：向量都已正規化，排序與 cosine 相同；照企劃書用 cosine。
- **資料放 bind mount（`./data/postgres`）**：Windows 與 macOS 的 bind mount 用在 PostgreSQL 資料目錄有權限與效能問題。
- **Alembic**：目前索引表每次整批重建、紀錄表用 `CREATE TABLE IF NOT EXISTS` 就夠；
  第一次需要改既有紀錄表欄位時再導入（企劃書 B 的工作）。

## 影響
- 程式只改了 `repositories/`、`pipelines/build_index.py` 與設定；`services/`、`rag/`、API 規格不變
  （`/health` 在資料庫連不上時不查最近紀錄，回 `db=false`）。
- 資料庫停掉時，後端請求會等連線池逾時（5 秒）才失敗；後端啟動時連不上就拒絕啟動並提示 `make db-up`。
- 資料量變大時：HNSW 是近似搜尋，加上 `WHERE`（只取某幅畫的段落）可能回不滿 k 筆，
  要設 pgvector 0.8 的 `hnsw.iterative_scan`；目前資料少，PostgreSQL 直接循序掃描，結果是精確的。
- 待補：每天 `pg_dump` 使用紀錄四張表（索引可由 `make index` 重建）；SQLite 裡既有的使用紀錄不搬。
