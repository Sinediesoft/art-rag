# 地端隱私多模態 RAG 專題 — 畫作導覽助理＋工廠機械加工圖助理（Demo）

拍下或上傳一幅畫 → 系統辨識是哪幅畫 → 依畫作知識庫以繁體中文回答，每句附出處。
**同一套架構換一個知識庫**：拍下一張工廠加工圖 → 辨識是哪張圖紙 → 用 **Ortho2CAD** 把三視圖還原成
可編輯的 CadQuery 程式碼與 3D 模型 → 依製程與檢驗規範回答問題。圖紙屬企業機密，全程不出本機。
架構依《地端隱私多模態 RAG 專題開發企劃書》，主打**地端部署＋資料隱私**：**混合式為主**（本地 JSON 知識庫＋
本地向量檢索＋本地 VLM，照片與知識全程不離開主機）、**雲端 API 只當對照組**（A1 無檢索、A2 有檢索，
預設關閉）、**LoRA 保留插槽**。各策略共用同一個檢索層與 prompt，只換生成端。

```
照片 ─► 領域路由（與畫作／圖紙原型比 CLIP 相似度，MMed-RAG 的領域辨識）─► 畫作走下一行、圖紙走「工廠圖紙」那行
照片 ─► 前處理（EXIF 轉正、1024px）─► Chinese-CLIP 粗篩 ─► ORB 幾何驗證 ─► 辨識結果／「知識庫中沒有這幅畫」
問題 ─► bge-m3 ─► 只取該畫作段落（門檻＋最多 5 段；比較／背景題才從全庫補足）
      ─►（選用，預設關）本地模型篩掉沒幫助的段落（MIRA 的 Rearrange）─► 共用 prompt（answer_v1）
      ─► strategy：hybrid（Ollama Qwen3-VL）｜lora（選做）｜api_nokb／api_kb（雲端對照組）─► OpenCC ─► SSE
            └─ 主推論伺服器連不上 → 改走本地備援模型，畫面標註「本地備援模型」；本地都失敗就暫停服務，不改走雲端
   本地生成端只准連本機／內網位址；每次回應記錄外送資料量（本地恆為 0）

工廠圖紙 ─► CLIP 粗篩 ─► ORB 幾何驗證（只看三視圖區）─► 拉正後比對線條重合度 ─► 辨識結果／「知識庫中沒有這張圖紙」
         ─► 裁掉標題欄、縮到 224×224 ─► Ortho2CAD（llama-server，Qwen3-VL-8B 微調）─► CadQuery 程式碼（串流）
         ─► AST 白名單＋受限子行程＋sandbox-exec ─► 依標註尺寸等比縮放 ─► STL／STEP／回投影三視圖／IoU
         └─ 未收錄圖紙：本地 Qwen3-VL 讀圖上尺寸；製程問答走同一條 RAG（drawing_v1），雲端一律拒絕
```

## 快速開始（macOS／WSL2）

需要：[uv](https://docs.astral.sh/uv/)、Node.js 24、[Docker](https://docs.docker.com/get-docker/)（資料庫；沒有也能跑，見下方「資料庫」）、
[Ollama](https://ollama.com)（沒有也能跑，見下方 mock 模式）

```bash
make setup      # 安裝套件、建立 .env、下載 qwen3-vl:4b-instruct
make db-up      # 啟動資料庫容器（PostgreSQL 17 + pgvector），等到可以連線才結束
make index      # 驗證知識庫、建索引並寫進資料庫（第一次會下載 Chinese-CLIP 約 750 MB、bge-m3 約 2.2 GB）
make demo       # 建置前端並啟動 http://localhost:8000
```

開發時用 `make dev`（後端 8000 熱重載＋前端 5173，Vite 把 `/api` 代理到後端）。

**工廠圖紙的 3D 重建（Ortho2CAD）**另需 llama.cpp 與約 6.2 GB 模型（Ollama 目前不能載入這個模型）：

```bash
brew install llama.cpp
make ortho2cad-setup   # 下載 Q4_K_M 語言模型（5.0 GB）＋視覺權重（1.15 GB），轉成 mmproj
make demo-all          # 同時啟動 Ortho2CAD（:8081）與展示伺服器（:8000）；或分兩個終端機 make ortho2cad / make demo
```

沒有 Ortho2CAD 時圖紙辨識與問答照常可用，3D 重建頁會提示啟動；`LLM_MODE=mock` 時回傳標準模型的程式碼。

**沒有 GPU／模型的電腦**：在 `.env` 設 `LLM_MODE=mock`（不呼叫生成模型）；
再加 `EMBED_MODE=mock` 連 embedding 也不下載（只能測流程，辨識結果無意義），改完要 `make index`。

### 資料庫（PostgreSQL 17 + pgvector，Docker）

照企劃書 §七：資料庫進 Docker（`deploy/docker-compose.yml`），Ollama 原生安裝在主機上，後端與前端在本機熱重載。
畫作、段落、向量、索引 manifest 與使用紀錄都存在資料庫；縮圖與標準模型的 STL／STEP 仍是 `data/index/` 下的檔案。
資料庫只綁 `127.0.0.1`，不對區網或公網開放；資料放在 Docker volume `artrag_pgdata`，容器刪掉重建資料還在。

| 指令 | 用途 |
|---|---|
| `make db-up` | 啟動資料庫，等到可以連線才結束 |
| `make index` | 建索引；`.env` 設了 `DATABASE_URL` 會在同一個交易裡寫進資料庫，執行中的後端自動換上 |
| `make index-db` | 不重算向量，把現有的 `data/index/` 寫進資料庫（剛裝好 Docker 時用） |
| `make db-import-sqlite` | 把 `data/artrag.sqlite3` 的舊紀錄（問答、上傳、3D 重建）搬進資料庫；重複執行不會重複寫入 |
| `make db-psql` | 進資料庫下 SQL，例如 `SELECT id, title_zh, license FROM artworks;` |
| `make db-stop` | 停止資料庫（資料保留） |

Windows 沒有 `make` 時，在專案根目錄用 PowerShell：

```powershell
docker compose -f deploy/docker-compose.yml --env-file .env up -d --wait db
cd backend; uv run python ..\pipelines\build_index.py --db-only
uv run python ..\pipelines\import_sqlite_logs.py
```

**沒有 Docker 的電腦**：`.env` 的 `DATABASE_URL` 留空，改用檔案索引（`data/index/`）＋SQLite（`data/artrag.sqlite3`），
功能相同（見 ADR 001、006）。兩種方式的檢索結果由 `backend/tests/test_postgres.py` 比對一致；
這個測試要 `TEST_DATABASE_URL` 指向一個可以清空的資料庫才會跑，CI 用 pgvector 容器跑。

### 組員：建立自己的資料庫

企劃書 §六：**不互傳資料庫**，每個人用同一個指令從 `kb/` 的 JSON 重建，manifest 一致才算通過。
每人一個資料庫，誰重建索引都不會影響別人。在 `make setup` 之後：

1. **安裝 Docker Desktop**。
   - macOS：直接安裝。
   - Windows 照《部署說明》在 WSL2 的 Ubuntu 裡開發：裝好後到 Docker Desktop 的
     Settings → Resources → WSL integration 打開 Ubuntu，Ubuntu 裡才有 `docker` 指令。
2. **確認 `.env` 有 `DATABASE_URL`**。`make setup` 由 `.env.example` 複製，預設已填好。
   舊的 `.env` 不會被覆蓋，要自己從 `.env.example` 補上「資料庫」那一段。
3. **`make db-up`，再 `make index`**。
4. （選用）**`make db-import-sqlite`**：部署包附的預跑 3D 重建結果記在 `data/artrag.sqlite3`，
   搬進資料庫後，圖紙頁的「最近的 3D 重建」才看得到（結果和上傳照片一樣只保留 7 天）。
5. **確認建好**：開「系統狀態」頁，「索引一致」要打勾，知識庫版本要和其他人相同（例如 `2026.09.6`）。
   模型版本鎖在 `shared/models.yaml`，embedding 一律用 CPU 算，所以各自建出來的內容相同。

**直接在 Windows 執行（不在 WSL 裡）時**：`make index` 會在工廠圖紙的標準模型那步失敗
（`backend/app/cad/runner.py` 的 `import resource`，Windows 沒有這個模組）。
修好之前，改用部署包附的 `data/index/`（已建好的索引）寫進資料庫：
`cd backend; uv run python ..\pipelines\build_index.py --db-only`。
不要改用資料庫匯出檔：縮圖與 STL／STEP 不在資料庫裡，而且 `--db-only` 會先檢查知識庫與模型版本是否一致。

**要把使用紀錄給別人**（例如問答紀錄給 D 做評估）：只匯出紀錄表，對方還原到另一個資料庫，不會蓋掉自己的紀錄。
紀錄裡有使用者的問題與回答，請私下傳，不要貼在群組。

```powershell
# 匯出：先寫在容器裡再複製出來（Windows PowerShell 用 > 轉存會把二進位檔改壞）
docker exec artrag-db-1 pg_dump -U artrag -d artrag -Fc -t chat_logs -t cad_logs -t feedback -f /tmp/artrag-logs.dump
docker cp artrag-db-1:/tmp/artrag-logs.dump ./artrag-logs.dump

# 對方還原到另一個資料庫
docker exec artrag-db-1 createdb -U artrag artrag_logs_from_teammate
docker cp ./artrag-logs.dump artrag-db-1:/tmp/
docker exec artrag-db-1 pg_restore -U artrag -d artrag_logs_from_teammate /tmp/artrag-logs.dump
```

- **`.env` 不要傳給別人**：裡面是個人設定，填了雲端對照組的話還有 API 金鑰。組員照 `.env.example` 建自己的。
- **不要讓組員直接連你的資料庫**：資料庫刻意只綁 `127.0.0.1`；共用一個的話，任何人重建索引都會改到所有人的資料。

## 展示腳本（約 8 分鐘）

| # | 展示項目 | 操作 | 對應驗收目標 |
|---|---|---|---|
| 1 | 以圖搜圖 | 首頁「拍照辨識」，用手機拍螢幕上的〈谿山行旅圖〉 | Top-1 ≥ 90% |
| 2 | 拒答 | 拍李唐〈萬壑松風圖〉（`eval/photos/unknown/unknown-05.jpg`）：CLIP 相似度 0.96 仍判定「知識庫中沒有這幅畫」 | 拒答率 ≥ 80% |
| 3 | 圖文問答 | 辨識成功 →「問問這幅畫」→ 點建議問題；點 [1] 標籤看出處；問「當年賣了多少錢？」看它說不知道 | 引用正確率、防幻覺 |
| 4 | 以文搜圖 | 首頁輸入「水邊草地上撐陽傘的人群」 | 中文以文搜圖 |
| 5 | 策略比較 | 「策略比較」頁選〈谿山行旅圖〉，問簽名在哪：開檢索答對、關檢索亂編；A1（雲端無檢索）、A2（雲端＋檢索）欄顯示「外送」標示（需 `ALLOW_CLOUD=true`，否則顯示未開啟） | 可替換、檢索增益、零外送對比 |
| 6 | 容錯 | 「系統狀態」按「模擬斷線」→ 回問答頁再問，回答標註「本地備援模型」；直接結束 Ollama 則顯示服務暫停，不改走雲端 | 5 秒內改走本地備援 |
| 6b | 斷網可用 | `.env` 設 `HF_OFFLINE=true` 後拔掉對外網路，以圖搜圖、問答、新增畫作照常運作；問答下方顯示「外送 0 · 全在本地」 | 零外送、斷網可用 |
| 7 | 擴充性 | 終端機執行 `make demo-add`，首頁重新整理變 5 幅；拍〈早春圖〉立刻辨識得出，**後端不必重啟** | 新增畫作不改程式 |
| 8 | 一致性保護 | 改 `kb/VERSION` 不重建索引，重啟後端 → 拒絕啟動並說明原因 | 一致性保護 |

### 工廠機械加工圖（約 6 分鐘）

| # | 展示項目 | 操作 | 重點 |
|---|---|---|---|
| 9 | 圖紙辨識 | 「工廠圖紙」→「拍攝圖紙」，拍螢幕上的〈立式軸承座〉（或上傳 `eval/drawing_photos/known/mfg-006__tilt.jpg`） | 對應點＋線條重合度雙重驗證 |
| 10 | 拒答 | 上傳 `eval/drawing_photos/unknown/unknown-02__tilt.jpg`（版面、標題欄都一樣的未收錄圖紙） | 「知識庫中沒有這張圖紙」 |
| 11 | Ortho2CAD 3D 重建 | 〈連接法蘭〉→「Ortho2CAD 3D 重建」：看 CadQuery 程式碼逐字產生 → 3D 模型、IoU、疊合比較、回投影三視圖、下載 STEP | 約 1 分鐘；外送 0 |
| 12 | 微調的效果 | 同一頁切換「Qwen3-VL 4B 未微調」 | 領域微調 vs 一般 VLM |
| 13 | 沒收錄也能建模 | 第 10 步的拒答頁按「沒收錄也能用 Ortho2CAD 重建 3D」 | Qwen3-VL 讀尺寸＋Ortho2CAD 建模 |
| 14 | 製程問答 | 「問問這張圖」→「有哪些公差要求？」→ 點 [n] 看內部文件出處；問「單價多少」看它說不知道 | 引用、防幻覺；機密圖紙不提供雲端生成端 |
| 15 | 擴充 | `make demo-add` 同時加入第 7 張圖紙〈治具定位板〉，重新整理就辨識得出 | 新增圖紙不改程式 |

3D 重建一次約 45–130 秒。展示前先跑一次 `make eval-cad`，現場可在圖紙頁「最近的 3D 重建」直接開啟結果，
再挑一張現場重跑。16 GB 記憶體同時載入兩個 VLM 很吃緊，展示時請關掉其他大型程式。

展示後 `make demo-reset` 還原成 3 幅畫、6 張圖紙。

## 新增一張工廠圖紙（不改程式）

1. 依 `shared/schemas/part.schema.json` 寫 `kb/parts/<id>.json`（料號、圖號、材料、機密等級、製程與檢驗段落與出處）
2. 標準模型寫在 `kb/cad/<id>.py`（CadQuery，單位 mm，最後把實體指定給變數 `solid`）
3. `make drawings` 產生 `kb/drawings/<id>.png`（Ortho2CAD 訓練時的三視圖格式＋標題欄）
4. 遞增 `kb/VERSION`，`make index`（會執行標準模型，算出外形尺寸與重量寫進段落）

## 新增一幅畫（不改程式）

1. 圖片縮到長邊 1024 px，存成 `kb/images/<id>.jpg`（ID 格式 `<來源代碼>-<編號>`，只用小寫 ASCII）
2. 依 `shared/schemas/artwork.schema.json` 寫 `kb/artworks/<id>.json`（必填授權與出處；授權只收 CC0、公有領域、CC BY 4.0）
3. 遞增 `kb/VERSION`（`uv run --project backend python pipelines/bump_version.py`）
4. `make index` — 執行中的後端會自動載入新索引

## 評估

```bash
make eval       # 需要後端在執行；結果存 eval/runs/，並顯示在「系統狀態」頁
```

2026-09-29 在 MacBook Air M5（Qwen3-VL 4B）上的結果：

| 指標 | 結果 | 目標 |
|---|---|---|
| 以圖搜圖 Top-1（15 張模擬實拍照） | 100% | ≥ 90% |
| 未收錄畫作拒答（15 張） | 100% | ≥ 80% |
| 問答正確率／引用正確率（混合式，16 題，自動代理指標） | 100%／100% | ≥ 80%／≥ 90% |
| 檢索增益（開 vs 關檢索） | +69 個百分點 | ≥ 15 |
| 混合式 P95 首字／完整回答 | 4.2 秒／6.0 秒 | ≤ 3／≤ 15 秒（5070 Ti） |

⚠️ 這些只驗證架構能運作：資料只有 3 筆、評估題由開發者撰寫、實拍照是由數位原圖加工的**模擬照**
（`eval/make_synthetic_photos.py`）。正式評估請 D 換成真實實拍照與兩人獨立評分。

### 領域路由（`make eval-router`）

需要後端在執行；約 1 分鐘。所有評估照片送 `/search/any`，先判斷是畫作還是工廠圖紙（見 ADR 004）。
2026-09-30 在 Windows 筆電（GTX 1650）上的結果：

| 指標 | 結果 |
|---|---|
| 路由正確率（30 張畫作＋65 張圖紙模擬照） | 100%，沒有一張落在不確定區 |
| margin（與圖紙原型相似度 − 與畫作原型相似度） | 畫作 ≤ −0.288、圖紙 ≥ +0.140，間距 0.428 |
| 端到端（路由＋辨識） | 94/95（唯一錯的是圖紙辨識本身誤收，與路由無關） |
| 延遲中位數（含辨識） | 567 ms |

### 檢索段落篩選（`make eval-rearrange`，預設關）

回答前先請本地模型挑出有幫助的段落，只用挑中的（MIRA 的 Rearrange，見 ADR 005）。
開關：請求的 `rearrange` ＞ `.env` 的 `REARRANGE` ＞ `shared/models.yaml` 的 `rearrange.enabled`（預設 false）。
2026-09-30 在 Windows 筆電（GTX 1650）上的開關對照（16 題）：

| | 問答／引用正確率 | 平均段數 | 首字 P95 | 總計中位數 |
|---|---|---|---|---|
| 篩選關 | 100%／100% | 4.31 | 20.8 秒 | 25.1 秒 |
| 篩選開 | 100%／100% | 1.25 | 23.6 秒 | 25.8 秒 |

### 工廠機械加工圖（`make eval-cad`）

需要後端與 Ortho2CAD 都在執行；約 20 分鐘。2026-09-30 在 MacBook Air M5 16 GB 上的結果：

| 指標 | Ortho2CAD | Qwen3-VL 4B 未微調（對照） |
|---|---|---|
| 圖紙辨識 Top-1（30 張模擬實拍照：6 張圖紙 × 5 種拍法） | 100% | — |
| 未收錄圖紙拒答（30 張：版面相同的 5 張未收錄圖紙＋治具定位板） | 100% | — |
| 程式碼可執行率（6 張知識庫圖紙） | 100% | 17% |
| 平均 IoU（論文評估法；論文在 DeepCAD 測試集為 0.79） | 0.54 | 0.07 |
| 平均外框對齊 IoU | 0.71 | 0.08 |
| 外送資料量 | 0 | 0 |

| 圖紙 | Ortho2CAD IoU | 外框對齊 | 耗時 | 未微調 IoU |
|---|---|---|---|---|
| L 型固定支架 | 0.28 | 0.81 | 147 秒 | ✗ 無法執行 |
| 連接法蘭 | 0.81 | 0.82 | 47 秒 | ✗ 無法執行 |
| 階梯傳動軸 | 0.29 | 0.33 | 73 秒 | ✗ 無法執行 |
| 步進馬達安裝板 | 0.85 | 0.92 | 197 秒 | ✗ 無法執行 |
| T 型槽螺帽 | 0.29 | 0.49 | 136 秒 | ✗ 無法執行 |
| 立式軸承座 | 0.70 | 0.89 | 129 秒 | 0.41 |

板件、法蘭、軸承座表現好；階梯軸（車削件）與 T 形截面較弱——Ortho2CAD 的訓練資料 DeepCAD 以草圖擠出件為主。
IoU 以體素計算、無效實體先以 ShapeFix 修復（見 ADR 003）。生成速度受記憶體影響：記憶體充足時約 25 token/s，
與 Qwen3-VL 同時載入、記憶體吃緊時約 15 token/s。

⚠️ 圖紙與製程文件是虛構的示範資料、照片是由數位原圖加工的模擬照；Ortho2CAD 權重是第三方版本，
數字不能直接與論文比較。

## 專案結構

```
art-rag/
├── frontend/          A  React + TypeScript + Vite + Tailwind（src/api 集中呼叫、SSE 只有一份解析）
├── backend/app/       B  FastAPI：api/ → services/ → rag/ + repositories/，core/ 放設定與錯誤碼
│   ├── rag/           C  embedders（Chinese-CLIP、bge-m3）、router（領域路由）、verify（ORB＋線條重合）、prompt、providers、textproc
│   └── cad/           C  drawing（三視圖產生器）、sandbox／runner（CadQuery 沙箱）、metrics（IoU）、preprocess
├── pipelines/         C  build_index.py（make index）、bump_version.py、make_drawings.py、setup_ortho2cad.py、import_sqlite_logs.py
├── kb/                D  畫作（artworks/、images/）＋工廠圖紙（parts/、cad/、drawings/）、VERSION；kb_staging/ 放展示用資料
├── eval/              D  qa.jsonl、photos/、drawing_photos/、run_eval.py、run_cad_eval.py、runs/
├── models/               make ortho2cad-setup 下載的 Ortho2CAD（不進 Git）
├── shared/            共用層：openapi.json、schemas/、prompts/、models.yaml、error_codes.md、sse_events.md
├── deploy/        B  docker-compose.yml（目前只有資料庫：PostgreSQL 17 + pgvector）
├── docs/adr/          技術決策紀錄
└── .github/workflows/ CI：知識庫、lint、型別、單元測試、openapi 同步、前端建置
```

## 與企劃書的差異（demo 階段）

| 項目 | 企劃書 | 目前 demo | 後續 |
|---|---|---|---|
| 資料庫 | PostgreSQL + pgvector | PostgreSQL 17 + pgvector 0.8.6 跑在 Docker（`.env` 設 `DATABASE_URL`）；留空時退回 `data/index/` 檔案索引＋SQLite。見 ADR 006 | B：Alembic、每日 `pg_dump` 使用紀錄 |
| 部署 | Docker Compose + Nginx + Tailscale Funnel | `deploy/docker-compose.yml` 目前只有資料庫；後端直接提供前端建置檔 | B 在同一份 Compose 補 Nginx、後端容器、Funnel |
| 以圖搜圖 | Chinese-CLIP 粗篩＋ORB 幾何驗證 | 相同 | 見 ADR 002；D 用真實實拍照校正 |
| 本地生成 | Qwen3-VL 8B（5070 Ti） | Qwen3-VL 4B（Mac 備用機設定） | 5070 Ti 在 `.env` 改 `HYBRID_MODEL` |
| 雲端 API | 只當對照組（A1／A2） | 介面已接好（OpenAI 相容），`ALLOW_CLOUD` 預設 false、**未設定金鑰** | 評估時在 `.env` 設 `ALLOW_CLOUD=true` 並填 `API_KEY`，跑 `make eval-cloud` |
| 零外送 | 後端容器封鎖對外連線 | 程式層保護：本地生成端只准連本機／內網位址、雲端預設關閉、每次回應記錄 egress | B 補 `deploy/` 時用 Docker network 封鎖對外連線 |
| 評估題型 | 知識庫獨有題、無答案題、干擾段落題，每題標註類型 | `qa.jsonl` 只有 1 題標為 `no_answer`，其餘為 `untyped` | D 補題並標註 `type`；干擾段落題需 C 加注入機制 |
| 故宮圖檔 | 故宮 Open Data | Wikimedia Commons 公有領域副本 | D 換成故宮 Open Data 並填 `source_id` |
| 畫作說明 | 從來源頁整理 | 依公開資料撰寫的摘要 | D 逐段對照來源頁校對 |
| 工廠圖紙 | （新增領域） | 虛構工廠「示範精密機械」的 6＋1 張圖紙與製程文件，圖紙由標準 CadQuery 模型自動產生 | 換成合作廠商授權的真實圖紙與實拍照 |
| Ortho2CAD 權重 | 論文作者版本 | 作者未公開權重，使用 Hugging Face 第三方版本（Q4_K_M，commit 固定） | 作者公開後替換並重跑 `make eval-cad` |
| Ortho2CAD 推論 | 與其他本地模型同在 Ollama | llama.cpp `llama-server`（Ollama 無法轉 Qwen3-VL safetensors） | Ollama 支援後可合併 |
