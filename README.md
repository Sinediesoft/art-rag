# 地端隱私多模態 RAG 專題 — 畫作導覽助理＋工廠機械加工圖助理（Demo）

拍下或上傳一幅畫 → 系統辨識是哪幅畫 → 依畫作知識庫以繁體中文回答，每句附出處。
**同一套架構換一個知識庫**：拍下一張工廠加工圖 → 辨識是哪張圖紙 → 用 **Ortho2CAD** 把三視圖還原成
可編輯的 CadQuery 程式碼與 3D 模型 → 依製程與檢驗規範回答問題。圖紙屬企業機密，全程不出本機。
架構依《地端隱私多模態 RAG 專題開發企劃書》，主打**地端部署＋資料隱私**：**混合式為主**（本地 JSON 知識庫＋
本地向量檢索＋本地 VLM，照片與知識全程不離開主機）、**雲端 API 只當對照組**（A1 無檢索、A2 有檢索，
預設關閉）、**LoRA 保留插槽**。各策略共用同一個檢索層與 prompt，只換生成端。

```
照片 ─► 前處理（EXIF 轉正、1024px）─► Chinese-CLIP 粗篩 ─► ORB 幾何驗證 ─► 辨識結果／「知識庫中沒有這幅畫」
問題 ─► bge-m3 ─► 只取該畫作段落（門檻＋最多 5 段；比較／背景題才從全庫補足）─► 共用 prompt（answer_v1）
      ─► strategy：hybrid（Ollama Qwen3-VL）｜lora（選做）｜api_nokb／api_kb（雲端對照組）─► OpenCC ─► SSE
            └─ 主推論伺服器連不上 → 改走本地備援模型，畫面標註「本地備援模型」；本地都失敗就暫停服務，不改走雲端
   本地生成端只准連本機／內網位址；每次回應記錄外送資料量（本地恆為 0）

工廠圖紙 ─► CLIP 粗篩 ─► ORB 幾何驗證（只看三視圖區）─► 拉正後比對線條重合度 ─► 辨識結果／「知識庫中沒有這張圖紙」
         ─► 裁掉標題欄、縮到 224×224 ─► Ortho2CAD（llama-server，Qwen3-VL-8B 微調）─► CadQuery 程式碼（串流）
         ─► AST 白名單＋受限子行程＋sandbox-exec ─► 依標註尺寸等比縮放 ─► STL／STEP／回投影三視圖／IoU
         └─ 未收錄圖紙：本地 Qwen3-VL 讀圖上尺寸；製程問答走同一條 RAG（drawing_v1），雲端一律拒絕
```

## 快速開始（macOS／WSL2）

需要：[uv](https://docs.astral.sh/uv/)、Node.js 24、[Ollama](https://ollama.com)（沒有也能跑，見下方 mock 模式）

```bash
make setup      # 安裝套件、建立 .env、下載 qwen3-vl:4b-instruct
make index      # 驗證知識庫並建索引（第一次會下載 Chinese-CLIP 約 750 MB、bge-m3 約 2.2 GB）
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
│   ├── rag/           C  embedders（Chinese-CLIP、bge-m3）、verify（ORB＋線條重合）、prompt、providers、textproc
│   └── cad/           C  drawing（三視圖產生器）、sandbox／runner（CadQuery 沙箱）、metrics（IoU）、preprocess
├── pipelines/         C  build_index.py（make index）、bump_version.py、make_drawings.py、setup_ortho2cad.py
├── kb/                D  畫作（artworks/、images/）＋工廠圖紙（parts/、cad/、drawings/）、VERSION；kb_staging/ 放展示用資料
├── eval/              D  qa.jsonl、photos/、drawing_photos/、run_eval.py、run_cad_eval.py、runs/
├── models/               make ortho2cad-setup 下載的 Ortho2CAD（不進 Git）
├── shared/            共用層：openapi.json、schemas/、prompts/、models.yaml、error_codes.md、sse_events.md
├── docs/adr/          技術決策紀錄
└── .github/workflows/ CI：知識庫、lint、型別、單元測試、openapi 同步、前端建置
```

## 與企劃書的差異（demo 階段）

| 項目 | 企劃書 | 目前 demo | 後續 |
|---|---|---|---|
| 資料庫 | PostgreSQL + pgvector | `data/index/` 檔案索引＋SQLite（`repositories/` 封裝） | B 換 pgvector，見 ADR 001 |
| 部署 | Docker Compose + Nginx + Tailscale Funnel | 後端直接提供前端建置檔 | B 補 `deploy/` |
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
