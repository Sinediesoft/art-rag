# 5070 Ti 推論主機操作手冊（Windows 11＋WSL2）

全組最強的硬體：RTX 5070 Ti 16 GB、記憶體 32 GB。負責：C（模型）。實測數字見 README「5070 Ti 主機（Qwen3-VL 8B）」。

| 服務 | 埠 | 說明 |
|---|---|---|
| Ollama | 11434 | 主力 `qwen3-vl:8b-instruct`（Q4）、備援 `qwen3-vl:4b-instruct` |
| Ortho2CAD | 8081 | llama.cpp **Windows CUDA 版** `llama-server`（router 模式，`deploy/llama-router.ini`），約 122 token/s |
| Timefold 排程 | 8082 | Microsoft OpenJDK 21 |
| 後端＋前端 | 8000 | `uv` 啟動 uvicorn，前端是 `frontend/dist` 建置檔 |

## 開機後：一個指令啟動全部

```powershell
powershell -ExecutionPolicy Bypass -File deploy\windows\start-services.ps1
```

已經在跑的會略過，最後印出健康檢查；`-Stop` 停掉（Ollama 除外）。紀錄在 `data\logs\`。

這台的 `.env`（不進 Git）和 `.env.example` 不同的地方：

```
HYBRID_MODEL=qwen3-vl:8b-instruct
HYBRID_FALLBACK_MODEL=qwen3-vl:4b-instruct
HF_OFFLINE=true
MEMORY_HIGH_PCT=92          # 這台常同時開其他大型程式；VRAM 另外看（ADR 021）
EVAL_INJECTION=true         # 評估主機才開（ADR 019）
MODEL_KEEP_ALIVE=60m        # 閒置 60 分鐘才卸載，避免冷啟動首字 6–16 秒（ADR 025）
DATABASE_URL=               # 還沒裝 Docker：檔案索引＋SQLite
```

## 第一次安裝（已做過，換電腦時參考）

1. `winget install OpenJS.NodeJS.LTS Microsoft.OpenJDK.21`，`uv` 另裝；`cd backend; uv sync --compile-bytecode`、`cd frontend; npm ci; npm run build`
2. `ollama pull qwen3-vl:8b-instruct`、`ollama pull qwen3-vl:4b-instruct`
3. `cd backend; uv run python ..\pipelines\build_index.py`（第一次會下載 Chinese-CLIP、bge-m3）
4. 排程：`uv run --project backend python pipelines/setup_scheduler.py`
5. Ortho2CAD：
   - llama.cpp 從官方 GitHub release 下載 **`llama-bXXXXX-bin-win-cuda-13.x-x64.zip` 與 `cudart-llama-bin-win-cuda-13.x-x64.zip`**
     （核對 release 頁的 SHA-256），解壓到 `%USERPROFILE%\tools\llama.cpp-bXXXXX\`。winget 的 `ggml.llamacpp` 是 Vulkan 版，比較慢
   - 模型：`uv run --project backend python pipelines/setup_ortho2cad.py`（約 6.2 GB；`LLAMA_CPP_TAG` 設成和 llama-server 同版）

## 3D 重建評估：Windows 產生程式碼、WSL 算 IoU

Windows 限制不了子行程，**不能執行模型產生的 CadQuery 程式碼**（`app/cad/sandbox.py`），但 GPU 推論在 Windows 最快。
所以拆兩半，算法和 `make eval-cad` 相同（同一個 `extract_code`→`run_cad`、同一份標準模型 STEP）：

```powershell
# Windows：產生程式碼 → data\cad_codes\<label>\
uv run --project backend python eval/cad_generate.py ortho2cad
uv run --project backend python eval/cad_generate.py qwen3-vl:8b-instruct --baseline
```

```bash
# WSL（Ubuntu 24.04）：一次性準備只有 CadQuery 的環境（不碰 repo 裡 Windows 的 .venv）
uv venv ~/artrag-cad --python 3.12
uv pip install --python ~/artrag-cad/bin/python cadquery==2.8.0 cadquery-ocp==7.9.3.1.1 numpy pillow opencv-python-headless
# 計分 → eval/runs/experiments/<run_id>-cadscore.json
cd /mnt/c/Users/<你>/Desktop/art-rag && ~/artrag-cad/bin/python eval/cad_score.py ortho2cad qwen3-vl_8b-instruct
```

驗證：連接法蘭這台產生的程式碼和 Mac 逐字相同，算出的 IoU 0.814 和 Mac 的 `make eval-cad`（0.81）一致；
4B 未微調對照組也和 Mac 一樣（可執行 17%、平均 IoU 0.07）。

WSL 預設 NAT 網路連不到 Windows 的 `127.0.0.1`，所以後端與推論都留在 Windows，WSL 只做執行與計分。

結果（2026-10-05）：Ortho2CAD 可執行 100%、平均 IoU 0.46；8B 未微調 33%、0.18；4B 未微調 17%、0.07。

**注意**：`cad_generate.py` 直接打 llama-server、不經過後端，後端的記憶體管理不知道 Ortho2CAD 正在用。
VRAM 超過門檻時會把載入中的 Ortho2CAD 卸載，請求回 500（實際發生過：WSL 的虛擬機把系統記憶體吃到 99%，
Ollama 的 8B＋4B 又佔著 VRAM）。腳本產生 Ortho2CAD 前會先請 Ollama 卸載模型；仍失敗時先 `wsl --shutdown` 釋放 WSL 的記憶體再跑。

## 開放給組員（Tailscale，待登入後設定）

組員用自己的電腦跑後端，只把推論交給這台：組員 `.env` 設
`HYBRID_BASE_URL=http://100.x.y.z:11434/v1`（`providers.py` 只接受本機、私有網段與 Tailscale 位址）。

1. 安裝 Tailscale（已裝），**用小組 tailnet 的帳號**登入；`tailscale ip -4` 查到 `100.x.y.z`
2. Ollama 對外聽：使用者環境變數 `OLLAMA_HOST=0.0.0.0:11434`，重開 Ollama
3. Windows 防火牆只允許 Tailscale 網段連 11434（系統管理員）：
   `New-NetFirewallRule -DisplayName "Ollama (Tailscale only)" -Direction Inbound -Protocol TCP -LocalPort 11434 -RemoteAddress 100.64.0.0/10 -Action Allow`
4. Ortho2CAD 也要給組員用時：`llama-server` 改 `--host 0.0.0.0` 並加 `--api-key`（組員的 `ORTHO2CAD_API_KEY` 填同一組），
   防火牆同樣只開 8081 給 `100.64.0.0/10`。llama-server 的 router 模式沒有金鑰時任何人都能載入／卸載模型
5. 組員端測試：`curl http://100.x.y.z:11434/api/tags`

注意：組員連過來時，這台的記憶體管理不會替他們卸載模型（推論伺服器不在組員本機，ADR 006／021）；
同時有人問答與 3D 重建會搶 GPU。
