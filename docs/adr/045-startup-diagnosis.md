# ADR 045：啟動失敗時的故障診斷（規則表優先、本地模型解釋）

- 日期：2026-10-27
- 狀態：採用（`deploy/diagnose.py`；`deploy/windows/start-services.ps1`、`deploy/install.ps1`、`deploy/install.sh` 失敗時呼叫）
- 編號：本分支最大是 041，但 dev 已有 042–044，為了日後合併不撞號用 045

## 背景
一鍵安裝與一鍵啟動失敗時，只印一句「看 data\logs\xxx.log」。要自己打開 log 找原因，而常見原因其實就那幾種：
Docker Desktop 沒開、連接埠被佔、記憶體不夠（遊戲、WSL）、`HF_OFFLINE=true` 但模型沒下載、Redis 沒起來、
WSL 的 CadQuery 環境壞了。還有 PowerShell 5.1 的坑：`*>` 寫出 UTF-16 的 log，docker 寫到 stderr 的正常訊息
被包成 `NativeCommandError`，看起來像錯誤。

## 決定
- 新增 `deploy/diagnose.py`，只用標準函式庫，用 `uv run --no-project python` 執行：後端套件壞了也能跑
- 只看**這次執行之後**寫過的 `data/logs/*.log`（腳本傳 `--since`），每個取最後 80 行；依 BOM 判斷 UTF-16／UTF-8，
  濾掉 PowerShell 包在外面的 `At …:N char:N`、`+ …` 那幾行
- **先比對規則表**（這台主機實際踩過的 13 種狀況，原因＋修法＋依據哪一行）；對到就不問模型
- 規則對不上才把「像錯誤的行」交給本地模型（`.env` 的 `HYBRID_BASE_URL`，OpenAI 相容 `/v1/chat/completions`，
  Ollama 和 Strata 都能用；先 `HYBRID_MODEL`，失敗換 `HYBRID_FALLBACK_MODEL`）。回答標「模型推測，未驗證」
- 模型叫不到（壞掉的正好是 Ollama）、mock 方案、`--no-llm`：只列出 log 裡像錯誤的最後 20 行
- 結束碼一律 0：診斷不改變呼叫它的腳本成功或失敗
- 串接：
  - `start-services.ps1`：腳本層級的 `trap` 接中途 throw；健康檢查有 ✗（Ollama 沒回應、後端沒回應、
    hybrid 模型不可用、WSL 的 CadQuery 不能用、worker 沒登記）時結尾再診斷一次；`-NoDiagnose` 關掉
  - `install.ps1`：`trap`；啟動那一步失敗時 `start-services.ps1` 已經診斷過，不重複
  - `install.sh`：`set -E`＋`trap … ERR`，後端沒起來的那條路徑另外呼叫

## 資料不出本機
- 模型位址用和 `backend/app/rag/providers.py` 的 `is_local_url` 同一套判斷：本機、私有網段、Tailscale、內網主機名稱才送；
  其他位址一律不送 log，只列出錯誤行
- 送出與印出前遮掉 `.env` 裡名稱含 KEY／SECRET／PASSWORD／TOKEN 的值（和 `.env.example` 一樣的公開預設值不遮，
  不然容器名稱 `artrag-db-1` 也會被遮掉）、網址裡的帳密、JWT
- 只印建議，**不執行任何指令**：模型的回答可能是錯的，修不修由使用者決定

## 為什麼不全部交給模型
- 壞掉的常常就是模型服務本身（Ollama 沒開、記憶體不夠），這時只剩規則表能用
- 常見狀況規則表答得又快又準，模型（8B）讀 log 偶爾會編出不存在的設定名稱

## 已知限制
- 規則表只收這台 5070 Ti 主機遇過的狀況，macOS／Linux 特有的錯誤多半要靠模型或自己看 log
- `install.sh` 沒有 `data/logs` 以外的安裝紀錄：中途失敗主要靠 `--message`（哪一行、哪個指令）判斷
