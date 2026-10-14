# ADR 042：3D 重建結果快取＋品質閘門（知識庫圖紙）

- 日期：2026-10-14
- 狀態：採用（TASK-20261014-002）
- 相關：ADR 003（Ortho2CAD）、035（孔位校正）、041（背景工作）

## 背景
- 2026-10-14 實測：Ortho2CAD 生成佔 3D 重建總時間 93–96%，M5 Air 已到記憶體頻寬上限（約 23 tok/s），再怎麼調參數也快不了多少。
- 同一張 input.png 在 temperature 0 下輸出逐位元相同（mfg-001 重跑 code sha `9e23262c51`、IoU 0.27 完全重現）。直接快取等於把錯的結果永久固定下來，重跑也修不好。
- mfg-001 用 temperature 0.7、seed 1／2／3 得到 IoU 0.84／0.27／0.25（都可執行）：重抽候選能換出好結果。
- 各零件歷來最佳 IoU：mfg-002 0.917、mfg-004 0.865、mfg-006 0.724、mfg-003 0.64、mfg-001／005 約 0.3。

## 決定

### 1. 只快取「知識庫圖紙本身＋Ortho2CAD」
`strategy == "ortho2cad"` 且 `layout == "kb"`。照片（`rectified`／`upload`）每張像素都不同、對照組 `hybrid` 只是展示用，都不進快取。mock 模式或模型身分不明時不查也不寫。

### 2. key＝送進模型的一切
| 欄位 | 來源 |
|---|---|
| `input_sha256` | 實際送進模型的 input.png（前處理後） |
| `strategy` | `ortho2cad` |
| `model` | `deploy/llama-router.ini` 裡這個 alias 的 `model`、`mmproj`：路徑＋大小＋mtime（換 GGUF 或 mmproj 就不命中；檔案找不到＝身分不明） |
| `prompt`、`max_pixels`、`max_tokens` | `shared/models.yaml` 的 `cad` |
| `preprocess_version` | `app/cad/preprocess.PREPROCESS_VERSION`（改了裁切、二值化、縮放就加一） |

值：程式碼、IoU、候選來源（greedy 或 temperature＋seed）、建立時間、來源（auto／warm／adopted）。temperature 與 seed 不是 key，是「這份程式碼怎麼來的」。

### 3. 寫入閘門
自動寫入要全部成立：執行成功（`ok`）、實體有效（`valid`，ShapeFix 修過的不算）、孔位校正沒有失敗（圖紙上偵測到孔卻沒校正成，或偵測本身出錯；圖紙沒有孔不算失敗）、有標準模型（`gt/<id>.step`）而且 IoU ≥ `cad.cache.min_iou`（預設 0.6）。已經有一筆時只在 IoU 更高時換掉。

沒有標準模型的零件（照片建檔，ADR 013）沒有 IoU 可比，不自動寫入：主管（`kb_intake`）在 3D 重建頁按「採用這個結果」才寫入，記稽核。

### 4. 命中
查快取在 `require_part`／資料範圍檢查**之後**（看不到圖紙的身分不會觸發快取讀取）。命中就跳過生成，但程式碼照樣經過 `check_code` 與平台沙箱、重算 IoU 與孔位校正；重跑後再過一次閘門（有標準模型的重新比門檻，採用的只看基本檢查），不過就移除那一筆、改由模型重新產生。背景工作的命中路徑和一般工作一樣走暫存資料夾→原子發布→`published_attempt`，寫回前重驗權限，發布規則沒有改。

結果事件多一個 `cache`：`{"status": "hit"|"stored"|"not_stored"|"rejected_before", "reason", "created_at", "iou", "candidate", "adoptable", "regenerable"}`。畫面命中時顯示「沿用 <時間> 的結果・IoU x・候選來源」。

### 5. 結果不對，重新生成
- `POST /cad/jobs/{id}/cache/reject`：讀得到這個工作的人都可以作廢（記稽核）。記下程式碼雜湊：之後不命中、不寫入、不能採用。作廢的是「畫面上那一份」——已發布的 `code.py` 雜湊要和工作的候選紀錄一致。
- 接著 `POST /cad/jobs`（`regenerate: true`）：不查快取，用 `cad.resample.temperature`（0.7）＋下一個沒試過的 seed（1…`max_seeds`，預設 8）產生**一個**候選；全部試過回 `CAD_RESAMPLE_EXHAUSTED`。現場重建不自動重抽（一個候選就要 1–2 分鐘）。
- greedy 被否決後，一般重建仍會跑出同一份 greedy：結果標示「先前被否決」（`rejected_before`）、不寫入，提示重新生成。

### 6. `make cad-warm`
逐張有標準模型的知識庫零件：先跑一般重建（已有合格快取就命中），不合格就依序重抽 seed 1…`warm_seeds`（預設 3），每張重抽時間上限 `time_limit_s`（900 秒）；快取只在 IoU 更高時換掉，所以最後留下 IoU 最高且達門檻的那個。執行時像 worker 一樣在 Redis 登記 ortho2cad 使用中（memory_guard＋每 30 秒續約），不會被其他行程判斷記憶體吃緊而卸載。試過的 seed 記在快取裡，之後按「重新生成」從下一個 seed 開始。

### 7. 儲存
`data/cad_cache/cache.sqlite3`（WAL）：`entries`（快取）、`rejected`（被否決的雜湊）、`tried_seeds`、`runs`（每個工作用的是哪一份程式碼，作廢、採用用）。不在 `data/cad/` 底下；下載端點只認 `data/cad/<job_id>/` 與固定檔名（`JOB_FILES`），讀不到快取檔。

## 後果
- 同一張知識庫圖紙第二次重建只剩沙箱執行（約幾秒），不載入、不呼叫 Ortho2CAD。
- 錯的結果不會因為快取變成標準答案：沒過閘門的不寫；寫錯的（IoU 達標但人看了覺得不對）可以作廢，雜湊永久記住。
- 換模型、prompt、前處理都會自動失效，不需要手動清快取；舊的筆數留在檔案裡不影響正確性。
- 新錯誤碼：`CAD_CACHE_NOT_APPLICABLE`、`CAD_CACHE_NOT_ADOPTABLE`、`CAD_CACHE_EXISTS`、`CAD_RESAMPLE_EXHAUSTED`。

## 沒做
- 不做「3D 重建逐字顯示」（另一個提案，同樣改 `cad_service.py`／`cad_job.py`／`ReconstructPage.tsx`，不能同時做）。
- 照片與對照組不快取。
- 快取命中時不先跳過 memory_guard 的模型預留（要先算 key 才知道會不會命中；預留只影響記憶體釋放的判斷）。
