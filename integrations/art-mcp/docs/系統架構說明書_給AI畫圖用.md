# 系統架構說明書（給 AI 畫圖用）— Door 個人專題

> 用途：把這整份檔案交給 AI（Claude、ChatGPT、Gemini 等），AI 只根據這份資料，就能畫出完整、專業的系統架構圖。
> 範圍：Door 在 ArtRAG 小組專題（地端隱私多模態 RAG 專題）裡**自己做的所有內容**，以及這些內容接到小組系統的接點。
> 依據：GitHub `Sinediesoft/art-rag` 分支 `data/taiwan-folk-art`（2026-10-27），作者 Door 的提交、`integrations/art-mcp/` 全部檔案。
> 版本：v1（2026-10-27）

---

## 0. 給 AI 的指示（請先讀完再畫）

你是一位系統架構師，要替 Door 的個人專題報告畫系統架構圖。請遵守以下規則：

1. **只用本文件的節點與連線**。節點在第 4 節，連線在第 5 節，每個都有固定 ID。不要自己加元件，也不要把兩個節點合併。
2. **每張圖都照第 8 節的「分圖規格」畫**：那裡寫了每張圖要放哪些節點、哪些連線、版面方向和重點。使用者只指定一張圖時，只畫那一張。
3. **一眼分得出擁有者**（第 2 節圖例）：Door 自己做的、小組既有的、外部服務，三種外觀要不一樣。這是個人報告，重點是讓人看出「哪些是 Door 做的」。
4. **節點標籤固定兩行**：第一行是「顯示名稱」，第二行（小字）是「副標」，寫技術或檔名。數字照第 7 節，不要四捨五入，也不要自己估。
5. **連線要寫標籤**：寫第 5 節的 `label`；有 `step` 的，在標籤前加上步驟編號（例如「③ 呼叫工具」）。
6. **狀態要畫出來**：`status: planned`（規劃中，未實作）用點線加「未實作」字樣；`status: out_of_scope`（刻意不做）用灰色點線加 ✕。不要把它們畫成已完成。
7. **輸出格式**：使用者沒指定時，預設輸出 **draw.io（diagrams.net）XML**，可以直接匯入、方便之後修改。使用者要求時，也可以輸出 Mermaid 或 SVG。輸出 Mermaid 時：
   - 用 `flowchart LR` 或 `TB`，用 `subgraph` 表示區域，用 `classDef` 表示三種擁有者。
   - 時序圖（圖 2）用 `sequenceDiagram`。
8. **語言**：全部用繁體中文。程式名稱、檔名、技術名稱保留原文（例如 `server.py`、`bge-small-zh-v1.5`）。
9. **畫完要自我檢查**：照第 10 節的清單逐項確認，最後在圖下方附一行「檢查結果：N/N 通過」。

---

## 1. 專題一句話與範圍

- **一句話**：Door 自己蒐集 20 件冷門的臺灣民間藝術資料（版印、交趾陶、剪黏），自建中文語意搜尋，再包成 MCP Server，讓 Claude 桌面版可以直接查這個知識庫，回答時附上原文出處與授權。最後用 30 題 A/B 對照評估成效，並整合進小組網站。
- **Door 負責的七塊**（每一塊對應第 3 節的一個區域）：
  1. MCP 模組（執行期）
  2. 資料建置：20 件民間藝術
  3. 索引建置
  4. 安裝與維運工具：一鍵接上 Claude
  5. 評估系統：A/B 對照
  6. 與小組系統整合
  7. 小組前端介面設計（10/15 完成，屬於小組網站）
- **不屬於 Door 的部分**：小組後端的七段權限控管、Ortho2CAD 3D、庫存 Text-to-SQL、Timefold 排程、PostgreSQL 等。畫圖時只用灰色畫出「接點」，不要展開它們的內部。

---

## 2. 圖例與視覺規範

### 2-1 擁有者（最重要）

| owner 值 | 意思 | 外觀 |
| --- | --- | --- |
| `door` | Door 自己做的 | 實線框、框線較粗、主色填色（建議藍綠色 #0F766E 系，淺底 #CCFBF1），左上角加小標「Door」 |
| `group` | 小組既有的系統 | 灰色虛線框（#9CA3AF），淺灰底，文字灰色 |
| `external` | 外部服務或資料來源 | 雲朵形，或圓角框加雲圖示；淺黃底（#FEF3C7） |

### 2-2 節點形狀（依 type）

| type | 形狀 |
| --- | --- |
| `actor` | 人形圖示 |
| `client` | 視窗圖示框（應用程式） |
| `service` | 圓角矩形（執行中的程式） |
| `function` | 小圓角矩形，畫在所屬 service 裡面 |
| `store` | 圓柱（資料庫／資料夾／檔案） |
| `model` | 六角形（AI 模型） |
| `script` | 帶齒輪圖示的矩形（批次程式、.bat） |
| `doc` | 文件形（右下角折角） |
| `ui` | 瀏覽器框（網頁或介面元件） |
| `process` | 平行四邊形（人工步驟） |

### 2-3 連線樣式（依 style）

| style | 意思 | 外觀 |
| --- | --- | --- |
| `runtime` | 執行期、同步呼叫 | 實線箭頭 |
| `data` | 讀寫資料 | 實線箭頭，線較粗 |
| `batch` | 建置或離線批次 | 虛線箭頭 |
| `manual` | 人工操作 | 細虛線箭頭，旁邊加手形圖示 |
| `planned` | 規劃中、未實作 | 點線，標籤後加「（未實作）」 |
| `out_of_scope` | 刻意不做 | 灰色點線，中間畫 ✕ |

### 2-4 其他規範

- 區域（第 3 節）用大圓角框包起來，左上角寫區域名稱。
- 主要流程（圖 2 時序圖）的箭頭依步驟編號 ①②③… 排序。
- 白底；字體用思源黑體／Noto Sans TC；程式名稱用等寬字。
- 圖的右下角放圖例；圖下方註明資料來源：「依 `Sinediesoft/art-rag` 分支 `data/taiwan-folk-art`，2026-10-27」。

---

## 3. 區域（Zones）與建議版面

全景圖（圖 1）建議的版面是**三欄**：左欄放「資料怎麼來」，中欄放「執行期」，右欄放「怎麼驗證與整合」。

| id | 區域名稱 | owner | 全景圖位置 |
| --- | --- | --- | --- |
| Z1 | 使用者 | external | 中欄最上 |
| Z2 | Claude 桌面版（AI 助理） | external | 中欄上 |
| Z3 | MCP 模組 art-mcp（執行期） | door | 中欄中 |
| Z4 | 資料建置：20 件臺灣民間藝術 | door | 左欄上 |
| Z5 | 共用知識庫 kb/ | door＋group（共用） | 左欄下，跨到中欄下方 |
| Z6 | 索引建置 | door | 左欄中，夾在 Z4、Z5 和 Z3 之間 |
| Z7 | 安裝與維運工具 | door | 中欄最下 |
| Z8 | 評估系統（A/B 對照） | door | 右欄上 |
| Z9 | 與小組系統整合 | door＋group | 右欄中 |
| Z10 | 小組前端介面設計 | door（做在小組網站裡） | 右欄下 |

---

## 4. 節點清單

格式：`id｜顯示名稱｜副標（技術或檔名）｜type｜owner｜zone｜重點說明`

### Z1 使用者

```yaml
- id: U1
  label: 使用者
  sub: 導覽志工／商品設計師／學生與遊客
  type: actor
  owner: external
  zone: Z1
  note: 用自然語言問冷門的在地藝術問題，要能附出處和授權
- id: U2
  label: Door（開發與評估者）
  sub: 出題・抽查・判讀
  type: actor
  owner: door
  zone: Z1
  note: 只出現在圖 3、圖 4（人工步驟的執行者）
```

### Z2 Claude 桌面版

```yaml
- id: CD1
  label: Claude 桌面版
  sub: MCP Host／Client（Windows）
  type: client
  owner: external
  zone: Z2
  note: 讀工具說明，自己決定要呼叫哪個工具；回答由它撰寫
- id: CD2
  label: Claude 模型（雲端）
  sub: Anthropic・評估用 Sonnet 5.5 Medium
  type: model
  owner: external
  zone: Z2
  note: 工具回傳的公開段落會送到這裡，請標成「離開本機的資料」
- id: CD3
  label: Claude 設定檔
  sub: claude_desktop_config.json → mcpServers.art-rag
  type: store
  owner: external
  zone: Z2
  note: 有 Store 版（%LOCALAPPDATA%\Claude-Data\claude_*\）和官網版（%APPDATA%\Claude\）兩種位置
```

### Z3 MCP 模組（執行期）

```yaml
- id: M1
  label: MCP Server
  sub: server.py・Python mcp 2.3.0・stdio
  type: service
  owner: door
  zone: Z3
  note: 由 Claude 桌面版用子行程啟動；不開網路埠；stdout 只給 MCP 用，紀錄改寫 logging
- id: M1a
  label: search_passages
  sub: 工具：依問題找原文段落（top_k=3）
  type: function
  owner: door
  zone: Z3
  parent: M1
- id: M1b
  label: search_artworks
  sub: 工具：依描述找作品（limit=5）
  type: function
  owner: door
  zone: Z3
  parent: M1
- id: M1c
  label: get_artwork
  sub: 工具：依編號取完整資料與授權
  type: function
  owner: door
  zone: Z3
  parent: M1
- id: M1d
  label: list_artworks
  sub: 工具：列出全部作品
  type: function
  owner: door
  zone: Z3
  parent: M1
- id: M1e
  label: artworks://catalog
  sub: 資料（resource）：館藏清單
  type: function
  owner: door
  zone: Z3
  parent: M1
- id: M1f
  label: tour_script
  sub: 範本（prompt）：導覽稿（主題、分鐘數）
  type: function
  owner: door
  zone: Z3
  parent: M1
- id: M2
  label: 工具說明
  sub: 適用情境／禁忌情境與替代方案／硬性規範（Door 主筆）
  type: doc
  owner: door
  zone: Z3
  parent: M1
  note: 決定 Claude 選哪個工具；有做「故意改壞再改回」的對照實驗
- id: M3
  label: 輸入檢查
  sub: check_query：不可空白、最多 500 字
  type: function
  owner: door
  zone: Z3
  parent: M1
- id: M4
  label: 語意搜尋 Searcher
  sub: search.py
  type: service
  owner: door
  zone: Z3
  note: 在 M1 的同一個行程裡（函式庫，不是另一個服務）
- id: M4a
  label: 問句向量化
  sub: fastembed（ONNX・CPU）→ 512 維
  type: function
  owner: door
  zone: Z3
  parent: M4
- id: M4b
  label: 餘弦相似度
  sub: numpy・和全部索引卡比
  type: function
  owner: door
  zone: Z3
  parent: M4
- id: M4c
  label: 名字加分
  sub: 每對上一個名字 +0.05，上限 0.10
  type: function
  owner: door
  zone: Z3
  parent: M4
- id: M4d
  label: 門檻擋離題
  sub: MIN_SCORE 0.60
  type: function
  owner: door
  zone: Z3
  parent: M4
- id: M4e
  label: 近重複去除
  sub: 相似 ≥ 0.97 算同一段
  type: function
  owner: door
  zone: Z3
  parent: M4
- id: M4f
  label: 取前 K 段＋過期檢查
  sub: 預設 3 段；資料比索引新時附警告
  type: function
  owner: door
  zone: Z3
  parent: M4
- id: M5
  label: 中文向量模型
  sub: BAAI/bge-small-zh-v1.5・約 90 MB・512 維
  type: model
  owner: external
  zone: Z3
  note: 存在 art-mcp/models/；執行時離線（HF_HUB_OFFLINE=1）；第一次搜尋才載入
- id: M6
  label: 名字清單
  sub: keywords.txt（人名、廟名、作品名）
  type: store
  owner: door
  zone: Z3
- id: M7
  label: 索引（卡櫃）
  sub: art-mcp/index/・97 段向量＋原文
  type: store
  owner: door
  zone: Z3
- id: M8
  label: 工具呼叫登記簿
  sub: logs/tool_calls.jsonl（工具、參數、耗時、成功與否）
  type: store
  owner: door
  zone: Z3
- id: M9
  label: 伺服器紀錄
  sub: logs/server.log（每次啟動一行「啟動」）
  type: store
  owner: door
  zone: Z3
```

### Z4 資料建置

```yaml
- id: S1
  label: 國立臺灣歷史博物館
  sub: 文化部典藏網・版印 12 件・CC BY 4.0
  type: store
  owner: external
  zone: Z4
- id: S2
  label: 文化部國家文化記憶庫
  sub: 班馬傳媒建檔・交趾陶／剪黏 8 件・CC BY 3.0 TW+
  type: store
  owner: external
  zone: Z4
- id: P1
  label: 原文擷取
  sub: Claude in Chrome get_page_text・逐字抄錄
  type: process
  owner: door
  zone: Z4
- id: P2
  label: 回網頁逐段比對＋授權確認
  sub: 版印看「CC BY」標示；交趾陶看授權欄與建檔單位
  type: process
  owner: door
  zone: Z4
- id: P3
  label: 整理成小組資料規格
  sub: id 規則 nmth-／tcmb-・主題段落・基本資料段
  type: process
  owner: door
  zone: Z4
- id: P4
  label: 人工抽查
  sub: 20/20 筆逐筆核對
  type: process
  owner: door
  zone: Z4
- id: P5
  label: 資料抓取紀錄
  sub: docs/資料抓取紀錄.md（來源、決定、原文問題）
  type: doc
  owner: door
  zone: Z4
```

### Z5 共用知識庫

```yaml
- id: K1
  label: 作品資料
  sub: kb/artworks/*.json・23 件（小組 3＋Door 20）
  type: store
  owner: door
  zone: Z5
  note: 網站和 MCP 讀的是同一份；20 件由 Door 新增
- id: K2
  label: 作品圖片
  sub: kb/images/*.jpg・Door 新增 20 張
  type: store
  owner: door
  zone: Z5
- id: K3
  label: 知識庫版本
  sub: kb/VERSION 2026.10.2 → 2026.10.3
  type: store
  owner: door
  zone: Z5
- id: K4
  label: 小組資料規格
  sub: shared/schemas/artwork.schema.json＋validate_kb（make check-kb）
  type: script
  owner: group
  zone: Z5
  note: Door 的 20 筆通過驗證：23/23、0 個問題
```

### Z6 索引建置

```yaml
- id: B1
  label: 建立索引
  sub: build_index.py／build_index.bat
  type: script
  owner: door
  zone: Z6
- id: B1a
  label: 讀取＋過濾
  sub: 排除未審核、沒有 source_url、授權不在白名單（CC0／公有領域／CC BY 4.0）
  type: function
  owner: door
  zone: Z6
  parent: B1
- id: B1b
  label: 切段
  sub: 一段只講一件事・最多約 300 字
  type: function
  owner: door
  zone: Z6
  parent: B1
- id: B1c
  label: 向量化
  sub: bge-small-zh-v1.5
  type: function
  owner: door
  zone: Z6
  parent: B1
- id: B1d
  label: 存進卡櫃
  sub: 寫入 index/
  type: function
  owner: door
  zone: Z6
  parent: B1
```

### Z7 安裝與維運工具

```yaml
- id: I1
  label: 一鍵接上 Claude
  sub: connect_claude.bat
  type: script
  owner: door
  zone: Z7
  note: 依序執行 I2 → B1 → I4，最後重開 Claude
- id: I2
  label: 環境安裝
  sub: setup.bat・uv・Python 3.12・.venv・requirements.lock.txt
  type: script
  owner: door
  zone: Z7
- id: I3
  label: 環境檢查＋產生設定範本
  sub: check_env.py（擋中文路徑、填好絕對路徑）
  type: script
  owner: door
  zone: Z7
- id: I4
  label: 寫入 Claude 設定
  sub: fix_claude_config.py・先備份、只合併 mcpServers、寫完驗證、失敗還原
  type: script
  owner: door
  zone: Z7
- id: I5
  label: 設定檢查
  sub: check_claude_config.bat
  type: script
  owner: door
  zone: Z7
- id: I6
  label: 自我檢查
  sub: self_check.py・用 stdio 模擬 Claude 連線・27 項
  type: script
  owner: door
  zone: Z7
- id: I7
  label: 連線測試工具
  sub: hello_server.py（階段 2）・remove_hello.py
  type: script
  owner: door
  zone: Z7
  note: 先確認「連線本身」通，再換正式 Server，用來分辨是哪一層壞掉
```

### Z8 評估系統

```yaml
- id: E1
  label: 評估考卷
  sub: eval/questions.csv・30 題（一般 6、冷門 15、知識庫沒有的 4、陷阱 5）
  type: store
  owner: door
  zone: Z8
- id: E2
  label: 搜尋小測驗
  sub: eval/search_questions.csv・25 題（20 有答案＋5 沒答案）
  type: store
  owner: door
  zone: Z8
  note: 用來調整門檻和加分；和評估考卷分開出（避免拿考古題當期末考）
- id: E3
  label: 搜尋測試與診斷
  sub: test_search.py（Top-3 命中 19/20）・diag_search.py（一次只改一件事）
  type: script
  owner: door
  zone: Z8
- id: E4
  label: 考試小幫手
  sub: exam_helper.py＋exam_A.bat／exam_B.bat（剪貼簿遞題、收答案、記時間）
  type: script
  owner: door
  zone: Z8
- id: E5
  label: 條件 A：不接 MCP
  sub: 同帳號、同模型、關網頁搜尋、每題新對話
  type: client
  owner: external
  zone: Z8
- id: E6
  label: 條件 B：接上 MCP
  sub: 同上，只差有沒有接 art-rag
  type: client
  owner: external
  zone: Z8
- id: E7
  label: 回答檔
  sub: eval/answers/A、eval/answers/B（每題一檔）
  type: store
  owner: door
  zone: Z8
- id: E8
  label: 自動計分
  sub: score.py・6 項指標・用存檔時間對到登記簿
  type: script
  owner: door
  zone: Z8
- id: E9
  label: 成績單
  sub: eval/results/（per_question.csv、score_summary.md、report.html、tool_map_B.csv）
  type: store
  owner: door
  zone: Z8
- id: E10
  label: 人工複核＋失敗分析
  sub: 修正 1 題標準答案、3 種計分誤判；分析 3 個失敗案例
  type: process
  owner: door
  zone: Z8
```

### Z9 與小組系統整合

```yaml
- id: W1
  label: MCP 說明頁
  sub: frontend/public/mcp.html・明暗模式・一鍵接上步驟
  type: ui
  owner: door
  zone: Z9
- id: W2
  label: 側欄連結
  sub: Sidebar.tsx「AI 助理接口（MCP）↗」・所有身分可見
  type: ui
  owner: door
  zone: Z9
- id: W3
  label: 搜尋同分排序修正
  sub: index_store.py／pg_index_store.py（檔案版和資料庫版一致）
  type: function
  owner: door
  zone: Z9
- id: W4
  label: 小組測試與管線修正
  sub: test_identify.py（不寫死件數）・make_lora_data.py（英文名可省略）
  type: function
  owner: door
  zone: Z9
- id: W5
  label: 整合說明書
  sub: INTEGRATION.md（給組員的 AI 讀）
  type: doc
  owner: door
  zone: Z9
- id: G1
  label: 小組網站前端
  sub: React SPA・由後端 :8000 提供
  type: ui
  owner: group
  zone: Z9
- id: G2
  label: 小組後端 API
  sub: FastAPI :8000・/api/v1・七段權限控管
  type: service
  owner: group
  zone: Z9
- id: G3
  label: 小組索引建置
  sub: make index・Chinese-CLIP＋bge-m3
  type: script
  owner: group
  zone: Z9
- id: G4
  label: 藝術模組功能
  sub: 作品頁・以文搜畫／以圖搜圖・色彩分析・作品問答
  type: ui
  owner: group
  zone: Z9
  note: Door 的 20 件資料重建索引後，在這些功能裡都查得到
```

### Z10 小組前端介面設計（10/15，Door 做進小組網站）

```yaml
- id: F1
  label: 側欄模組入口
  sub: 工廠／藝術模組直接進入・可展開功能頁・收合窄欄圖示
  type: ui
  owner: door
  zone: Z10
- id: F2
  label: 明暗模式
  sub: 頁首太陽／月亮圖示・入口與模組共用設定
  type: ui
  owner: door
  zone: Z10
- id: F3
  label: 展示區細節
  sub: 浮在圖上的玻璃展籤・可收合縮圖列（記住狀態）
  type: ui
  owner: door
  zone: Z10
- id: F4
  label: 工廠模組工作台
  sub: Workbench.tsx・左對話＋右四板塊（圖紙／3D／排程／庫存）
  type: ui
  owner: door
  zone: Z10
  note: 可拖曳縫、聚焦、最大化、收合；四格／設計檢視／生產檢視；container query 精簡內容
- id: F5
  label: 工作台原型
  sub: demo/factory-workbench/index.html（單一檔案、不連後端）
  type: ui
  owner: door
  zone: Z10
  note: 先做原型、組內同意，再正式做進前端
```

---

## 5. 連線清單

格式：`id｜from → to｜label｜style｜step（屬於哪個流程的第幾步）｜status`

```yaml
# ── 執行期問答（流程 F-A）──
- {id: R1,  from: U1,  to: CD1, label: 用自然語言提問,                          style: runtime, step: A1}
- {id: R2,  from: CD1, to: CD2, label: 問題＋工具說明,                          style: runtime, step: A2}
- {id: R3,  from: CD2, to: CD1, label: 決定呼叫哪個工具與參數,                  style: runtime, step: A3}
- {id: R4,  from: CD1, to: M1,  label: MCP 工具呼叫（JSON-RPC・stdio）,          style: runtime, step: A4}
- {id: R5,  from: M1,  to: M3,  label: 檢查問句,                                style: runtime, step: A5}
- {id: R6,  from: M1,  to: M4,  label: 搜尋,                                    style: runtime, step: A6}
- {id: R7,  from: M4a, to: M5,  label: 問句 → 512 維向量,                       style: runtime, step: A7}
- {id: R8,  from: M4b, to: M7,  label: 讀索引、算相似度,                        style: data,    step: A8}
- {id: R9,  from: M4c, to: M6,  label: 讀名字清單,                              style: data,    step: A9}
- {id: R10, from: M4,  to: M1,  label: 前 3 段原文＋出處＋授權,                  style: runtime, step: A10}
- {id: R11, from: M1,  to: M8,  label: 記一筆工具呼叫,                          style: data,    step: A11}
- {id: R12, from: M1,  to: CD1, label: 工具結果（原文段落＋source_url＋license）, style: runtime, step: A12}
- {id: R13, from: CD1, to: CD2, label: 依段落撰寫回答,                          style: runtime, step: A13}
- {id: R14, from: CD1, to: U1,  label: 回答＋出處＋授權標示,                    style: runtime, step: A14}
- {id: R15, from: M1,  to: K1,  label: get_artwork／list_artworks 讀作品資料,     style: data}
- {id: R16, from: CD3, to: CD1, label: 啟動時讀 mcpServers,                     style: runtime}
- {id: R17, from: CD1, to: M1,  label: 用子行程啟動 server.py,                  style: runtime}
- {id: R18, from: M1,  to: M9,  label: 寫啟動與錯誤紀錄,                        style: data}

# ── 資料建置（流程 F-B）──
- {id: D1, from: S1, to: P1, label: 作品頁面,                                   style: manual, step: B1}
- {id: D2, from: S2, to: P1, label: 作品頁面,                                   style: manual, step: B1}
- {id: D3, from: P1, to: P2, label: 逐字抄錄的原文,                             style: manual, step: B2}
- {id: D4, from: P2, to: P3, label: 確認過的原文與授權,                         style: manual, step: B3}
- {id: D5, from: P3, to: K1, label: 20 筆 JSON,                                 style: batch,  step: B4}
- {id: D6, from: P3, to: K2, label: 20 張圖片,                                  style: batch,  step: B4}
- {id: D7, from: K1, to: K4, label: 格式驗證（23/23 通過）,                     style: batch,  step: B5}
- {id: D8, from: U2, to: P4, label: 逐筆抽查,                                   style: manual, step: B6}
- {id: D9, from: P4, to: K1, label: 抽查 20/20,                                 style: manual, step: B6}
- {id: D10, from: P3, to: K3, label: 版本 +1,                                   style: batch,  step: B7}
- {id: D11, from: P3, to: P5, label: 記錄來源與決定,                            style: manual}

# ── 索引建置（流程 F-C）──
- {id: X1, from: K1,  to: B1a, label: 讀 23 件作品（沒有 data/artworks/ 時讀 kb/artworks/）, style: batch, step: C1}
- {id: X2, from: B1a, to: B1b, label: 通過過濾的作品,                           style: batch, step: C2}
- {id: X3, from: B1b, to: B1c, label: 97 段文字,                                style: batch, step: C3}
- {id: X4, from: B1c, to: M5,  label: 段落 → 向量（第一次會下載模型）,          style: batch, step: C4}
- {id: X5, from: B1d, to: M7,  label: 寫入卡櫃,                                 style: batch, step: C5}

# ── 一鍵接上 Claude（流程 F-D）──
- {id: N1, from: U1, to: I1,  label: 雙擊,                                      style: manual, step: D1}
- {id: N2, from: I1, to: I2,  label: ① 安裝環境,                                 style: batch,  step: D2}
- {id: N3, from: I2, to: I3,  label: 檢查環境、產生設定範本,                    style: batch,  step: D3}
- {id: N4, from: I1, to: B1,  label: ② 建索引,                                   style: batch,  step: D4}
- {id: N5, from: I1, to: I4,  label: ③ 關 Claude → 寫設定,                      style: batch,  step: D5}
- {id: N6, from: I4, to: CD3, label: 備份＋合併 mcpServers.art-rag,             style: data,   step: D6}
- {id: N7, from: I1, to: CD1, label: 重開 Claude,                               style: batch,  step: D7}
- {id: N8, from: I6, to: M1,  label: 模擬 Claude 用 stdio 連線檢查 27 項,       style: batch}
- {id: N9, from: I5, to: CD3, label: 檢查設定是否正確,                          style: batch}
- {id: N10, from: I7, to: CD1, label: hello 工具先測連線（階段 2）,              style: batch}

# ── 評估（流程 F-E）──
- {id: V1,  from: U2, to: E2,  label: 出搜尋小測驗,                             style: manual, step: E1}
- {id: V2,  from: E2, to: E3,  label: 25 題,                                    style: batch,  step: E2}
- {id: V3,  from: E3, to: M4,  label: 直接測搜尋；定門檻 0.60、加分、去重,      style: batch,  step: E3}
- {id: V4,  from: U2, to: E1,  label: 出評估考卷（先寫問題再查答案）,           style: manual, step: E4}
- {id: V5,  from: E1, to: E4,  label: 依序遞題,                                 style: batch,  step: E5}
- {id: V6,  from: E4, to: E5,  label: 貼上題目（條件 A）,                       style: manual, step: E6}
- {id: V7,  from: E4, to: E6,  label: 貼上題目（條件 B）,                       style: manual, step: E6}
- {id: V8,  from: E6, to: M1,  label: 條件 B 會呼叫工具,                        style: runtime, step: E7}
- {id: V9,  from: E5, to: E7,  label: 回答 → answers/A,                         style: data,   step: E8}
- {id: V10, from: E6, to: E7,  label: 回答 → answers/B,                         style: data,   step: E8}
- {id: V11, from: E7, to: E8,  label: 30＋30 份回答,                            style: batch,  step: E9}
- {id: V12, from: M8, to: E8,  label: 登記簿（用存檔時間對到每一題）,           style: batch,  step: E9}
- {id: V13, from: E1, to: E8,  label: 標準答案與得分關鍵字,                     style: batch,  step: E9}
- {id: V14, from: E8, to: E9,  label: 6 項指標、HTML 成績單,                    style: data,   step: E10}
- {id: V15, from: E9, to: E10, label: 人工複核、失敗分析,                       style: manual, step: E11}

# ── 與小組系統整合（流程 F-F）──
- {id: J1,  from: K1, to: G3, label: 同一份資料（重建索引後網站查得到 20 件）,   style: batch,  step: F1}
- {id: J2,  from: G3, to: G2, label: 小組索引,                                  style: data,   step: F2}
- {id: J3,  from: G2, to: G4, label: 作品頁、搜尋、問答,                        style: runtime, step: F3}
- {id: J4,  from: G1, to: W2, label: 側欄顯示連結,                              style: runtime, step: F4}
- {id: J5,  from: W2, to: W1, label: 另開分頁 /mcp.html,                        style: runtime, step: F5}
- {id: J6,  from: W1, to: I1, label: 引導使用者執行 connect_claude.bat,         style: manual, step: F6}
- {id: J7,  from: W3, to: G2, label: 修正搜尋同分排序,                          style: data}
- {id: J8,  from: W5, to: G2, label: 指示組員 AI：合併後要 check-kb、index、重啟, style: manual}
- {id: J9,  from: M1, to: G2, label: 連接模式：MCP 轉呼叫小組 /api/v1,           style: planned, status: planned}
- {id: J10, from: G2, to: M1, label: 網站聊天改走 MCP（要改七段權限控管）,      style: out_of_scope, status: out_of_scope}

# ── 前端介面設計（流程 F-G）──
- {id: Q1, from: F5, to: F4, label: 原型 → 組內同意 → 正式做進前端,             style: manual, step: G1}
- {id: Q2, from: F4, to: G2, label: 板塊建議問句經 /agent/route 送進對話,        style: runtime}
- {id: Q3, from: F1, to: G1, label: 做進小組網站,                               style: data}
- {id: Q4, from: F2, to: G1, label: 做進小組網站,                               style: data}
- {id: Q5, from: F3, to: G1, label: 做進小組網站,                               style: data}
- {id: Q6, from: F4, to: G1, label: 做進小組網站（工廠模組）,                   style: data}
```

---

## 6. 流程說明（文字版，給時序圖和流程圖用）

### F-A 執行期問答（圖 2，時序圖）
參與者由左到右：U1 使用者 → CD1 Claude 桌面版 → CD2 Claude 模型（雲端）→ M1 server.py → M4 search.py → M5 向量模型 → M7 索引 → M8 登記簿。

1. 使用者問：「團鶴版印彩紙的圖案可以商用嗎？要怎麼標示？」
2. Claude 桌面版把問題和工具說明交給 Claude 模型。
3. 模型決定呼叫 `search_passages`，參數 query 要帶作品名（工具說明規定的）。
4. Claude 桌面版透過 stdio 呼叫 server.py。
5. server.py 檢查問句：不可空白、不超過 500 字。
6. search.py 執行：問句向量化 → 餘弦相似度 → 名字加分 → 門檻 0.60 → 去重 → 取前 3 段。
7. 找不到時，回傳「下一步該做什麼」，例如改用 `list_artworks`。Claude 會照著做，不會放棄或亂編。
8. server.py 在登記簿記一筆。
9. 回傳原文段落、出處網址、授權。
10. Claude 模型只根據段落撰寫回答。
11. 使用者看到回答，附出處與授權標示。

時序圖右側請加註：**工具只找原文、不寫答案**。這樣筆電（8 GB、無獨立顯卡）不用跑大模型，出錯時也分得出是「搜尋錯」還是「回答錯」。

### F-B 資料建置（圖 3 左半）
外部網站 →（Claude in Chrome 逐字抄）→ 回網頁逐段比對＋授權確認 → 整理成小組規格（id、主題段落、基本資料段）→ 寫入 kb/artworks 和 kb/images → 小組 validate_kb 驗證 23/23 → Door 抽查 20/20 → kb/VERSION +1 → 寫資料抓取紀錄。

### F-C 索引建置（圖 3 右半）
kb/artworks → 讀取並過濾（未審核、沒有出處、授權不在白名單的都不收）→ 切段（最多約 300 字，共 97 段）→ bge-small-zh 向量化 → 寫入 index/。資料一改就要重建；資料比索引新時，搜尋結果會附過期警告。

### F-D 一鍵接上 Claude（圖 5）
雙擊 connect_claude.bat →
1. setup：用 uv 裝 Python 3.12、建 .venv、照鎖定版本裝套件；check_env 擋中文路徑、產生設定範本。
2. build_index：建索引（第一次下載約 90 MB 的模型）。
3. 關掉 Claude → fix_claude_config：備份 → 只合併 mcpServers → 寫完重讀驗證，失敗就還原。
4. 重開 Claude。

完成後，輸入框旁的「＋」選單裡看得到 art-rag。輔助工具：check_claude_config（檢查設定）、self_check（27 項）、hello_server（最早期的連線測試）。

### F-E 評估（圖 4）
兩條線：

- **調整搜尋**：搜尋小測驗 25 題 → test_search／diag_search → 決定門檻和加分（Top-3 命中 19/20）。
- **A/B 評估**：評估考卷 30 題 → 考試小幫手遞題 → 條件 A（不接）和條件 B（接上）各答 30 題 → 回答檔 → score.py。score.py 讀三樣東西：回答檔、登記簿（用存檔時間對到每一題）、標準答案。

score.py 算出 6 項指標：關鍵字命中、出處是否真實、該拒答有沒有拒答、不該拒答卻拒答、工具選對、參數正確。接著產生成績單，再經人工複核和失敗分析。

### F-F 與小組整合（圖 6，分層圖）
用五層由下往上畫，每層標狀態：

| 層 | 內容 | 狀態 |
| --- | --- | --- |
| 1 資料 | 20 件進 kb/，小組網站重建索引後查得到 | ✔ 已完成（展示主機要執行 make index） |
| 2 連接模式 | MCP 轉呼叫小組後端 /api/v1 | ⋯ 規劃中，未實作 |
| 3 隨系統啟動 | stdio 由 Claude 啟動，不需要 | — 不需要 |
| 4 前端 | /mcp.html＋側欄連結 | ✔ 已完成 |
| 5 網站聊天走 MCP | 要改七段權限控管 | ✕ 刻意不做 |

另外標出 Door 對小組程式的小修正：W3 搜尋同分排序、W4 測試與管線。

### F-G 前端介面設計（圖 7）
先做原型 demo/factory-workbench（F5）→ 組內同意 → 正式做進前端，成為工作台（F4）。另外三項（側欄模組入口 F1、明暗模式 F2、展示區細節 F3）都做進小組網站 G1。

---

## 7. 關鍵數字（可以標在圖上，照抄）

| 項目 | 數字 |
| --- | --- |
| 知識庫作品 | 23 件（小組 3＋Door 20：版印 12、交趾陶／剪黏 8） |
| 索引 | 97 段 |
| 向量模型 | BAAI/bge-small-zh-v1.5，512 維，約 90 MB，CPU 可跑 |
| 搜尋參數 | 門檻 0.60；名字加分每個 0.05，上限 0.10；去重 0.97；預設前 3 段；問句最多 500 字；切段最多約 300 字 |
| MCP | 4 個工具＋1 個資料＋1 個範本；mcp 2.3.0；stdio |
| 自我檢查 | 27/27 |
| 搜尋小測驗 | 25 題；Top-3 命中 19/20 |
| A/B 評估 | 30 題；關鍵字全對 19%→96%（5/26→25/26）；附出處 0%→92%（0/26→24/26）；編造網址 0／0；該拒答 4/4、4/4；不該拒答卻拒答 10/26→0/26；工具選對 29/30 |
| 穩定性 | 基準 10 題，條件 A 不同天、不同帳號跑兩次，8/10 相同 |
| 小組驗證 | validate_kb 23/23、0 個問題；kb 版本 2026.10.3 |
| 開發期間 | 2026-10-23～10-27（MCP）；前端介面 2026-10-15 |

---

## 8. 分圖規格

每張圖都要有：標題、副標題（一句話說這張圖要看什麼）、圖例、資料來源註記。

### 圖 1 全景圖：Door 做了什麼（一張看懂）
- **型式**：分區方塊圖（flowchart LR），照第 3 節三欄版面。
- **放進來的節點**：U1、CD1、CD2、CD3、M1（只顯示 M1a～M1f 名稱，不展開）、M4（標示「語意搜尋」，副標列出五個步驟）、M5、M7、M8、S1、S2、P3、K1、K2、K4、B1、I1、E1、E8、E9、W1、W2、G1、G2、G3、G4、F4。
- **連線**：R1、R4、R6、R12、R14、R15、D1、D2、D5、D7、X1、X5、N1、N6、V8、V11、V12、V14、J1、J3、J5、J6、J9（點線）、J10（灰 ✕）、Q6。
- **重點**：Door 的區塊用主色，小組的用灰色，外部用黃色；讀者看一眼就能分辨。右上角放一句結論：「接上 MCP 後關鍵字全對 19%→96%、附出處 0%→92%」。

### 圖 2 MCP 執行期時序圖
- **型式**：sequenceDiagram。
- **內容**：照 F-A 的 11 步；連線用 R1～R14。
- **重點**：標出「離開本機」的邊界（CD2 在雲端，其他都在使用者的電腦上）。在 M4 那一段畫一個方框，列出五個搜尋步驟和參數。

### 圖 3 資料與索引建置管線
- **型式**：flowchart LR，分兩段：左邊 Z4→Z5，右邊 Z5→Z6→M7。
- **節點**：S1、S2、P1～P5、U2、K1～K4、B1、B1a～B1d、M5、M7。
- **連線**：D1～D11、X1～X5。
- **重點**：授權白名單、人工抽查 20/20、validate_kb 23/23 都要出現在圖上。

### 圖 4 評估系統
- **型式**：flowchart TB，上下兩條泳道：「調整搜尋」和「A/B 評估」。
- **節點**：U2、E1～E10、M1、M4、M8。
- **連線**：V1～V15。
- **重點**：E5 和 E6 左右並排，標「只差有沒有接 MCP」；E8 有三條輸入線（V11、V12、V13）。旁邊放第 7 節的 A/B 數字小表。

### 圖 5 一鍵接上 Claude（安裝流程）
- **型式**：flowchart TB，由上往下。
- **節點**：U1、I1～I7、B1、CD1、CD3、M1。
- **連線**：N1～N10、R16、R17。
- **重點**：I4 旁加註「先備份、只合併、寫完驗證、失敗還原」；最下方是完成狀態「＋選單看得到 art-rag」。

### 圖 6 與小組系統整合（五層）
- **型式**：分層圖，由下往上畫第 1～5 層，每層一列，右邊放狀態圖示（✔／⋯／—／✕）。
- **節點**：K1、G1～G4、W1～W5、M1。
- **連線**：J1～J10。
- **重點**：J9 用點線加「未實作」，J10 用灰色加 ✕。

### 圖 7 前端介面設計
- **型式**：元件地圖。中間畫 G1 小組網站框，裡面放 F1～F4；左邊放 F5 原型，用箭頭指向 F4。
- **連線**：Q1～Q6。
- **重點**：F4 畫成縮圖示意：左邊對話欄，右邊 2×2 四個板塊（圖紙、3D、排程、庫存）。

---

## 9. 不要畫的、常見錯誤

- ✘ 不要把 search.py 畫成獨立的服務或網路服務。它跟 server.py 在同一個行程裡。
- ✘ 不要在 MCP 和 Claude 之間畫 HTTP 或網路埠。它們之間是 **stdio**：Claude 用子行程啟動 server.py。
- ✘ 不要畫「MCP 呼叫小組後端」的實線。連接模式**未實作**，只能畫點線（J9）。
- ✘ 不要說 MCP Server 會產生回答。它只回傳原文段落，回答由 Claude 撰寫。
- ✘ 不要把小組的 Chinese-CLIP／bge-m3 畫進 MCP。MCP 用的是 bge-small-zh-v1.5，跟小組後端是不同的模型和索引，只共用 kb/artworks 原始資料。
- ✘ 不要展開小組後端的內部（七段權限控管、Ortho2CAD、Text-to-SQL、Timefold、PostgreSQL）。畫成一個灰色的 G2 就好。
- ✘ 數字不要自己改，照第 7 節。

---

## 10. 自我檢查清單（AI 畫完要逐項確認）

1. 所有 owner=door 的節點都用主色實線框，而且看得出「Door」標記。
2. 小組既有節點（G1～G4、K4）是灰色虛線框。
3. 外部節點（U1、CD1、CD2、CD3、S1、S2、M5、E5、E6）用外部樣式。
4. MCP 和 Claude 之間的連線標的是「stdio」，不是 HTTP。
5. search.py 畫在 server.py 同一個行程裡（巢狀，或緊貼在一起）。
6. J9 是點線加「未實作」；J10 是灰色點線加 ✕。
7. 搜尋的五個步驟和參數（0.05／0.10／0.60／0.97／前 3 段）都有出現（圖 1 用副標，圖 2 用方框）。
8. 評估數字和第 7 節完全一致。
9. 每條連線都有文字標籤；流程連線有步驟編號。
10. 有圖例、標題、副標題、資料來源註記。
11. 沒有出現本文件以外的元件。
12. 全部用繁體中文，程式名稱保留原文。

---

## 11. 給 Door：怎麼用這份說明書

1. 開一個新的 AI 對話，把**整份檔案**貼上（或當附件上傳）。
2. 接著貼下面其中一句：
   - 「請照第 8 節畫『圖 1 全景圖』，輸出 draw.io XML。」
   - 「請照第 8 節畫『圖 2 MCP 執行期時序圖』，輸出 Mermaid。」
   - 「請照第 8 節依序畫圖 1 到圖 7，每張圖分開輸出 Mermaid。」
3. draw.io XML 的用法：打開 https://app.diagrams.net → 檔案 → 匯入 → 貼上 XML，之後可以拖拉、改顏色，再匯出成 PNG 放進簡報。
4. 畫出來有缺漏時，貼一句：「請照第 10 節重新檢查，把沒通過的項目補上。」

> 這份文件沒有寫到的貢獻（例如小組企劃討論、簡報、其他沒有進 Git 的工作）請自己補在第 4 節，照同樣格式加節點，再到第 5 節加連線。
