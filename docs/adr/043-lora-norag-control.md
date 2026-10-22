# ADR 043：LoRA 不檢索對照組（lora_norag）

- 日期：2026-10-21
- 狀態：採用（TASK-20261021-001）
- 編號：本分支（d-rag-front-end）最大是 041，但 dev 已有 `042-cad-result-cache.md`，為了日後合併不撞號用 043
- 相關：ADR 024（問答附圖）、029（畫作區域）、030（評估模式 EVAL_CONTROLS）、014／015（七段防護、Metadata Filter）

## 背景

- 企劃書把 LoRA 列為選做。生成端早就留了 `lora` 插槽（`providers.py`、`LORA_*` 設定、`models.yaml` 的 `lora` 段），但一直沒有模型，評估也沒有「LoRA 關檢索」的組合。
- 要回答的問題是：把畫作知識訓練進權重，能不能取代檢索？這是**負面對照**：預期答案是「不能」，要用數字說明。
- 過去「檢索增益 +69 個百分點」是 8B 量出來的。這次三組統一用 Qwen3-VL 4B 重量，避免把模型大小的差異算成檢索的效果。

## 決定

### 1. 三組比較，同一個模型、同一個 llama-server

| 策略 | 模型 | 檢索 |
|---|---|---|
| `hybrid` | art-base（底模） | 開 |
| `hybrid_norag` | art-base（底模） | 關 |
| `lora_norag` | art-lora（LoRA 微調） | 關 |

- 三組都是 Qwen3-VL-4B-Instruct、GGUF Q4_K_M、temperature 0，掛在同一個 llama-server :8081 的 router（`deploy/llama-lora.ini`），只差模型別名。
- art-base 和 art-lora 走**同一條轉檔流程**（第 4 點），避免 Ollama 與 llama-server 量化方式不同造成的差異。主方案平常用的 Ollama `qwen3-vl:4b-instruct` 不參與比較。
- 不做 LoRA＋檢索，之後另開任務。

### 2. 訓練資料只用畫作（`pipelines/make_lora_data.py`）

- **來源**：`kb/artworks` 的 3 幅畫，包括作品卡（JSON 的 `title`、`artist`、`date_text`、`medium`、`dimensions`、`collection`、`style_tags`）、descriptions、regions（區域標籤、位置，以及綁在該區域的段落）。
- **排除「（未審核）」段落**：〈有絲柏的麥田〉的「觀眾留言（未審核）」其實是要助理洩漏內部電話與庫存的指令（ADR 014 的汙染資料）。檢索時由第 2、4 段防護處理；但訓練進權重之後就沒有任何一段攔得住，所以不能用。實際用了 14 段 descriptions。
- **題型比例**（約 75／15／10）：
  - fact：每段 20–30 種問法，由地端模型依段落產生問與答。答案的字元 3-gram 至少 35% 要出現在段落裡，否則丟掉，擋下模型自己加料的答案；
  - refusal：問知識庫沒有記載的事，例如保養、展廳溫度、畫筆數量，固定回「知識庫中沒有這方面的資料。」。題目的關鍵詞出現在該畫作 JSON 裡就不出這題；
  - retain：一般藝術常識，答案由底模自己產生，用來保留原本的能力。碰到遺忘評估 `qa_general.jsonl` 的作品名或答案就不用，也避開知識庫三位畫家。
- **格式和推論逐字相同**：每筆都呼叫 `prompt.build_messages(..., use_retrieval=False)`。圖是 `data/lora/images/<id>.jpg`，和後端送給生成端的是同一份位元組（`to_jpeg_bytes(load_image(...))`，長邊 1024 px）。單元測試把樣本換回推論格式，和 `build_messages` 的輸出比對。
- **外洩檢查**：和 `eval/qa*.jsonl` 全部題目比字元 3-gram Jaccard，超過 0.5 的刪掉，報告記錄刪掉幾題、最高 Jaccard 是多少。不用 `eval/photos/`。
- **只准用地端模型**：產生資料的端點必須是本機或內網位址，不能用雲端 API。
- **輸出**：寫到 `data/lora/`（不進 Git），`review.jsonl` 是兩成抽樣，給組員 D 抽查。

### 3. 機密資料不能進權重

工廠領域（`kb/parts`、`kb/drawings`、`kb/cad`、庫存、排程）完全不進訓練資料。原因有兩個：
- 第 3 段 Metadata Filter 是在檢索時依文件的機密等級與資料範圍過濾。訓練進權重的知識沒有文件邊界，任何人只要問到就答得出來，Filter 擋不住；
- 權重也無法依帳號撤銷或更新。

這是本實驗的結論之一：**機密資料只能放在檢索端**。

### 4. 訓練與轉檔（`pipelines/lora/`，獨立 uv 環境，不進 CI）

在組員 D 的 MacBook Air M5 16 GB 上跑：

- **底模**：`mlx-community/Qwen3-VL-4B-Instruct-4bit`（固定 revision）。它是以 mlx-vlm 從官方 Qwen3-VL-4B-Instruct 量化的版本：語言層 affine 4-bit、group 64，視覺層保留原精度。
  - mlx-vlm 訓練時的 `--quantization-bits` 會連視覺編碼器一起量化，但推論時用的是原精度的 mmproj。所以改用只量化語言層的版本，讓訓練與推論看到相同的影像特徵。
  - 官方 bf16 權重約 9 GB，這台 Mac 的網路約 300 KB/s；4-bit 版只有 3.1 GB，量化結果和自己用 `mlx_vlm convert -q` 相同。
- **QLoRA**：r=16、alpha=32、dropout 0.05；只調語言層的 q/k/v/o/gate/up/down，視覺編碼器凍結；batch 1、梯度累積 16、gradient checkpointing、lr 2e-4、3–5 epochs，只對回答算 loss。記憶體不夠時用 `--image-size` 把訓練圖縮到 448–672 px。
- **挑 epoch**：每個 epoch 存一次 adapter，用 `eval/qa.jsonl` 的 dev 題（`data/lora/select.jsonl`）在 MLX 上回答。答對最多的勝出，同分取較早的。test 只在 `make eval-lora` 跑一次。
- **匯出**（`export.py`）：
  - art-lora：掛 adapter → fuse 時 de-quantize → 其餘量化層 de-quantize；
  - art-base：不掛 adapter，直接 de-quantize；
  - 之後語言層換回 HF 權重名稱，設定檔與 tokenizer 用官方原版，以 llama.cpp `convert_hf_to_gguf.py`（b11146，和 brew 的 llama-server 同版）轉 bf16 GGUF，再 `llama-quantize` 成 Q4_K_M；
  - 影像編碼器沿用 Qwen 官方 GGUF 的 `mmproj-Qwen3VL-4B-Instruct-F16.gguf`，兩個模型共用。
- **第一步先跑通**：`make -C pipelines/lora smoke` 用 10 筆資料走完「訓練 → fuse → GGUF → llama-server 回答」。

### 5. 評估（`make eval-lora`）

三步，結果都寫到 `eval/runs/`：

1. **問答與亂編率**：`qa.jsonl`，跑 `hybrid,hybrid_norag,lora_norag`，加 `--outside-kb`。
   - 正確率看 test 的一般題（`by_split_type.test.untyped`）；dev 只用來挑 epoch。
   - 亂編率看 `no_answer` 題，以及問知識庫外畫作 `npm-000002`、`aic-16568` 的題目。這些題目不帶畫作、不附圖送出（附圖會在辨識階段就回「知識庫中沒有這幅畫」，三組都一樣，量不到模型）。
   - 亂編的判定：拒答（含生成閘門的「查無資料」），或答對常識，都不算亂編；其他回答都算亂編。
2. **遺忘**：`qa_visual.jsonl`（6 題看圖題）加 `qa_general.jsonl`（20 題一般藝術常識，避開知識庫三位畫家），只比 `hybrid_norag` 與 `lora_norag`。
3. **知識更新**（`eval/run_lora_update_eval.py`）：
   - 在暫存副本把 5 個事實換掉：雨點皴→斧劈皴、1958→1966、第八屆→第五屆、1993→1987、埃及方尖碑→希臘神殿的石柱；
   - 重建索引，在 :8011 另開後端（清空資料庫與佇列設定、關掉記憶體管理），問這 5 題；
   - 預期 hybrid 跟上新答案，`lora_norag` 還是講舊答案；腳本會先確認副本後端讀到的是改過的知識庫。

**第 7 段接受 `[0]`**：關閉檢索時 prompt 要模型在句尾標 `[0]` 表示無來源，但第 7 段輸出檢查原本只接受 1 到 N 的引用編號，照 prompt 回答的不檢索回覆全部被當成「引用了沒提供的編號」擋下（第一次實跑時，hybrid_norag、lora_norag 有呼叫模型的題目全部被擋）。`guard.check_output` 加上 `no_source_mark`：只有關閉檢索時 `[0]` 才合法，開檢索時照舊擋。這也修好了既有 `make eval` 的 hybrid_norag。

**落到備援的題目一律記錯**：done 事件的 `fallback` 為 true（實際改用了 `FALLBACK_CHAIN` 的模型），答案就不是這個策略產生的，記為錯誤、不判亂編。`lora` 開頭的策略只要有一題落到備援或出錯，`make eval-lora` 就失敗（結束碼 3）。評估本來就送 `allow_fallback: false`，這是第二道保險。

**延遲與資源**：
- P95 首字時間與總時間來自 `run_eval` 的摘要；
- 訓練時間、尖峰記憶體（MLX 回報）、adapter 大小來自 `train_report.json`；
- 三個 GGUF 的 SHA-256 與 llama-server 版本由 `make -C pipelines/lora info` 寫到 `models/lora/model_info.txt`。

### 6. 接法（不改 `.env`，生成端設定集中在 Makefile 的 `LORA_EVAL_ENV`）

```sh
make -C pipelines/lora serve   # 終端機 1：llama-server :8081（art-base、art-lora）
make eval-lora-backend         # 終端機 2：後端 :8000
make eval-lora
```

- `eval-lora-backend` 與知識更新的副本後端用同一組 `LORA_EVAL_ENV`。副本後端照呼叫端的環境變數設定生成端；第一次實跑時只有 :8000 帶了設定，副本後端就讀到 `.env` 的 Ollama 預設，lora 全部出錯（結束碼 3 攔下）。
- 評估用後端清空 `DATABASE_URL`、`JOBS_DATABASE_URL`、`REDIS_URL`（用 SQLite，不需要 PostgreSQL／Redis），並設 `MEMORY_GUARD=false`，避免記憶體管理卸載 llama-server 的模型。
- `EVAL_INJECTION=true` 讓 hybrid 的干擾段落題能注入；沒開的話這些題目會回 FORBIDDEN、記成錯誤。
- `MODEL_KEEP_ALIVE` 有設定時，lora 與 hybrid 會在請求裡多送 `keep_alive`（Ollama 的欄位）。已用 `pipelines/lora/ask.py --keep-alive 60m` 確認 llama-server 不會因此出錯（結果見下方「結果」）。

## 結果

2026-10-22 在組員 D 的 MacBook Air M5 16 GB 上跑完。`eval/runs/` 的輸出不提交，檔名、三個 GGUF 的 SHA-256 與 llama-server 版本附在 Implementation Report 的證據目錄。

**訓練**：train 488 筆、valid 54 筆（fact 406、refusal 82、retain 54；外洩檢查刪 2 題，留下的最高 Jaccard 0.5），4 epochs 共 1952 步，171.8 分鐘，MLX 尖峰記憶體 13.5 GB，adapter 132 MB。dev 題 epoch 1–4 為 3、4、6、6／6，選 epoch 3。

| 指標 | hybrid | hybrid_norag | lora_norag |
|---|---|---|---|
| 正確率（test 一般題，12 題） | **100%** | 17% | 75% |
| 干擾段落題（test，12 題） | 100% | 8% | 50% |
| 亂編率（no_answer 1 題＋知識庫外畫作 4 題） | **0／5** | 3／5 | 5／5 |
| 知識更新：跟上新答案／還講舊答案（5 題） | **5／0** | 0／0 | 0／5 |
| 遺忘：一般常識（20 題） | — | 70% | 25% |
| 遺忘：看圖題（6 題） | — | 83% | 83% |
| P95 首字／完整回答 | 4.9／6.5 秒 | 2.1／2.2 秒 | 2.3／2.8 秒 |

**解讀**：
- LoRA 記住了大部分畫作事實（不檢索時 17% → 75%），但沒有一題比得上檢索（100%），而且沒有引用可以查證；
- 不知道的事也照答：知識庫外的畫作與知識庫沒寫的問題，lora_norag 5 題全部編了答案。訓練資料裡的 refusal 題沒有讓它學會拒答，反而把知識庫的詞彙帶到別的畫上（例如說莫內在「聖保羅療養院」畫睡蓮）；
- 改了知識庫，hybrid 重建索引就 5 題全部跟上；lora_norag 5 題都還講舊答案，要重新訓練才會變；
- 一般常識從 70% 掉到 25%，答錯的多半是畫家名字（〈清明上河圖〉→李唐、〈雅典學院〉→米開朗基羅）。其中 1 題是答「達·芬奇」，被以「達文西」為答案的評分算錯；不算這題也是明顯變差。retain 題只有 54 筆，不足以保住原本的常識；
- 不檢索的兩組比較快，因為 prompt 沒有參考資料、也不跑檢索。

結論和預期一致：**權重裡的知識不能取代檢索**，尤其在拒答、更新與遺忘三方面。

**解讀範圍**：資料只有 3 幅畫、18 段（作品卡、14 段 descriptions、區域），每項只有 5–20 題，結果只證明流程能運作、方向如預期，不作為一般結論。

## 後果

- 評估多了 `lora_norag` 策略與 `--outside-kb`、`--qa`、`--skip-images` 選項；`make eval` 等既有指令的行為不變（不加 `--outside-kb` 時，知識庫外畫作的題目照舊跳過）。
- 主方案 hybrid 的預設模型與 prompt 不變。
- 模型權重、GGUF、訓練資料都不進 Git（`models/`、`data/`）。`eval/runs/` 本來就有追蹤，但本實驗的評估輸出只附在 Implementation Report 的證據目錄，不提交。
- 已知限制：
  - art-base 是 4-bit 再 de-quantize 後轉 Q4_K_M，比直接從 bf16 轉 Q4_K_M 多一次量化誤差。三組都用 art-base／art-lora，比較仍然公平，但絕對分數可能略低於主方案的 Ollama 模型；
  - retain 題的答案由底模產生，底模答錯的常識也會一起學進去。這是保留原本行為的代價，所以遺忘評估比的是「和底模比有沒有變差」，不是絕對正確率。
