# ADR 046：離線安裝包（安裝過程不連網）

- 日期：2026-10-27
- 狀態：採用（`deploy/offline/`：`pack.ps1`、`install-offline.ps1`、`offline-env.ps1`、`bundle.py`、`安裝.cmd`／`啟動.cmd`／`停止.cmd`）
- 編號：本分支最大是 045（dev 已有 042–044），接著用 046

## 背景
目標是「每個人都能輕鬆部署」。一鍵安裝（`deploy/install.ps1`）要連網：winget 裝工具、`ollama pull` 下載模型、
Hugging Face 下載 embedding、`uv sync`、`npm ci`，還要自己開 PowerShell 打指令。專題主打地端與資料隱私，
展示現場、評審或組員的電腦不一定能連網，也不一定會用終端機。

## 決定
- **兩段**：在已經裝好、可以連網的電腦（5070 Ti 主機）跑一次 `deploy\offline\pack.ps1` 產生離線包資料夾；
  複製到別台 Windows 電腦後點兩下 `安裝.cmd`，**安裝過程不連網**
- 離線包的結構：
  ```
  art-rag-offline\
    安裝.cmd 啟動.cmd 停止.cmd 使用說明.txt
    app\        git archive HEAD（不含 .env、data\）＋建置好的 frontend\dist（裝的電腦不用 Node.js）
    offline\    offline.json（完成標記）、uv\、python\（可攜版 3.12）、uv-cache\（照 uv.lock 的套件）、
                ollama\（程式）、ollama-models\（qwen3-vl 8B＋4B）、hf\（bge-m3、Chinese-CLIP 釘選版本）
  ```
- 實測大小（這台的資料）：Ollama 程式 1.9 GB、模型 9.4 GB、bge-m3 2.3 GB、Chinese-CLIP 0.75 GB，加 Python 套件約 17.5 GB
- **只挑需要的檔案**（`bundle.py`，標準函式庫）：Ollama 只帶兩個模型的 manifest 與 blob；Hugging Face 只帶
  `shared/models.yaml` 釘選版本的 snapshot 與 `.no_exist` 記號（離線時 transformers 靠它跳過不存在的 safetensors）；
  Ollama 程式拿掉 AMD 的 ROCm（約 1 GB）、系統匣與解除安裝程式
- **Python 環境不直接打包**：虛擬環境裡寫死路徑、不能搬。改帶 uv 的套件快取，裝的電腦用 `uv sync`
  （`UV_OFFLINE`、`UV_FROZEN`）重建；快取和環境在同一顆磁碟時 uv 用硬連結，不會再佔一份空間
- **Ollama 用離線包自己的、連接埠 11435**：對方電腦原本裝的 Ollama（11434）不受影響，也不用把 9 GB 模型複製到它的資料夾；
  `.env` 的 `HYBRID_BASE_URL` 指到 11435，後端與記憶體管理本來就照這個位址找 Ollama
- `offline-env.ps1`：`start-services.ps1`、`install-offline.ps1` 都 dot-source 它，偵測到 `..\offline\offline.json`
  才設環境變數（PATH、uv、`HF_HOME`、`OLLAMA_MODELS`、`OLLAMA_HOST`），**只在這個行程與子行程**，不改系統設定
- `start-services.ps1` 的 Ollama 連接埠改成照 `.env` 的 `HYBRID_BASE_URL`（本機位址才採用，否則照舊 11434）；
  離線包的 `-Stop` 會一起停掉自己起的 Ollama；mock 方案不預熱模型
- 安裝完在桌面建「啟動 art-rag」「停止 art-rag」捷徑；`.cmd` 以 CRLF 存（`.gitattributes`）
- 故障診斷（ADR 045）一起帶：uv 與 Python 在包裡，安裝一開始就能比對規則表；Ollama 起來後才能請模型解釋。
  在離線包裡時，「HF 快取缺檔、模型沒下載、找不到 Ollama／uv」的修法改成「重新複製離線包」（沒有網路可以補下載），
  另加一條「離線環境找不到套件」的規則

## 不包含
- Docker（PostgreSQL、Redis）：用檔案索引＋SQLite，問答、圖紙、庫存都能用；3D 重建與排程的背景工作不能用
- Ortho2CAD（llama.cpp）、Timefold 排程（Java）：原本就是選裝
- macOS、AMD／Intel 顯示卡：沒有 NVIDIA 顯卡時是 mock 方案（只看介面與流程）

## 注意
- 離線包綁定打包時的 commit；要包新功能就重新打包
- 隨身碟要 exFAT 或 NTFS：8B 模型單一個檔案超過 4 GB
- 沒有程式碼簽章：第一次點 `安裝.cmd` 時 SmartScreen 會警告，要按「其他資訊」→「仍要執行」
- 打包會轉散布 Ollama（MIT）、Qwen3-VL（Apache 2.0）、bge-m3（MIT）、Chinese-CLIP 與 Ollama 附的 NVIDIA CUDA 函式庫；
  只在組內與展示使用，對外散布前要再確認授權
