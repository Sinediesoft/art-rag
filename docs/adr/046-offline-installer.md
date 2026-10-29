# ADR 046：離線安裝包（完整功能、安裝過程不連網）

- 日期：2026-10-27
- 狀態：採用（`deploy/offline/`：`pack.ps1`、`install-offline.ps1`、`offline-env.ps1`、`bundle.py`、`wsl-setup.sh`）；
  入口從 `安裝.cmd`／`啟動.cmd`／`停止.cmd` 改成安裝精靈＋桌面程式，見 ADR 047（下面的資料夾結構是 payload 裡的內容）
- 編號：本分支最大是 045（dev 已有 042–044），接著用 046

## 背景
目標是「每個人都能輕鬆部署，裝完就能用全部功能」。一鍵安裝（`deploy/install.ps1`）要連網：winget 裝工具、`ollama pull`、
Hugging Face 下載 embedding、`uv sync`、`npm ci`；3D 重建還要另外裝 llama.cpp、Ortho2CAD 模型、WSL 裡的 CadQuery，
背景工作要 Docker 的 PostgreSQL 與 Redis。專題主打地端與資料隱私，展示現場、評審或組員的電腦不一定能連網，也不一定會用終端機。

## 決定
- **兩段**：在已經裝好、可以連網的電腦（5070 Ti 主機）跑一次 `deploy\offline\pack.ps1` 產生離線包資料夾；
  複製到別台 Windows 電腦後點兩下 `安裝.cmd`，**安裝過程不連網，全部功能都裝好**（畫作、圖紙、庫存、3D 重建、排程）
- **功能裝好、預設展示模式**（2026-10-29 使用者決定，取代 2026-10-27 的「權限不開」）：第一次寫 `.env` 時設
  `DEMO_CONTROLS=true`，裝好就能不用密碼切換 5 個身分、展示全部功能；只接受本機連線（`DEMO_TRUSTED_HOSTS`）。
  只在 `.env` 還不存在時設：使用者改回 false 之後，重裝新版不會被打開
- **用一個專用的 WSL 環境取代 Docker**：3D 重建產生的程式碼本來就要在 Linux 沙箱執行（ADR 027），
  所以 PostgreSQL＋pgvector、Redis 也放進同一個 WSL 環境 `artrag`，對方電腦不用 Docker Desktop。
  打包時從 Ubuntu 官方的 24.04 WSL 映像（核對 SHA-256）建一個暫時的環境，跑 `wsl-setup.sh` 裝好之後 `wsl --export`；
  和打包那台自己的 WSL 發行版完全分開，不會帶到個人檔案。帳密和 Docker 版相同（`artrag`，只綁本機）
- 離線包的結構：
  ```
  art-rag-offline\
    安裝.cmd 啟動.cmd 停止.cmd 使用說明.txt
    app\        git archive HEAD（不含 .env、data\）＋建置好的 frontend\dist＋models\ortho2cad＋scheduler\target\scheduler.jar
    offline\    offline.json（完成標記）、uv\、python\（可攜版 3.12）、uv-cache\（照 uv.lock 的套件）、
                ollama\、ollama-models\（qwen3-vl 8B＋4B）、hf\（bge-m3、Chinese-CLIP 釘選版本）、
                llama.cpp\（CUDA 版）、jdk\（Java 21）、wsl\artrag.tar（WSL 環境）＋WSL 離線安裝程式
    wsl-data\   安裝時匯入的 WSL 環境的虛擬磁碟
  ```
- 實測大小 25.5 GB：Ollama 模型 9.4 GB、Ortho2CAD 6.2 GB、bge-m3 2.3 GB、Ollama 1.9 GB、Chinese-CLIP 0.75 GB、
  llama.cpp 0.7 GB、Java 0.3 GB、WSL 環境與 Python 套件約 4 GB
- **只挑需要的檔案**（`bundle.py`，標準函式庫）：Ollama 只帶兩個模型的 manifest 與 blob；Hugging Face 只帶
  `shared/models.yaml` 釘選版本的 snapshot 與 `.no_exist` 記號（離線時 transformers 靠它跳過不存在的 safetensors）；
  Ollama 程式拿掉 AMD 的 ROCm（約 1 GB）、系統匣與解除安裝程式
- **Python 環境不直接打包**：虛擬環境裡寫死路徑、不能搬。改帶 uv 的套件快取，裝的電腦用 `uv sync`（`UV_OFFLINE`）重建；
  不設 `UV_FROZEN`：uv.lock 和 pyproject 一致時檢查不用連網，設了反而每次 `uv run --no-project` 都印一行警告
- **Ollama 用離線包自己的、連接埠 11435**：對方電腦原本裝的 Ollama（11434）不受影響，也不用把 9 GB 模型複製到它的資料夾；
  `.env` 的 `HYBRID_BASE_URL` 指到 11435，後端與記憶體管理本來就照這個位址找 Ollama
- **WSL2 的準備**：安裝時先看 BIOS 虛擬化（`HypervisorPresent` 或 `VirtualizationFirmwareEnabled`）與 `wsl --status`。
  沒有 WSL2 就以系統管理員權限（UAC 一次）用 dism 啟用「虛擬機器平台」「Windows 子系統 Linux 版」兩個內建功能、
  裝離線包裡的 WSL msi，請使用者重新開機後再點一次 `安裝.cmd`；全程不連網
- WSL 環境的服務由 `artrag-services`（PostgreSQL 在背景、Redis 在前景）帶起來：Windows 這邊有一個隱藏的 `wsl.exe` 行程
  帶著它，WSL 就不會把環境關掉；`-Stop` 停掉這個行程並 `wsl --terminate artrag`
- `offline-env.ps1`：`start-services.ps1`、`install-offline.ps1` 都 dot-source 它，偵測到 `..\offline\offline.json`
  才設環境變數（PATH、uv、`HF_HOME`、`OLLAMA_*`、`LLAMA_SERVER`），**只在這個行程與子行程**，不改系統設定；
  起停 WSL 環境的函式也在這裡
- `start-services.ps1`：離線包裡用 WSL 環境取代 `docker compose`；Ollama 連接埠照 `.env` 的 `HYBRID_BASE_URL`
  （本機位址才採用，否則照舊 11434）；mock 方案不預熱模型
- 故障診斷（ADR 045）一起帶。在離線包裡時，缺檔的修法改成「重新複製離線包」（沒有網路可以補下載），資料庫／Redis 的修法改成
  重啟 WSL 環境；另加「WSL2 不能用」「WSL 環境不見」「離線環境找不到套件」的規則

## 實測（2026-10-27，5070 Ti，`D:\art-rag-offline`）
- 安裝：匯入 WSL 環境、`uv sync` 115 個套件、建索引寫進 PostgreSQL，啟動後健康檢查全綠（Ollama、Ortho2CAD、Timefold、
  兩個 worker、WSL 的 CadQuery）
- 問答：七段流程全部地端、外送 0 bytes；3D 重建 mfg-002 35 秒、IoU 0.917、孔位校正 7 個；排程 Timefold 17 秒、0 hard
  （3D 重建與排程要權限：只在這次測試裝的 `.env` 暫時開 `DEMO_CONTROLS=true` 切成主管，測完改回 false）
- 全部停止後冷啟動 60 秒；停止時只停離線包自己的服務，這台原本的 Ollama 不受影響
- 沒測到：「這台沒有 WSL2」的啟用流程（這台本來就有）

## 不包含
- macOS、Linux、AMD／Intel 顯示卡：沒有 NVIDIA 顯卡時生成模型是 mock 方案，3D 重建會用 CPU（很慢）
- 對外開放：資料庫、Redis、後端都只綁本機

## 注意
- 離線包綁定打包時的 commit；要包新功能就重新打包
- 隨身碟要 exFAT 或 NTFS：8B 模型單一個檔案超過 4 GB
- 要移除：先點「停止」，執行 `wsl --unregister artrag`，再刪整個資料夾
- 沒有程式碼簽章：第一次點 `安裝.cmd` 時 SmartScreen 會警告，要按「其他資訊」→「仍要執行」
- 打包會轉散布 Ollama（MIT）、Qwen3-VL（Apache 2.0）、bge-m3（MIT）、Chinese-CLIP、Ollama 與 llama.cpp 附的 NVIDIA CUDA 函式庫、
  Microsoft OpenJDK（GPLv2＋CE）、Ubuntu 與其套件；只在組內與展示使用，對外散布前要再確認授權
