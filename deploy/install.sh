#!/usr/bin/env bash
# 一鍵安裝（macOS、Linux、WSL2）：偵測這台電腦能跑到什麼程度，裝好套件、寫好 .env、下載模型、建索引，
# 最後啟動並檢查。重複執行沒關係：已經裝好的會略過，.env 裡你自己改過的設定不會被覆蓋。
#
#   bash deploy/install.sh               # 自動判斷
#   bash deploy/install.sh --dry-run     # 只列出會做什麼，不改任何東西
#   bash deploy/install.sh --mode mock --no-start
#
# 方案（--mode auto 依顯卡選）：
#   full  NVIDIA 顯卡 12 GB 以上，或 Apple 晶片記憶體 32 GB 以上：Qwen3-VL 8B 主力、4B 備援
#   light NVIDIA 顯卡 4～12 GB，或 Apple 晶片 16 GB 以上：4B（6 GB 以下回答不附圖）
#   mock  沒有可用的顯卡：不呼叫生成模型，只能看介面與流程
# 沒有 Docker 時資料庫改用檔案索引＋SQLite（功能相同）。3D 重建（Ortho2CAD）與排程（Java）是選裝，見 README。
# 缺少 uv、Node.js、Ollama 時會先列出來，問過才安裝（--yes 不問）；Docker 只提示怎麼裝。
set -Eeuo pipefail

MODE=auto
YES=0
NO_START=0
DRY_RUN=0
while [ $# -gt 0 ]; do
  case "$1" in
    --mode) MODE="$2"; shift 2 ;;
    --yes) YES=1; shift ;;
    --no-start) NO_START=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "不認得的參數：$1（--help 看用法）"; exit 2 ;;
  esac
done
case "$MODE" in auto|full|light|mock) ;; *) echo "--mode 只能是 auto、full、light、mock"; exit 2 ;; esac

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

say() { printf '\n\033[36m▶ %s\033[0m\n' "$1"; }
note() { printf '  %s\n' "$1"; }
has() { command -v "$1" >/dev/null 2>&1; }

# 失敗時交給 deploy/diagnose.py 說明原因（docs/adr/045）；uv 還沒裝好就只留原本的錯誤訊息
RUN_START="$(date +%s)"
diagnose() {
  has uv || return 0
  uv run --no-project python deploy/diagnose.py --since "$RUN_START" --message "$1" || true
}
on_error() {
  local code=$? line=${BASH_LINENO[0]} cmd=$BASH_COMMAND
  trap - ERR
  diagnose "安裝在 install.sh 第 ${line} 行失敗（結束碼 ${code}）：${cmd}"
}
trap on_error ERR
ask() {
  [ "$YES" = 1 ] && return 0
  read -r -p "$1 [y/N] " answer
  [[ "$answer" =~ ^([yY]|yes)$ ]]
}

# ---------------------------------------------------------------- 1. 偵測
say "偵測這台電腦"
OS="$(uname -s)"
ARCH="$(uname -m)"
VRAM_MB=0
GPU=""
if has nvidia-smi; then
  line="$(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader,nounits 2>/dev/null | head -n 1 || true)"
  if [ -n "$line" ]; then
    GPU="${line%%,*}"
    VRAM_MB="$(echo "${line##*,}" | tr -d ' ')"
  fi
fi
APPLE_GB=0
if [ "$OS" = Darwin ] && [ "$ARCH" = arm64 ]; then
  # Apple 晶片：顯示卡與系統共用記憶體
  APPLE_GB=$(( $(sysctl -n hw.memsize) / 1024 / 1024 / 1024 ))
  note "Apple 晶片，記憶體 ${APPLE_GB} GB"
elif [ -n "$GPU" ]; then
  note "顯卡：${GPU}（${VRAM_MB} MB）"
else
  note "顯卡：沒有偵測到 NVIDIA 顯卡"
fi

if [ "$MODE" = auto ]; then
  if [ "$VRAM_MB" -ge 12000 ] || [ "$APPLE_GB" -ge 32 ]; then MODE=full
  elif [ "$VRAM_MB" -ge 4000 ] || [ "$APPLE_GB" -ge 16 ]; then MODE=light
  else MODE=mock; fi
fi
SMALL_GPU=0
if [ "$MODE" = light ] && [ "$APPLE_GB" = 0 ] && [ "$VRAM_MB" -lt 6000 ]; then SMALL_GPU=1; fi
note "方案：${MODE}$([ "$SMALL_GPU" = 1 ] && echo '（小顯卡，回答不附圖）' || true)"

# ---------------------------------------------------------------- 2. 系統軟體
say "檢查系統軟體"
NEED=()
for tool in uv node; do
  if has "$tool"; then note "${tool}：已安裝"; else note "${tool}：缺少（必要）"; NEED+=("$tool"); fi
done
if [ "$MODE" != mock ]; then
  if has ollama; then note "ollama：已安裝"; else note "ollama：缺少（必要）"; NEED+=(ollama); fi
fi
if has docker; then note "docker：已安裝"; else note "docker：缺少（選裝，沒有就改用 SQLite）"; fi

env_args=(--profile "$MODE")
has docker || env_args+=(--no-docker)
[ "$SMALL_GPU" = 1 ] && env_args+=(--small-gpu)

if [ "$DRY_RUN" = 1 ]; then
  say "只列出會做什麼（--dry-run）"
  [ ${#NEED[@]} -gt 0 ] && note "會詢問是否安裝：${NEED[*]}"
  note "寫 .env（方案 ${MODE}）："
  has uv && uv run --no-project python deploy/configure_env.py "${env_args[@]}" --dry-run
  note "接著：uv sync、npm ci、啟動資料庫、下載模型、建索引、建置前端、啟動並檢查"
  exit 0
fi

install_tool() {
  local tool="$1"
  if [ "$OS" = Darwin ]; then
    if ! has brew; then echo "沒有 Homebrew：請先安裝 https://brew.sh 或手動安裝 ${tool}"; return 1; fi
    case "$tool" in
      uv) brew install uv ;;
      node) brew install node ;;
      ollama) brew install ollama ;;
    esac
  else
    case "$tool" in
      # 官方安裝程式（astral.sh、ollama.com）
      uv) curl -LsSf https://astral.sh/uv/install.sh | sh; export PATH="$HOME/.local/bin:$PATH" ;;
      ollama) curl -fsSL https://ollama.com/install.sh | sh ;;
      node) echo "Linux 請用發行版套件或 nvm 安裝 Node.js 24（https://nodejs.org）後再執行一次"; return 1 ;;
    esac
  fi
}
for tool in "${NEED[@]+"${NEED[@]}"}"; do
  if ask "要安裝 ${tool} 嗎？"; then
    install_tool "$tool" || { echo "缺少 ${tool}，沒辦法繼續"; exit 1; }
  else
    echo "缺少 ${tool}，沒辦法繼續；裝好後再執行一次"; exit 1
  fi
done

# ---------------------------------------------------------------- 3. Docker（選裝）
USE_DOCKER=0
if has docker && docker info >/dev/null 2>&1; then
  USE_DOCKER=1
elif has docker; then
  note "Docker 已安裝但沒有在執行：這次改用檔案索引＋SQLite（開好 Docker 後再執行一次就會改用資料庫）"
else
  note "沒有 Docker：資料庫改用檔案索引＋SQLite（功能相同）；要用 PostgreSQL 請安裝 Docker"
fi
env_args=(--profile "$MODE")
[ "$USE_DOCKER" = 1 ] || env_args+=(--no-docker)
[ "$SMALL_GPU" = 1 ] && env_args+=(--small-gpu)

# ---------------------------------------------------------------- 4. .env
say "寫 .env"
uv run --no-project python deploy/configure_env.py "${env_args[@]}" --set HF_OFFLINE=false

# ---------------------------------------------------------------- 5. 套件
say "安裝後端套件（uv sync）"
(cd backend && uv sync --compile-bytecode)
say "安裝前端套件（npm ci）"
(cd frontend && npm ci)

# ---------------------------------------------------------------- 6. 資料庫
if [ "$USE_DOCKER" = 1 ]; then
  say "啟動資料庫（PostgreSQL 17＋pgvector）"
  docker compose -f deploy/docker-compose.yml --env-file .env up -d --wait db
fi

# ---------------------------------------------------------------- 7. 模型
if [ "$MODE" != mock ]; then
  if ! curl -fsS http://localhost:11434/api/version >/dev/null 2>&1; then
    if [ "$OS" = Darwin ] && [ -d /Applications/Ollama.app ]; then open -a Ollama; else (ollama serve >/dev/null 2>&1 &); fi
    sleep 5
  fi
  models=(qwen3-vl:4b-instruct)
  [ "$MODE" = full ] && models=(qwen3-vl:8b-instruct qwen3-vl:4b-instruct)
  for m in "${models[@]}"; do
    say "下載模型 ${m}（第一次要一段時間）"
    ollama pull "$m"
  done
fi

# ---------------------------------------------------------------- 8. 索引
say "建索引（第一次會下載 bge-m3 約 2.2 GB、Chinese-CLIP 約 750 MB）"
(cd backend && uv run python ../pipelines/build_index.py)
if [ "$MODE" != mock ]; then
  # 模型都下載好了：之後只讀本機快取，斷網也能用
  uv run --no-project python deploy/configure_env.py "${env_args[@]}" --set HF_OFFLINE=true
fi

# ---------------------------------------------------------------- 9. 建置與啟動
say "建置前端"
(cd frontend && npm run build)

START_CMD="cd backend && uv run uvicorn app.main:app --host 127.0.0.1 --port 8000"
if [ "$NO_START" = 1 ]; then
  say "安裝完成（--no-start）"
  note "啟動：${START_CMD}"
  note "要連 3D 重建與排程：make demo-all（另需 llama.cpp、Java 21，見 README）"
  exit 0
fi
say "啟動並檢查"
mkdir -p data/logs
(cd backend && uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 >../data/logs/backend.out.log 2>../data/logs/backend.log) &
SERVER=$!
for _ in $(seq 1 60); do
  sleep 3
  if curl -fsS http://127.0.0.1:8000/api/v1/health >/dev/null 2>&1; then
    say "完成：打開 http://127.0.0.1:8000（方案 ${MODE}）；按 Ctrl+C 停止"
    wait "$SERVER"
    exit 0
  fi
  kill -0 "$SERVER" 2>/dev/null || break
done
echo "後端沒起來，看 data/logs/backend.log"
diagnose "後端沒起來"
exit 1
