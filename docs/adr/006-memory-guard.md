# ADR 006：記憶體管理——使用率超過 80% 時釋放目前流程用不到的模型

- 日期：2026-10-01
- 狀態：採用（demo）

## 背景
展示機是 16 GB 統一記憶體的 MacBook Air。全部模型同時載入時：

| 模型／服務 | 位置 | 約 |
|---|---|---|
| Chinese-CLIP（以圖搜圖） | 後端行程（PyTorch） | 0.75 GB |
| bge-m3（文字檢索） | 後端行程（PyTorch） | 2.2 GB |
| Qwen3-VL 4B（問答、Text-to-SQL、讀尺寸） | Ollama | 3.3 GB＋KV |
| Ortho2CAD（3D 重建，Qwen3-VL-8B 微調） | llama-server | 6.2 GB |
| Timefold 排程服務 | JVM | 0.1 GB |

再加上瀏覽器與作業系統，展示中很容易超過 80%，開始壓縮、換頁。但每個流程只用其中幾個模型：
生產排程完全不用 AI 模型、Text-to-SQL 只用 Qwen3-VL、3D 重建只用 Ortho2CAD。

## 決定
後端加一個記憶體管理器（`app/services/memory_guard.py`），規則：

1. **觸發**：系統記憶體使用率（psutil）≥ `MEMORY_HIGH_PCT`（預設 80）。
   - 進入流程時：把這個流程**還要載入**的模型（估計大小）算進去，預估超過門檻就先釋放，
     避免「載入後才超過、等背景監控才處理」（實測沒有預估時，78% 進入 Text-to-SQL、載入 Qwen3-VL 後衝到 96%）
   - 背景每 5 秒再檢查一次目前的使用率
2. **保留**：目前流程要用的模型＋其他進行中請求正在用的模型（引用計數 > 0，不會打斷生成中的回答）；
   背景監控保留「最近一次流程」的模型
3. **釋放**其餘有載入的模型，下次用到時都會自動重新載入：

| 模型 | 釋放方式 | 重新載入 |
|---|---|---|
| Chinese-CLIP、bge-m3 | 從後端行程卸載（`embedders.release()`＋gc） | 下次 embedding 時，約 1 秒（檔案在快取） |
| Qwen3-VL | Ollama `keep_alive=0` | Ollama 下次請求時，約 3–5 秒 |
| Ortho2CAD | llama-server **router 模式**的 `POST /models/unload` | 下次 3D 重建時自動載入，約 5–10 秒 |
| Timefold | `POST /admin/release`（清掉舊結果＋GC，SerialGC 會把 heap 還給系統） | 不需要 |

| 流程 | 用到的模型 |
|---|---|
| 以圖搜圖／圖紙辨識 | Chinese-CLIP |
| 以文搜尋 | Chinese-CLIP、bge-m3（找圖紙只用 bge-m3） |
| 圖文問答／製程問答 | bge-m3、Qwen3-VL（照片辨識另加 Chinese-CLIP） |
| 3D 重建 | Ortho2CAD（照片另加 Chinese-CLIP、Qwen3-VL） |
| 庫存查詢（Text-to-SQL） | Qwen3-VL |
| 生產排程 | Timefold |

推論伺服器不在本機時（例如組員連 5070 Ti）不動它：釋放遠端記憶體對本機沒有幫助。

### Ortho2CAD 改用 llama-server 的 router 模式
原本 `llama-server -m …` 是單一模型模式，模型載入後無法卸載。改成 router 模式（`--models-preset deploy/llama-router.ini`），
參數（圖片 token 上限、context、q8_0 KV cache）寫在 preset 裡，`load-on-startup = false`：第一次 3D 重建時才載入。
2026-10-01 以〈連接法蘭〉核對：router 模式產生的 CadQuery 程式碼與原本**逐字相同**（709 token）；
卸載後 llama-server 行程從 6,164 MB 降到 50 MB。

## 實測（`make demo-test`，MacBook Air M5 16 GB，2026-10-01）

| 步驟 | 記憶體 | 記憶體管理 |
|---|---|---|
| 1. 以文字找圖紙 | 67% → 71% | — |
| 2. 製程問答（載入 Qwen3-VL） | 71% → 73% | — |
| 3. 3D 重建 | 73% → 79% | 進入時預估載入 Ortho2CAD 後約 111%：先釋放 Chinese-CLIP、bge-m3、Qwen3-VL（73% → 49%） |
| 4. 開立工單 | 79% | — |
| 5. 生產排程（Timefold） | 79% → 80% | 未達門檻、沒有要載入的模型，不動作 |
| 6. Text-to-SQL | 80% → 74% | 進入時預估載入 Qwen3-VL 後約 104%：先釋放 Ortho2CAD（80% → 44%） |

只有背景監控、沒有預估的版本：3D 重建載入後衝到 96%、Text-to-SQL 載入後衝到 96%，要等下一次背景檢查才降下來。

## 可看到的地方
- 每個頁面右上角：記憶體使用率；剛釋放過模型時顯示「釋放 N 個模型 · 降到 X%」
- 系統狀態頁「記憶體管理」：使用率與 80% 門檻、各模型是否載入／使用中、最近的釋放紀錄、
  「立即釋放閒置模型」按鈕（展示用）
- 排程頁與 SSE `done.memory`：這次排程前釋放了哪些模型、記憶體從幾 % 降到幾 %
- `make demo-test`：走一遍「找圖紙 → 問答 → 3D 重建 → 開立工單 → 排程 → Text-to-SQL」，逐步列出記憶體與釋放紀錄

## 取捨
- 被釋放的模型下次用到要重新載入（見上表），換來的是展示時不會因為換頁而全面變慢
- 記憶體壓力本身不會讓 Ortho2CAD 解碼變慢（ADR 003 的控制變因實驗）；這裡處理的是**記憶體不足**，
  不是速度。重建時同時問答會搶 GPU，仍建議展示時不要同時進行
- `MEMORY_GUARD=false` 可關閉；測試環境（pytest）關閉，避免卸載開發者本機正在用的模型
