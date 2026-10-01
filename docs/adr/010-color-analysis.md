# ADR 010：畫作色彩分析（主色盤、冷暖、明度／彩度、色塊分布圖）

- 日期：2026-10-01
- 狀態：提議（實作完成後補上校正數字，改為「採用（demo）」）

## 背景
畫作區塊目前只有以圖搜圖、以文搜圖與問答；工廠區塊在辨識之後還有 Ortho2CAD 3D 重建、庫存 Text-to-SQL、
生產排程。工廠區塊不只是「搜尋」，是因為它有三樣東西：**專用模型或演算法產生結構化結果**（CadQuery → STL／STEP）、
**有指標能驗證**（IoU、可執行率）、**數字交給計算而不是交給生成模型**（文件走 RAG、數字走資料庫，ADR 004）。

畫作這邊最便宜、也最符合這三點的分析是色彩：純運算、同一張圖結果固定、可以寫單元測試，
未收錄的畫也能分析（對應「沒收錄也能用 Ortho2CAD 重建」）。現在問「這幅畫主要用了什麼顏色」，
知識庫段落沒有答案，模型只能看圖目測或拒答。

範圍（使用者在 2026-10-01 選定，見 `專案問題.md` Q-2026-10-01-04）：
- 畫作頁顯示知識庫畫作的分析；
- 上傳的照片也能分析，包含未收錄的畫；
- 問答可以引用分析結果並附 [n] 出處；
- 分析項目：主色盤＋占比、冷暖比例、明度／彩度分布、色塊分布圖。

不在範圍內：依色彩找畫（知識庫只有 3–5 幅，展示效果有限）、照片與原圖的色差顯示在畫面上（只放進評估）。

## 選項
1. **建索引時算好，結果寫成可檢索的段落**：照工廠圖紙的做法——`make index` 執行標準模型、
   把外形尺寸與重量寫進「基本資料」段落（`backend/app/rag/chunking.py` 的 `part_metadata_text`）。
   問答流程完全不用改；上傳的照片另開 API 即時計算。缺點：改參數要重建索引。
2. 全部即時計算，問答時偵測到顏色問題才把結果插進 prompt：不用重建索引，
   但問答要多一條「是不是在問顏色」的判斷路徑（關鍵字容易漏），prompt 組裝流程也要改。
3. 讓 Qwen3-VL 看圖描述顏色：幾乎不用寫程式，但結果無法驗證、每次可能不同，正好違反防幻覺的主軸。

## 決定
採用 1。

### 流程
```
make index ─► 每幅畫：原圖 ─► 色彩分析 ─► colors 欄位（data/index/artworks.json；PostgreSQL 的 artworks.doc）
                                       ├► 色塊分布圖 data/index/colormaps/<id>.png
                                       └► 段落「色彩分析」（chunk_id <id>#color）─► bge-m3 向量
畫作頁 ─► GET /artworks/{id}/colors ─► 色彩分析卡（source = original）
上傳照片 ─► GET /images/{image_id}/colors ─► 即時計算 ─► 同一張卡（source = photo，標示受光線影響）
問答 ─► 現有檢索取到 <id>#color ─► 回答附 [n] ─► 出處「系統計算：色彩分析（數位圖檔）」
```
`artworks.doc` 是 JSONB，`colors` 跟工廠的 `geometry` 一樣放在畫作資料裡，不改資料表。

### 計算（`backend/app/analysis/color.py`，只用 numpy）
- **前處理**：沿用 `load_image`（EXIF 轉正、長邊 1024px），分群用長邊 256px（約 6 萬像素）。
- **色彩空間**：numpy 自己實作 sRGB → CIELAB（D65）。不用 OpenCV 的 Lab 轉換：它對浮點輸入是否做 gamma 校正，
  文件寫得不清楚；自己寫可以對已知值測試（純紅 (255,0,0) → L\* 53.24、a\* 80.09、b\* 67.20）。
- **主色盤**：Lab 空間 k-means，k = 6，k-means++ 初始化、固定亂數種子、最多 30 次迭代。
  再把長邊 512px 的圖逐像素歸到最近的主色，用這個結果算占比、畫色塊分布圖——畫面上看到的面積和數字一致。
  色盤依占比排序，每色附色碼、Lab、占比、中文色名、冷暖、明暗。
- **中文色名**：約 24 個基本色名的對照表（黑、深灰、灰、淺灰、白、米白、米黃、土黃、褐、深褐、暗紅、紅、粉紅、
  橙、黃、黃綠、綠、深綠、灰綠、青、天藍、藍、深藍、紫），以 CIEDE2000 色差取最接近者；CIEDE2000 以
  Sharma 等人的標準測試資料驗證。**不用**赭石、花青、石青等傳統顏料名：從圖檔的顏色推不出用了哪種顏料。
- **冷暖**（逐像素，不是逐主色）：Lab 換成彩度 C\* 與色相角 h。C\* < 10 為中性色（接近黑白灰）；
  h 落在 340°–110°（跨 0°：紫紅、紅、橙、黃）為暖色；其餘有彩色（黃綠、綠、青、藍、紫）為冷色。
  紫、紫紅一帶本來就有爭議，邊界放在 `models.yaml`。
- **明度／彩度**：明度分暗調 L\* < 35、中間調 35–70、亮調 ≥ 70，另算平均、P5–P95 明暗範圍、10 格長條圖；
  彩度分低 < 15、中 15–40、高 ≥ 40，另算平均與 10 格長條圖。分界值實作時用 5 幅畫（`kb/` 3 幅＋`kb_staging/` 2 幅）
  校正，結果補在本文件。
- **總結句**：「整體偏暖、低彩度、以暗調為主」這類形容由數字套固定規則產生，畫面與段落共用同一套規則。
- **色塊分布圖**：512px PNG，每個像素塗成所屬主色，只有 6 種顏色、無損；前端點色票時直接比對像素顏色，
  只亮那一色，不另存遮罩。

### 設定（`shared/models.yaml` 新增，參數一併寫進 manifest）
```yaml
color_analysis:                 # 改了就要重建索引（manifest 比對，對不上拒絕啟動）
  method: lab-kmeans-v1
  n_colors: 6
  fit_long_edge: 256
  map_long_edge: 512
  kmeans_max_iter: 30
  seed: 0
  neutral_chroma: 10
  warm_hue_deg: [340, 110]      # 跨 0°；其餘有彩色為冷色
  lightness_bands: [35, 70]
  chroma_bands: [15, 40]
```
照片的快取張數（32）不放進這個區塊，寫在程式裡：它不影響結果，放進來會讓改快取也被要求重建索引。
manifest 已經會比對 `chunking`（`IndexStore.check_manifest`），`color_analysis` 比照辦理：參數改了沒重建索引，
畫作頁的結果會和照片的算法不一致，所以拒絕啟動並提示 `make index`。

### API（4 支，都是 GET：結果固定、沒有副作用，可以快取）
| 路徑 | 內容 |
|---|---|
| `GET /api/v1/artworks/{id}/colors` | 讀索引裡的 `colors`，`source: "original"` |
| `GET /api/v1/artworks/{id}/colormap.png` | `data/index/colormaps/<id>.png`，網址帶知識庫 hash（同 `part_summary` 的 `?v=`） |
| `GET /api/v1/images/{image_id}/colors` | 即時計算，`source: "photo"`，行程內快取最近 32 張 |
| `GET /api/v1/images/{image_id}/colormap.png` | 與上一支共用快取，被擠掉就重算（結果相同） |

- 色塊圖不寫檔：照片 7 天到期清除（`purge_uploads`）時不用多清一個檔。
- `ArtworkDetail` 不變；錯誤碼沿用 `ARTWORK_NOT_FOUND`、`IMAGE_NOT_FOUND`。

回應 `ColorAnalysis`：
```
source        "original" | "photo"
method        "lab-kmeans-v1"
palette[]     hex、rgb、lab、share、name、temperature（warm／cool／neutral）、tone（dark／mid／light）
temperature   warm、cool、neutral
lightness     dark、mid、light、mean、p5、p95、histogram[10]
chroma        low、mid、high、mean、histogram[10]
summary       總結句
notes[]       例如「依照片計算，受光線與白平衡影響」「照片裡的畫框、背景也會算進去」
map_url、latency_ms
```

### 前端（`frontend/src/components/color/ColorAnalysisCard.tsx`，兩頁共用）
- 依占比切成 6 段的色帶＋6 個色票（色名、色碼、占比）；冷暖、明度、彩度各一條三段比例條，
  明度與彩度另附 10 格長條圖與平均值；色塊分布圖用 canvas，點色票只亮那一色、再點一次取消；
  最下面是總結句、來源（原圖／照片）與注意事項。
- 不加圖表套件（甘特圖也是自己畫的），只用 div＋Tailwind。
- 畫作頁：放在基本資料表下面、介紹段落上面（`useArtworkColors(id)`）。
- 以圖搜圖頁：辨識成功時在成功卡片下方顯示「依你的照片計算」，附「原圖的分析見畫作介紹」；
  拒答時在「知識庫中沒有這幅畫」下方顯示照片的分析。
- 型別照現有流程：`make openapi` → `npm run gen:api`（CI 檢查同步）。

### 問答
- 段落 `<id>#color`，主題「色彩分析」，`source_url` 為 null、`source` 為「系統計算：色彩分析（數位圖檔）」、
  授權 CC0。前端參考來源本來就能顯示沒有網址的出處（`frontend/src/components/ChatAnswer.tsx` 的 `source_label`）。
- 段落文字由固定規則產生，不用模型寫，例如：
  > 依系統對〈谿山行旅圖〉數位圖檔的色彩分析（CIELAB 空間分成 6 個主色）：主色依占比為深褐（#4A3B2C）32%、
  > 土黃 21%……。暖色占 71%、冷色 3%、中性色（接近黑白灰）26%。暗調 45%、中間調 40%、亮調 15%，
  > 平均彩度 12，屬於低彩度的畫面。以上依數位圖檔計算，可能與原作現況及展場光線下看到的顏色不同。

  （數字是示意，實際值由實作算出。）
- 檢索、`answer_v1`、問答流程都不改：色彩段落和其他段落一起比分數，照現有門檻與「最多 5 段」決定是否放進 prompt。

### 照片的限制（卡片上標示）
- 白平衡、曝光會讓顏色偏掉；辨識成功時畫作頁用的是原圖的分析。
- 未收錄的畫無法裁切，照片裡拍到的畫框、牆壁、螢幕邊框也會算進去。

計算量：預估一張圖 200 ms 以內、只用 CPU，不會觸發記憶體管理（ADR 006）。

## 驗證
**單元測試**（`backend/tests/test_color.py`）：sRGB → Lab 已知值；CIEDE2000 對 Sharma 測試資料；
70% 紅＋30% 藍的合成圖色盤占比誤差 ≤ 1%、色名為紅與藍、冷暖約 70／30；灰階漸層為 100% 中性、100% 低彩度；
同一張圖算兩次結果相同；色名表每個代表色對到自己；段落文字含畫名、占比與注意事項；
API 的 200／404；`color_analysis` 參數改了沒重建索引時拒絕啟動。

**評估**（`make eval-color`，`eval/run_color_eval.py`，程序內執行、不用開後端，結果存 `eval/runs/*-color.json`）：
| 指標 | 用途 |
|---|---|
| 決定性：每幅原圖算兩次是否完全相同 | 對應「各自建索引內容相同」 |
| 照片 vs 原圖：25 張模擬照（5 幅 × blur／crop／dim／glare／tilt）的色盤色差（依占比加權的 CIEDE2000）與冷暖比例差（百分點） | 量化「辨識成功就用原圖」的理由，看出哪種拍法最偏色 |
| 檢索：3 題顏色題（每幅知識庫畫作一題，加進 `eval/qa.jsonl`）是否取到色彩段落；其他題的段落是否被擠掉 | 確認新段落不傷現有問答 |
| 延遲 P50／P95 | 確認「200 ms 以內」 |

## 前置：Windows 重建索引
學校電腦（Windows 原生）`make index` 會在工廠標準模型那步失敗：`backend/app/cad/runner.py` 的 `import resource`，
Windows 沒有這個模組。本功能需要重建索引，所以先修（另一個 commit）：沒有 `resource` 時，
知識庫自己的標準模型（`trusted=True`）跳過 CPU／檔案大小限制；Ortho2CAD 產生的程式碼明確拒絕執行並說明原因
（和現在一樣跑不了，只是錯誤訊息變清楚）。安全性與現在相同。

## 影響
- `shared/`：`models.yaml` 新增 `color_analysis`；`openapi.json` 新增 4 支 API 與 `ColorAnalysis`；
  `CHANGELOG.md` 加一列（A：前端卡片；C：分界值校正；D：顏色題）。
- 索引：新增 `colors` 欄位、`colormaps/`、每幅畫多一段 `#color`；組員拉下來後要重跑 `make index`（manifest 會提示）。
- README：展示腳本加兩步（畫作頁色彩分析、拒答頁也能分析色彩）、評估結果表、專案結構補 `analysis/`。
