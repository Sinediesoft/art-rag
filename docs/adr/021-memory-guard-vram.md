# ADR 021：記憶體管理分兩個池——有 NVIDIA 顯示卡時，VRAM 裡的模型另外看顯示記憶體

- 日期：2026-10-05
- 狀態：採用（補充 ADR 006）

## 背景
ADR 006 的記憶體管理只看一個數字：系統記憶體使用率。展示機原本是 16 GB 統一記憶體的 Mac，
Qwen3-VL、Ortho2CAD 和其他程式共用同一塊記憶體，這樣是對的。

換到 5070 Ti 主機（獨立顯示卡、16 GB VRAM＋32 GB 系統記憶體）後：
- Qwen3-VL（Ollama）、Ortho2CAD（llama-server）**在 VRAM**，卸載它們幾乎不會降低系統記憶體使用率
- 系統記憶體常被其他程式（遊戲、錄影、瀏覽器）吃到 90% 以上，和模型無關
- 實測（2026-10-03）：系統記憶體 98% 時，背景監控**每 18 秒卸載一次 Qwen3-VL 8B**（98% → 94%，又被其他程式吃回去），
  下一題問答要重新載入（3–40 秒）。只好把這台的 `MEMORY_HIGH_PCT` 調到 92，但問題還在

## 決定
分成兩個記憶體池，各自判斷、各自釋放：

| 池 | 模型 | 看什麼 | 門檻 |
|---|---|---|---|
| `ram` | Chinese-CLIP、bge-m3（後端行程，CPU）、Timefold（JVM） | 系統記憶體使用率（psutil） | `MEMORY_HIGH_PCT`（80） |
| `gpu` | Qwen3-VL（Ollama）、Ortho2CAD（llama-server） | 第一張 NVIDIA 顯示卡的使用率（`nvidia-smi`，整張卡、含其他程式） | `MEMORY_GPU_HIGH_PCT`（90，新增） |

- **只動超過門檻的池**：系統記憶體滿了只釋放 `ram` 的模型；VRAM 滿了只釋放 `gpu` 的模型。
- **沒有 NVIDIA 顯示卡**（Mac、沒裝驅動）時沒有 `gpu` 池，所有模型都在 `ram`，**行為和 ADR 006 完全相同**。
  後端啟動後第一次查詢決定（插拔顯示卡要重啟後端）。
- 進入流程前的預估（ADR 006）保留：要載入的模型算進**它自己的池**——3D 重建要載入 Ortho2CAD，
  只拿 VRAM 去推估，不拿系統記憶體推估。
- 手動「立即釋放閒置模型」兩個池都放。
- Qwen3-VL 的大小改從 Ollama `/api/tags` 讀實際檔案大小 ×1.15（KV cache），查不到才用 4 GB：
  主力換成 8B（約 6.7 GB）後，預估才不會低估。
- `nvidia-smi` 每次檢查呼叫一次（背景每 5 秒）；Windows 上用 `CREATE_NO_WINDOW`，不會跳出主控台視窗。
- 推論伺服器不在本機時照舊不動它（`probe` 回 None）。

### API 與前端
- `MemoryStatus.gpu`（`name`、`percent`、`threshold`、`used_mb`、`total_mb`；沒有顯示卡時 `null`）、
  `MemoryModel.pool`、`MemoryEvent.pool`／`gpu_percent_before`／`gpu_percent_after`，都有預設值，舊的前端不會壞。
  `MemoryEvent.percent_*`、`threshold` 是**觸發的那個池**的數字。
- 前端：系統狀態頁多一條顯示記憶體長條、每個模型標「VRAM／系統記憶體」、釋放紀錄標「顯示記憶體」；
  頁首顯示「記憶體 98% · VRAM 90%」；智慧助理的系統狀態回答與排程頁的釋放通知也分池顯示。

## 結果（5070 Ti，2026-10-05）
- 系統記憶體 97%：只釋放 Chinese-CLIP、bge-m3（97.2% → 91.4%）。
- 系統記憶體 98.8%、Qwen3-VL 8B 已載入：**45 秒內沒有被卸載**，釋放紀錄是「沒有可釋放的模型，保留 Qwen3-VL」。
  修正前同樣的情況每 18 秒卸載一次。
- 單元測試（`tests/test_memory_gpu.py`）：系統記憶體滿不動 VRAM 模型、VRAM 滿只放 VRAM 裡沒在用的、
  預估算進自己的池、手動釋放兩池都放、`nvidia-smi` 解析與失敗。ADR 006 的單池測試照舊通過。

## 注意
- 顯示記憶體使用率是**整張卡**的（含遊戲、瀏覽器的硬體加速）。其他程式把 VRAM 吃滿時，會先釋放目前流程用不到的
  那個模型（例如問答時放掉 Ortho2CAD），目前流程正在用的不會被放；Ollama 自己在 VRAM 不夠時會把部分層放到 CPU，變慢但不會失敗。
- 只看第一張顯示卡；多張卡的主機要再擴充。
- AMD／Intel 顯示卡、Apple Silicon 不在範圍內（Apple 是統一記憶體，本來就該看系統記憶體）。
