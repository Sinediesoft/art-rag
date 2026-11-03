# ADR 047：桌面程式萊問（LAIWEN）與安裝精靈（setup.exe）

- 日期：2026-10-27
- 狀態：採用（`desktop/`：Electron；`deploy/offline/art-rag.iss`：Inno Setup；`deploy/offline/uninstall.ps1`）
- 接續 ADR 046（離線安裝包），取代它的 `安裝.cmd`／`啟動.cmd`／`停止.cmd` 入口

## 背景
ADR 046 的離線包裝完是一個資料夾、幾個 `.cmd` 與瀏覽器網址：要知道先點「安裝」、每次點「啟動」、會跳終端機視窗、
用完要記得點「停止」。目標是「安裝後變成一個程式，打開就能用」，和一般軟體一樣有安裝精靈、開始功能表、解除安裝。

## 決定
- **Electron 桌面程式「萊問」`LaiWen.exe`**（`desktop/`）：視窗裡就是後端在 `:8000` 提供的網頁，`frontend/` 一行都不用改。
  主行程只做起停：
  - 打開時：後端已經在跑就直接顯示；沒有就跑 `start-services.ps1`（約 1 分鐘），進度與輸出顯示在啟動畫面
  - 第一次打開（或換了新版離線包，`data\installed.json` 的 commit 和 `offline.json` 不同）：先跑
    `install-offline.ps1 -NoStart -NoShortcut`；它啟用 WSL2 後以結束碼 3010 結束，程式就提示重新開機
  - 失敗時畫面攤開紀錄，最後一段是故障診斷（ADR 045），可以「重試」
  - **安裝畫面右邊是 AI 助理**（使用者要求，2026-10-29；`desktop/assistant.js`）：失敗時自動貼出故障診斷的原因與修法，
    也可以隨時打字問或點常見問題。用這台電腦的 Ollama（只准本機／內網位址），安裝途中模型服務還沒起時先開離線包的
    `ollama serve`，之後 `start-services.ps1` 直接沿用；送給模型的內容分成故障診斷的結論、紀錄裡的錯誤、最近的紀錄，
    遮掉 `.env` 的金鑰。主力模型優先（實測 4B 解釋錯誤常答非所問），`keep_alive` 1 分鐘，問完很快把顯示記憶體還回去。
    2026-10-30 學校電腦試裝的回饋「不夠白話」：自動貼的診斷先講白話（`deploy/diagnose.py` 的 `PLAIN`），技術內容收進
    「給技術人員看」；給模型的指示改成對不懂電腦的人講話，不用術語、不叫人打指令，處理不了就請他找管理員
  - **關掉視窗只是收到系統匣**（使用者決定，2026-10-27）：服務繼續跑，再打開很快；系統匣選單有
    重新啟動服務、故障診斷、開啟紀錄資料夾、結束（停止全部服務）
  - 只允許一個執行個體；外部連結交給預設瀏覽器；網頁不拿任何 IPC（preload 只對 `file://` 的啟動畫面開放）
- **安裝與啟動不另寫一套**：桌面程式在背景呼叫既有的 PowerShell 腳本（`-EncodedCommand` 避開引號問題，
  輸出強制 UTF-8：預設是系統的 OEM 字碼頁，英文版 Windows 會把中文變成 ?）
- **安裝精靈用 Inno Setup 6**（只有打包那台需要，免費可商用）：`setup.exe` 很小，約 26 GB 的資料放在旁邊的 `payload\`，
  安裝時直接複製（external）。模型本來就壓不小，壓進 exe 只會讓打包多花很久
  - 預設裝到 `C:\LaiWen`，可以改（資料夾與執行檔用英文名：uv、WSL、PostgreSQL 的路徑都經過它）。不放 Program Files：程式執行時要在安裝資料夾裡寫 `.env`、紀錄、Python 環境與
    WSL 的虛擬磁碟；一般使用者權限就能裝（`PrivilegesRequired=lowest`）
  - 開始功能表、桌面捷徑（可取消）、「新增或移除程式」；覆蓋安裝新版前先停止服務
  - **第一頁先檢查硬體與環境**（使用者要求，2026-10-29）：`deploy/offline/precheck.ps1` 只讀不改、不用系統管理員權限，
    列出作業系統、記憶體、顯示卡（門檻和 `install-offline.ps1` 一樣，決定 8B／4B）、WSL2／虛擬化、各磁碟剩餘空間；
    有問題只提醒不擋，不適合的電腦在複製 26 GB 之前就知道。磁碟不夠由 Inno Setup 自己在選位置那頁擋
  - 解除安裝：`uninstall.ps1` 關程式、停服務、`wsl --unregister artrag`（虛擬磁碟在安裝資料夾裡才移除），再刪整個安裝資料夾
  - 介面語言用 Inno Setup 官方 repo 的正體中文翻譯（`ChineseTraditional.isl`，is-6_7_3；安裝程式本身沒附）
- **名稱與圖示**：程式叫「萊問」（使用者決定，2026-10-27），圖示是使用者指定的 LAIWEN 萊問 標誌（黑底、米白的門＋問號、琥珀點 #B99005），目前是照圖片重畫的近似版；
  啟動畫面用同一組顏色
- Electron 44.7.0（打包時已發布超過兩週）、`@electron/packager` 20.3.0，版本鎖在 `desktop/package-lock.json`

## 打包後的結構
```
art-rag-offline\            （pack.ps1 的輸出，整個資料夾複製給別人）
  setup.exe  使用說明.txt
  payload\app\  payload\offline\  payload\desktop\（LaiWen.exe）
C:\LaiWen\                  （安裝後）
  app\  offline\  desktop\  使用說明.txt   ← 第一次打開後多出 wsl-data\、app\.env、app\backend\.venv、app\data\
```

## 不採用
- **Edge 的 app 模式**（不用建置）：使用者要真的程式（自己的 exe、圖示、系統匣）
- **NSIS／electron-builder 的安裝程式**：單一安裝檔有 2 GB 限制，26 GB 要另外處理；Inno Setup 的 external 檔案最直接
- **壓成單一 setup.exe**：要切成多個 .bin，打包慢很多，好處只有少一個資料夾

## 注意
- 沒有程式碼簽章：`setup.exe`、`LaiWen.exe` 第一次執行時 SmartScreen 會警告（「其他資訊」→「仍要執行」）
- 程式被強制結束（工作管理員）時服務會留著：下次打開偵測到後端在跑就直接接上；要停止從系統匣「結束」，
  或執行 `start-services.ps1 -Stop`
- `安裝.cmd`／`啟動.cmd`／`停止.cmd` 不再放進離線包；需要時可以直接在終端機跑 `install-offline.ps1`、`start-services.ps1`
