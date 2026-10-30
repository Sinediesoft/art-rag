"""啟動或安裝失敗時的故障診斷。

由 deploy/windows/start-services.ps1、deploy/install.ps1、deploy/install.sh 呼叫。
只用標準函式庫，用 `uv run --no-project python deploy/diagnose.py …` 執行：後端套件壞了也能跑。
- 讀 data/logs/ 裡這次執行之後寫過的 log（--since），每個取最後幾行；
  UTF-16（PowerShell 寫的）、UTF-8 都讀
- 先比對規則表（這台主機實際踩過的狀況）；對不上才把錯誤交給本地模型解釋，標明「模型推測，未驗證」
- 模型只准連本機／內網位址（和 backend/app/rag/providers.py 的 is_local_url 同一套判斷），
  送出前遮掉 .env 的密碼與金鑰
- 只印建議，不執行任何指令；結束碼一律 0，不改變呼叫它的腳本成功或失敗
- 例外：離線包（docs/adr/046）在安裝途中失敗時，模型服務還沒啟動（最後一步才起）：
  暫時起包裡的 ollama serve 來解釋，問完就關，不動其他服務
"""

import argparse
import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

TAIL_LINES = 80
# 沒給 --since（手動執行）時，看最近這麼久內寫過的 log
DEFAULT_WINDOW_S = 3600
# 檔案系統的修改時間有粒度：開始時間往前放寬一點，剛好同一秒寫的 log 才不會漏掉
SINCE_SLACK_S = 2
LLM_TIMEOUT_S = 60
LLM_MAX_CHARS = 6000
OLLAMA_START_TIMEOUT_S = 30

# (代號, 錯誤樣式, 原因, 修法)；由上往下比，同一個代號只報一次
RULES: list[tuple[str, str, str, str]] = [
    (
        "docker-engine",
        r"error during connect|dockerDesktopLinuxEngine|Cannot connect to the Docker daemon"
        r"|docker daemon is not running|找不到 docker",
        "Docker Desktop 沒開，或引擎還沒準備好",
        "打開 Docker Desktop，等它顯示 Engine running 再重跑；"
        "不想用 Docker 就把 .env 的 DATABASE_URL、REDIS_URL 留空",
    ),
    (
        # 連得上卻密碼錯：連到的是這台電腦另外裝的 PostgreSQL（2026-10-30 學校電腦）
        "db-port-taken",
        r"password authentication failed|連接埠 \d+ 被這台電腦上的其他程式",
        "資料庫的連接埠被這台電腦上另外裝的程式（通常是 PostgreSQL）佔走，"
        "連到的不是這套系統的資料庫",
        "找出佔用連接埠的程式（Get-NetTCPConnection -LocalPort <埠> -State Listen）並停掉它，"
        "或把 .env 的 DATABASE_URL 改用別的連接埠",
    ),
    (
        "db-unreachable",
        r"連不上資料庫|資料庫沒起來|connection to server at .*5432|could not connect to server",
        "後端連不上資料庫（PostgreSQL 容器沒起來）",
        "先確認 Docker Desktop 有開，再執行 "
        "docker compose -f deploy/docker-compose.yml --env-file .env up -d db",
    ),
    (
        "redis",
        r"redis\.exceptions\.ConnectionError|Error \d+ connecting to [^\s]+:6379|JOBS_UNAVAILABLE",
        "背景工作連不上 Redis，3D 重建與排程會回 503",
        "確認 .env 的 REDIS_URL 與 redis 容器"
        "（docker compose -f deploy/docker-compose.yml --env-file .env up -d redis）",
    ),
    (
        "port-in-use",
        r"10048|address already in use|只允許使用一個通訊端位址|port is already allocated",
        "連接埠被舊的行程或其他程式佔著",
        "先執行 deploy\\windows\\start-services.ps1 -Stop 再重新啟動；容器的連接埠被佔，"
        "就是本機另外裝了 PostgreSQL／Redis，先停掉它",
    ),
    (
        "out-of-memory",
        r"CUDA out of memory|cudaMalloc failed|failed to allocate|0xc0000005|MemoryError",
        "顯示記憶體或系統記憶體不夠（遊戲、WSL 都會佔記憶體）",
        "關掉遊戲等大型程式；還不夠就執行 wsl --shutdown 釋放 WSL 的記憶體，再重跑",
    ),
    (
        "hf-offline",
        r"LocalEntryNotFoundError|local_files_only|outgoing traffic has been disabled",
        ".env 設了 HF_OFFLINE=true，但 embedding 模型還沒下載到本機快取",
        "把 .env 的 HF_OFFLINE 暫時改成 false，"
        "建一次索引（pipelines/build_index.py）下載模型，再改回 true",
    ),
    (
        "ollama-missing",
        r"找不到 ollama|ollama: command not found|'ollama' is not recognized|啟動 Ollama：000",
        "Ollama 沒裝或沒開",
        "打開 Ollama（或 winget install Ollama.Ollama），確認 http://127.0.0.1:11434 有回應",
    ),
    (
        "model-missing",
        r"model ['\"]?[\w.:/-]+['\"]? not found|try pulling it first|pull model manifest",
        ".env 指定的模型還沒下載",
        "執行 ollama pull <.env 的 HYBRID_MODEL>，備援模型（HYBRID_FALLBACK_MODEL）也要下載",
    ),
    (
        "wsl-cadquery",
        r"unshare: .*(Operation not permitted|不允許)|No module named ['\"]?cadquery"
        r"|CadQuery 不能用",
        "WSL 裡跑 3D 重建程式碼的 CadQuery 環境不能用",
        "照 docs/5070ti-host.md「第一次安裝」第 7 步重建 WSL 的 venv，"
        "並確認 .env 的 CAD_WSL_PYTHON、CAD_WSL_DISTRO",
    ),
    (
        "uv-missing",
        r"'uv' is not recognized|The term 'uv' is not recognized|uv: command not found|找不到 uv",
        "uv 沒裝，或裝完後這個視窗的 PATH 還是舊的",
        "關掉這個視窗、開一個新的再重跑；還是不行就重跑 deploy/install.ps1",
    ),
    (
        "venv-broken",
        r"is not a valid Python environment|No Python at ['\"]|did not find executable at",
        "Python 環境（backend\\.venv）不完整：上次安裝中斷，或這個資料夾是從別台電腦複製來的",
        "刪掉 backend\\.venv 資料夾再重跑，會重新建立",
    ),
    (
        "wsl-unavailable",
        r"BIOS 的虛擬化功能沒有開|HCS_E_HYPERV_NOT_INSTALLED|0x80370102|Virtual Machine Platform"
        r"|WSL_E_WSL_OPTIONAL_COMPONENT_REQUIRED",
        "WSL2 不能用：Windows 的「虛擬機器平台」沒啟用，或 BIOS 的虛擬化功能沒開",
        "重新點一次「安裝.cmd」會自動啟用 WSL2（要系統管理員權限、重新開機）；還是不行就進 BIOS "
        "開啟 Intel VT-x 或 AMD SVM",
    ),
    (
        "wsl-distro-missing",
        r"WSL_E_DISTRO_NOT_FOUND|There is no distribution with the supplied name|匯入 WSL 環境失敗",
        "離線包的 WSL 環境 artrag 不見了或匯入失敗",
        "重新點一次「安裝.cmd」會再匯入；還是失敗就看磁碟空間，"
        "並確認離線包的 offline\\wsl\\artrag.tar 完整",
    ),
    (
        "offline-missing",
        r"Network connectivity is disabled|(was not|wasn't) found in the cache",
        "離線環境找不到需要的 Python 套件或 Python",
        "離線包和這份程式碼的 uv.lock 對不上，或複製時漏了檔案：用同一個 commit 重新打包"
        "（deploy/offline/pack.ps1），整個資料夾重新複製",
    ),
    (
        # 離線包的 llama.cpp 是 CUDA 13 版：GTX 10 系列等舊卡、580 版以前的驅動程式不能用
        # （2026-10-30 學校電腦）
        "llama-cuda",
        r"no kernel image is available|ggml_cuda_init: failed|CUDA driver version is insufficient"
        r"|cudaErrorNoKernelImageForDevice|cudaErrorInsufficientDriver"
        r"|unsupported gpu architecture",
        "Ortho2CAD 的 llama-server 和這張顯示卡或驅動程式不相容，一載入模型就閃退"
        "（3D 重建回 HTTP 500）",
        "換用 Vulkan 版的 llama.cpp（-LlamaServer 指定），或更新 NVIDIA 驅動程式",
    ),
    (
        "scheduler",
        r"UnsupportedClassVersionError|Unable to access jarfile|scheduler\.jar",
        "排程服務（Timefold）需要 Java 21 和建好的 scheduler.jar",
        "執行 pipelines/setup_scheduler.py；不需要排程就加 -SkipScheduler，排程頁會改用簡易排程",
    ),
    (
        "disk-full",
        r"No space left on device|There is not enough space on the disk|磁碟空間不足",
        "磁碟空間不夠",
        "清掉不用的模型與 %LOCALAPPDATA%\\Temp\\wsl-crashes 裡的 WSL 傾印檔（一個約 370 MB）",
    ),
    (
        "frontend-build",
        r"lightningcss|Unexpected token Comma",
        "前端建置失敗（CSS 語法），不是部署設定的問題",
        "回報前端組；暫時可以用之前建好的 frontend/dist",
    ),
]

# 離線包（docs/adr/046）沒有網路可以補下載、Ollama 也是包裡附的：這幾種改成請對方重新複製離線包
OFFLINE_FIXES = {
    "hf-offline": "離線包的 offline\\hf 缺檔：從原本的離線包重新複製整個資料夾",
    "model-missing": "離線包的 offline\\ollama-models 缺檔：從原本的離線包重新複製整個資料夾",
    "ollama-missing": "離線包的 offline\\ollama 缺檔，或連接埠 11435 被佔："
    "重新複製離線包、確認沒有別的程式用 11435",
    "uv-missing": "離線包的 offline\\uv 缺檔：從原本的離線包重新複製整個資料夾",
    # 離線包沒有 Docker：資料庫與 Redis 在自己的 WSL 環境裡
    "db-unreachable": "WSL 環境 artrag 的 PostgreSQL 沒起來："
    "系統匣的萊問圖示按右鍵 →「重新啟動服務」，"
    "還是不行就看 app\\data\\logs\\wsl-services.log",
    "redis": "WSL 環境 artrag 的 Redis 沒起來："
    "系統匣的萊問圖示按右鍵 →「重新啟動服務」，"
    "還是不行就看 app\\data\\logs\\wsl-services.log",
    "venv-broken": "刪掉安裝資料夾裡的 app\\backend\\.venv，"
    "再按萊問視窗的「重試」（會從離線包重新建立）",
    "wsl-unavailable": "重新打開萊問會自動啟用 WSL2"
    "（跳出系統管理員確認時按「是」，完成後重新開機）；"
    "還是不行就進 BIOS 開啟 Intel VT-x 或 AMD SVM",
    "wsl-distro-missing": "按萊問視窗的「重試」會重新匯入；還是失敗就看磁碟空間，"
    "並確認安裝資料夾的 offline\\wsl\\artrag.tar 完整（不完整就重新安裝）",
    "db-port-taken": "錯誤訊息裡有佔用連接埠的程式名稱：停掉它（多半要系統管理員權限）再按「重試」",
    "llama-cuda": "新版安裝包會依顯示卡自動改用 Vulkan 版：用新版安裝包重新安裝",
}

# 離線包的使用者多半不懂電腦（2026-10-30 學校電腦試裝的回饋）：每條規則另外給一段白話，
# 不用術語、只講發生什麼事和要按什麼；桌面程式先顯示這段，原因與修法收進「給技術人員看」
_ASK_ADMIN = "還是不行，請找電腦的管理員，把「給技術人員看」的內容給他。"
_RESTART = "請在螢幕右下角找到萊問的圖示，按右鍵選「重新啟動服務」。"
_REINSTALL = (
    "安裝檔不完整，可能是複製或解壓縮時漏了檔案。請重新解壓縮安裝包，再執行一次 setup.exe。"
)
PLAIN = {
    "db-port-taken": "這台電腦上有別的軟體（多半是學校或課程裝的資料庫）佔走了萊問要用的位置，"
    "萊問的資料庫沒辦法啟動。這需要電腦的管理員處理：請找管理員，把「給技術人員看」的內容給他。",
    "db-unreachable": "萊問的資料庫沒有順利啟動。" + _RESTART + _ASK_ADMIN,
    "redis": "萊問的背景服務沒有順利啟動。" + _RESTART + _ASK_ADMIN,
    "port-in-use": "有別的程式佔用了萊問需要的位置。請重新開機，再打開萊問。" + _ASK_ADMIN,
    "out-of-memory": "這台電腦的記憶體不夠用。請關掉其他程式（特別是遊戲），再按「重試」。",
    "hf-offline": _REINSTALL,
    "model-missing": _REINSTALL,
    "ollama-missing": _REINSTALL,
    "uv-missing": _REINSTALL,
    "offline-missing": _REINSTALL,
    "venv-broken": "上次安裝沒有做完，留下了不完整的檔案。"
    "請打開安裝資料夾裡的 app\\backend 資料夾，刪掉裡面的 .venv 資料夾，再按「重試」。",
    "wsl-unavailable": "這台電腦還沒開啟萊問需要的 Windows 功能。重新打開萊問會自動開啟："
    "跳出「是否允許變更」時按「是」，完成後重新開機。"
    "如果跳出來的視窗要你輸入管理員密碼，代表你沒有權限，請找電腦的管理員協助。",
    "wsl-distro-missing": "萊問的資料庫沒有建立成功。請按「重試」；還是不行，"
    "可能是硬碟空間不夠，請清出至少 10 GB 再試。",
    "wsl-cadquery": "3D 重建需要的元件不能用，其他功能不受影響。請重新執行一次 setup.exe。",
    "llama-cuda": "這台電腦的顯示卡和 3D 重建的程式不相容，其他功能不受影響。"
    "請用新版的萊問安裝包重新安裝，它會自動改用相容的版本。",
    "scheduler": "排程功能需要的元件沒有準備好，其他功能不受影響。請重新執行一次 setup.exe。",
    "disk-full": "硬碟空間不夠。請刪掉不需要的檔案，清出至少 10 GB，再按「重試」。",
}

# 對到前者時不再報後者：「密碼錯」那一行也有 connection to server at，會再對到「資料庫沒起來」
SUPERSEDES = {"db-port-taken": {"db-unreachable"}}

# PowerShell 5.1 把原生程式寫到 stderr 的內容包成 NativeCommandError：就算成功，db.log 也有這幾行
_PS_NOISE = re.compile(r"^\s*(At .*:\d+ char:\d+|\+ .*)$")
_ERRORISH = re.compile(
    r"error|exception|traceback|failed|fatal|refused|denied|not found|not recognized"
    r"|錯誤|失敗|找不到|拒絕|沒起來|不能用|沒有回應|沒有登記|✗",
    re.IGNORECASE,
)
_SECRET_KEY = re.compile(r"KEY|SECRET|PASSWORD|TOKEN", re.IGNORECASE)
_URL_CREDENTIALS = re.compile(r"(\w+://)[^/\s:@]+:[^/\s@]+@")
_JWT = re.compile(r"eyJ[\w-]+\.[\w-]+\.[\w-]+")
_THINK = re.compile(r"<think>.*?</think>", re.DOTALL)
_LOCAL_SUFFIXES = (".local", ".internal", ".lan", ".ts.net")

SYSTEM_PROMPT = (
    "你是一套地端 RAG 展示系統的部署故障診斷助手。"
    "服務有：PostgreSQL 與 Redis（Docker）、Ollama（:11434）、Timefold 排程（:8082，Java）、"
    "Ortho2CAD llama-server（:8081）、後端 uvicorn（:8000）、Celery worker、WSL2 裡的 CadQuery。"
    "只根據使用者給的錯誤與 log 判斷，用正體中文回答兩段："
    "「原因：」一句話；「修法：」最多 3 步。看不出原因就直接說看不出來，不要編造檔案、設定或指令。"
)
# 離線包和開發機的服務不一樣：放在要問的內容前面，模型才不會叫人開 Docker 或連網下載
OFFLINE_CONTEXT = (
    "【環境】這台是離線安裝包裝的桌面程式「萊問」：沒有 Docker，"
    "PostgreSQL 與 Redis 在 WSL 環境 artrag 裡；"
    "Ollama 在 :11435；Python 環境在安裝資料夾的 app\\backend\\.venv；沒有網路可以下載。"
    "使用者能做的操作：萊問視窗的「重試」、系統匣萊問圖示右鍵的「重新啟動服務」，"
    "或刪掉壞掉的資料夾後重試。"
    "使用者多半不懂電腦：修法用白話寫要按什麼，不用 WSL、PostgreSQL、連接埠、.env 這類術語；"
    "需要系統管理員權限的事，請他找電腦的管理員。"
)


def parse_env(text: str) -> dict[str, str]:
    values = {}
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        key, _, value = s.partition("=")
        values[key.strip()] = value.strip()
    return values


def decode(raw: bytes) -> str:
    # PowerShell 5.1 的 *> 寫 UTF-16 LE（有 BOM）；其他程式寫 UTF-8
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", errors="replace")
    return raw.decode("utf-8-sig", errors="replace")


def clean(text: str) -> list[str]:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return [line.rstrip() for line in lines if line.strip() and not _PS_NOISE.match(line)]


def read_logs(paths: list[Path], since: float | None) -> dict[str, list[str]]:
    """{檔名: 最後幾行}；只收修改時間在 since 之後的（since 為 None 時全收）。"""
    out = {}
    for path in paths:
        try:
            if since is not None and path.stat().st_mtime < since - SINCE_SLACK_S:
                continue
            lines = clean(decode(path.read_bytes()))
        except OSError:
            continue
        if lines:
            out[path.name] = lines[-TAIL_LINES:]
    return out


def match_rules(message: str, logs: dict[str, list[str]]) -> list[tuple[str, str, str, str]]:
    """回傳 (代號, 原因, 修法, 依據)；依據是對到的那一行和它在哪裡。"""
    sources = [("錯誤訊息", line) for line in clean(message)]
    sources += [(name, line) for name, lines in logs.items() for line in lines]
    found = []
    for rule_id, pattern, cause, fix in RULES:
        rx = re.compile(pattern, re.IGNORECASE)
        for where, line in sources:
            if rx.search(line):
                found.append((rule_id, cause, fix, f"{where}：{line.strip()[:200]}"))
                break
    hit = {f[0] for f in found}
    hidden = set().union(*(SUPERSEDES.get(r, set()) for r in hit))
    return [f for f in found if f[0] not in hidden]


def error_lines(message: str, logs: dict[str, list[str]], per_file: int = 15) -> list[str]:
    # 只收像錯誤的行：正常啟動的 log 不該拿去問模型
    out = [f"[錯誤訊息] {line}" for line in clean(message)]
    for name, lines in logs.items():
        picked = [line for line in lines if _ERRORISH.search(line)]
        out += [f"[{name}] {line}" for line in picked[-per_file:]]
    return out


def secrets_in(env: dict[str, str], example: dict[str, str]) -> list[str]:
    # 和 .env.example 一樣的值（ollama、artrag）本來就公開，遮了反而把容器名稱 artrag-db-1 也遮掉
    return [
        value
        for key, value in env.items()
        if _SECRET_KEY.search(key) and len(value) >= 4 and value != example.get(key)
    ]


def mask(text: str, secrets: list[str]) -> str:
    for value in sorted(secrets, key=len, reverse=True):
        text = text.replace(value, "***")
    text = _URL_CREDENTIALS.sub(r"\1***@", text)
    return _JWT.sub("***", text)


def is_local_url(url: str) -> bool:
    """本機、私有網段、Tailscale（100.64.0.0/10、*.ts.net）或內網主機名稱才算本地。"""
    host = urllib.parse.urlsplit(url).hostname
    if not host:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return "." not in host or host.endswith(_LOCAL_SUFFIXES)
    return ip.is_private or ip.is_loopback or ip in ipaddress.ip_network("100.64.0.0/10")


def ask_model(base_url: str, model: str, api_key: str, text: str) -> str:
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        "temperature": 0.2,
        "max_tokens": 500,
        "stream": False,
    }
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key or 'ollama'}",
        },
    )
    with urllib.request.urlopen(req, timeout=LLM_TIMEOUT_S) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return _THINK.sub("", data["choices"][0]["message"]["content"]).strip()


def bundle_info() -> dict | None:
    """離線包的 offline.json（app\\ 旁邊）；不是離線包時 None。"""
    path = REPO.parent / "offline" / "offline.json"
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def port_open(url: str) -> bool:
    parts = urllib.parse.urlsplit(url)
    try:
        with socket.create_connection((parts.hostname, parts.port or 80), timeout=1):
            return True
    except OSError:
        return False


def start_bundled_ollama(base_url: str) -> subprocess.Popen | None:
    """起離線包裡的 ollama serve，等它回應；起不來回傳 None。

    只在本機位址上起，連接埠照 base_url。"""
    exe = REPO.parent / "offline" / "ollama" / "ollama.exe"
    parts = urllib.parse.urlsplit(base_url)
    if not exe.is_file() or parts.hostname not in ("127.0.0.1", "localhost") or not parts.port:
        return None
    env = {
        **os.environ,
        "OLLAMA_HOST": f"127.0.0.1:{parts.port}",
        "OLLAMA_MODELS": str(REPO.parent / "offline" / "ollama-models"),
    }
    # 不給標準輸入：從桌面程式跑時不能讓子行程掛在繼承來的管線上（docs/adr/047）
    proc = subprocess.Popen(
        [str(exe), "serve"],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    deadline = time.time() + OLLAMA_START_TIMEOUT_S
    while time.time() < deadline and proc.poll() is None:
        if port_open(base_url):
            return proc
        time.sleep(1)
    stop(proc)
    return None


def stop(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(10)
    except subprocess.TimeoutExpired:
        proc.kill()


def explain_with_model(
    env: dict[str, str], secrets: list[str], lines: list[str], bundle: dict | None = None
) -> tuple[str | None, str]:
    """回傳（模型的回答或 None，說明用了哪個模型／為什麼沒用）。"""
    if env.get("LLM_MODE", "").lower() == "mock":
        return None, "這台是 mock 方案，沒有生成模型"
    # 離線包在寫 .env 之前就失敗時沒有設定可讀：照 offline.json 用包裡的 Ollama 與模型
    default_url = "http://localhost:11434/v1"
    if bundle is not None:
        default_url = f"http://127.0.0.1:{bundle.get('ollama_port', 11435)}/v1"
    base_url = env.get("HYBRID_BASE_URL") or default_url
    if not is_local_url(base_url):
        host = urllib.parse.urlsplit(base_url).hostname
        return None, f"HYBRID_BASE_URL 不是本機或內網位址（{host}），log 不送出"
    text = mask("\n".join(lines), secrets)[-LLM_MAX_CHARS:]
    if bundle is not None:
        text = OFFLINE_CONTEXT + "\n\n" + text
    models = [m for m in (env.get("HYBRID_MODEL"), env.get("HYBRID_FALLBACK_MODEL")) if m]
    if not models and bundle is not None:
        models = list(bundle.get("models") or [])
    if not models:
        models = ["qwen3-vl:4b-instruct"]
    started = None
    if bundle is not None and not port_open(base_url):
        started = start_bundled_ollama(base_url)
    note = "；模型服務還沒啟動，診斷時暫時開了離線包的 Ollama，問完已關閉" if started else ""
    reasons = []
    try:
        for model in dict.fromkeys(models):
            try:
                return ask_model(base_url, model, env.get("HYBRID_API_KEY", ""), text), model + note
            except (urllib.error.URLError, OSError, ValueError, KeyError, IndexError) as e:
                reasons.append(f"{model}：{getattr(e, 'reason', e)}")
    finally:
        if started:
            stop(started)
    return None, "本地模型叫不到（" + "；".join(str(r) for r in reasons) + "）"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--since", type=float, help="只看這個時間（Unix 秒）之後寫過的 log")
    ap.add_argument("--message", default="", help="腳本拋出的錯誤訊息")
    ap.add_argument(
        "--log",
        action="append",
        default=[],
        help="指定要看的 log（可重複；不給就看 data/logs/*.log）",
    )
    ap.add_argument("--no-llm", action="store_true", help="只比對規則表，不問本地模型")
    ap.add_argument("--env", default=str(REPO / ".env"))
    args = ap.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")

    if args.log:
        paths, since = [Path(p) for p in args.log], args.since
    else:
        paths = sorted((REPO / "data" / "logs").glob("*.log"))
        since = args.since if args.since is not None else time.time() - DEFAULT_WINDOW_S
    env_path, example_path = Path(args.env), REPO / ".env.example"
    env = parse_env(env_path.read_text(encoding="utf-8")) if env_path.exists() else {}
    example = parse_env(example_path.read_text(encoding="utf-8")) if example_path.exists() else {}
    secrets = secrets_in(env, example)
    logs = read_logs(paths, since)

    print("\n── 故障診斷 ──")
    print("看了：" + ("、".join(logs) if logs else "（沒有這次執行之後寫的 log）"))
    found = match_rules(args.message, logs)
    bundle = bundle_info()
    offline_bundle = bundle is not None
    for rule_id, cause, fix, evidence in found:
        if offline_bundle:
            fix = OFFLINE_FIXES.get(rule_id, fix)
            if rule_id in PLAIN:
                print(f"白話：{PLAIN[rule_id]}")
        print(f"✗ {cause}")
        print(f"  修法：{fix}")
        print(f"  依據（{evidence}）")
    if found:
        return 0

    lines = error_lines(args.message, logs)
    if not lines:
        print("沒有找到錯誤訊息。")
        return 0
    if args.no_llm:
        answer, used = None, "加了 --no-llm"
    else:
        answer, used = explain_with_model(env, secrets, lines, bundle)
    if answer:
        print(f"規則表沒有對到，以下是本地模型（{used}）看 log 的判斷（模型推測，未驗證）：")
        print("  " + answer.replace("\n", "\n  "))
    else:
        print(f"規則表沒有對到，{used}。以下是 log 裡看起來像錯誤的最後幾行：")
        for line in lines[-20:]:
            print("  " + mask(line, secrets))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
