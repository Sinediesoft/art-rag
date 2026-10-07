# ADR 027：Windows 主機的 3D 重建程式碼改到 WSL2 執行

- 日期：2026-10-07
- 狀態：採用（每台 Windows 主機用 `.env` 的 `CAD_WSL_PYTHON` 決定；沒設時行為不變）

## 背景
沙箱（`app/cad/sandbox.py`）執行模型產生的 CadQuery 程式碼時，靠 Linux／macOS 的 `resource` 模組限制
子行程的 CPU 時間與輸出檔大小。Windows 沒有這個模組，所以**模型產生的程式碼一律拒絕執行**。

5070 Ti 是展示主機，後端直接跑在 Windows（Ollama、llama.cpp 的 CUDA 版都在 Windows 最快），結果：
- 展示腳本的「3D 重建」在這台做不了（回「這台電腦（Windows）限制不了子行程…」）；
- `make demo-test` 一直略過第 3 步，`tests/test_drawings.py` 的 3 個沙箱測試在這台跳過；
- IoU 只能拆兩半量（Windows 產生程式碼、WSL 計分，`eval/cad_generate.py`＋`eval/cad_score.py`）。

這台已經裝了 WSL2（Ubuntu 24.04），也有只裝 CadQuery 的環境 `~/artrag-cad`（拆兩半計分時建的）。

## 選項
1. **整個後端搬進 WSL**：WSL 預設 NAT 網路連不到 Windows 的 `127.0.0.1`（Ollama、llama-server、資料庫都在那），
   要改 mirrored 網路或讓推論伺服器對外聽，動到整台的部署。
2. **Windows Job Object** 限制 CPU 時間與記憶體：限制不了輸出檔大小、也沒有網路隔離，要另外維護一套 Windows 專用的程式。
3. **只把「執行」這一步交給 WSL**：後端照舊在 Windows，執行時用 `wsl.exe` 在 Linux 裡跑同一支 `app/cad/runner.py`。

採用 3：防護和 Linux 主機完全相同（同一支 runner、同樣的 `resource` 限制），還多了網路隔離；改動只在沙箱。

## 決定
- `.env` 新增 `CAD_WSL_PYTHON`（WSL 裡裝好 CadQuery 的 Python）、`CAD_WSL_DISTRO`（留空＝預設發行版）。
  只有「沒有 `resource` 模組（Windows）＋模型產生的程式碼＋有設 `CAD_WSL_PYTHON`」才走 WSL；
  知識庫自己的標準模型（`trusted`，`make index` 用）照舊在 Windows 執行；macOS、Linux 忽略這個設定。
- 執行指令：`wsl.exe -d <發行版> --cd /mnt/c/.../backend --exec`
  1. `timeout -k 5 <秒數+10>`：Linux 裡自己計時，時間到一定結束，不靠 Windows 這邊殺掉 `wsl.exe`
  2. `unshare -rn`：新的網路命名空間，沒有網路（實測連外回 `Network is unreachable`）
  3. `env -i`：不帶任何環境變數，只給 `PATH`、`PYTHONPATH`、`HOME`／`TMPDIR`＝工作目錄；Windows 這邊也拿掉 `WSLENV`
  4. `<CAD_WSL_PYTHON> -m app.cad.runner`：和 Linux 主機同一支，AST 白名單（`check_code`）照樣先在 Windows 這邊檢查
- 工作 JSON 裡的路徑換成 `/mnt/<磁碟機>/...`（WSL 預設掛載點），結果檔直接寫回 Windows 的工作目錄。
- Windows 這邊的逾時改成 `執行上限＋30 秒`（WSL 沒在執行時要先開機）。
- **不留 core dump**：程式碼被 CPU 時間限制砍掉時，WSL 預設把當掉的行程傾印成約 **370 MB** 的檔案存到
  `%LOCALAPPDATA%\Temp\wsl-crashes`（實測發生過）。WSL 的 `core_pattern` 是交給程式處理（開頭是 `|`），
  核心這時不看 `RLIMIT_CORE`，所以 runner 在 Linux 另外呼叫 `prctl(PR_SET_DUMPABLE, 0)`；Linux、macOS 也一併把 `RLIMIT_CORE` 設 0。
- 超過 CPU 時間（`SIGXCPU`）時 runner 自己寫出「執行超過 CPU 時間限制」再結束；逾時發生在 `import cadquery` 途中時，
  例外會變成看不懂的 `ImportError`，一律改說逾時。程式碼自己吞掉這個例外時，硬限制（+5 秒）的 `SIGKILL` 結束它，
  `wsl.exe` 回 9、`timeout` 回 124／137、直接執行回 -9，都顯示「執行逾時」。
- `deploy/windows/start-services.ps1` 最後檢查 WSL 裡的 CadQuery 能不能在 `unshare -rn` 裡載入（順便讓 WSL 開機）。

## 結果（5070 Ti，2026-10-07）

| 檢查 | 結果 |
|---|---|
| 正常程式碼（WSL 已在執行／剛開機） | 成功，執行 2.3 秒／7.7 秒；輸出 STL、STEP、回投影圖 |
| 執行錯誤（`box()` 少參數） | 回「程式碼執行錯誤（第 2 行）」 |
| `import os` | Windows 這邊的 AST 檢查就擋下，不會叫 WSL |
| 無窮迴圈（上限 10 秒） | 13.2 秒回「執行超過 CPU 時間限制（10 秒）」 |
| 吞掉逾時例外的無窮迴圈 | 14.4 秒被硬限制結束，回「執行逾時」 |
| 連外網路 | `Network is unreachable` |
| 新增的 core dump | 0 |
| `tests/test_drawings.py` | 原本跳過的 3 個沙箱測試在這台經 WSL 通過 |
| `make demo-test`（`20261006T225046-ca91`） | 6/6；第 3 步 3D 重建（以前在這台一直略過）可執行、IoU 0.81 |
| 展示頁（生管身分、連接法蘭） | 程式碼可執行、實體有效、IoU 0.814，3D 模型正常顯示，執行 5.0 秒、外送 0 |

`make eval-cad`（`eval/run_cad_eval.py --skip-photos`，經後端 API、真的 Ortho2CAD，run_id `20261006T224124-ca3e`）：
**可執行 100%、平均 IoU 0.463（外框對齊 0.644）**，6 張圖紙的 IoU 和拆兩半量的（`20261005T071657-fc5c`）**逐位相同**
（例如 L 型固定支架 0.13894827531191167）——同一台、temperature 0 產生的程式碼一樣，WSL 裡執行的結果也一樣。
在 WSL 執行（含開 WSL、載入 CadQuery、算 IoU）每張 3.8–4.9 秒。這次 GPU 同時被其他程式佔用（使用率 97%），
生成時間（17–75 秒）不能代表平常的速度，速度看 README「Ortho2CAD 生成速度」。

## 注意
- 防護主力仍是 AST 白名單與受限的內建函式；WSL 裡的行程讀得到 `/mnt/c` 底下這個使用者的檔案，
  和 Linux 主機上 runner 讀得到使用者家目錄一樣（程式碼不能 `open`、不能 import `os`）。
- 假設 WSL 用預設掛載點 `/mnt/`；`/etc/wsl.conf` 改了 `automount.root` 的主機路徑會對不上。
- `unshare -rn` 需要非特權使用者命名空間；這台的 WSL2 核心（6.18）可以用。換一台不允許的 WSL 環境時執行會失敗
  （不會默默不隔離），`start-services.ps1` 的檢查也經過 `unshare -rn`，開機時就會看到 ✗。
- WSL 閒置後會停，之後第一次 3D 重建多幾秒開機；WSL 的虛擬機也吃系統記憶體（Docker Desktop 本來就開著它）。
- `eval/cad_generate.py`＋`eval/cad_score.py`（拆兩半）留著，只想重算 IoU、不想經過後端時用。
