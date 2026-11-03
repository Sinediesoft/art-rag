"""一鍵安裝用：依設備方案寫 .env（deploy/install.ps1、deploy/install.sh 呼叫）。

只用標準函式庫，裝好 uv 之後用 `uv run --no-project python deploy/configure_env.py …` 執行。
- 沒有 .env：從 .env.example 複製一份
- 方案要改的設定，只改「還是 .env.example 的預設值或空白」的；使用者自己改過的一律保留，並列出來
- --set KEY=VALUE 一定照寫（安裝流程自己要控制的，例如建索引前後的 HF_OFFLINE）
"""

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# 各方案要的設定
PROFILES: dict[str, dict[str, str]] = {
    # 顯卡記憶體 12 GB 以上（例如 RTX 5070 Ti）：8B 主力、4B 備援
    "full": {
        "LLM_MODE": "real",
        "EMBED_MODE": "real",
        "HYBRID_MODEL": "qwen3-vl:8b-instruct",
        "HYBRID_FALLBACK_MODEL": "qwen3-vl:4b-instruct",
    },
    # 小顯卡或 Apple 晶片：4B
    "light": {
        "LLM_MODE": "real",
        "EMBED_MODE": "real",
        "HYBRID_MODEL": "qwen3-vl:4b-instruct",
        "HYBRID_FALLBACK_MODEL": "qwen3-vl:4b-instruct",
    },
    # 沒有顯卡：不呼叫生成模型、不下載 embedding，只能看介面與流程
    "mock": {"LLM_MODE": "mock", "EMBED_MODE": "mock"},
}


def parse_env(text: str) -> dict[str, str]:
    values = {}
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        key, _, value = s.partition("=")
        values[key.strip()] = value.strip()
    return values


def set_key(text: str, key: str, value: str) -> str:
    """改掉 KEY= 那一行（保留其他行與註解）；沒有這個 key 就加在最後。"""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip().startswith(f"{key}=") and not line.lstrip().startswith("#"):
            lines[i] = f"{key}={value}"
            return "\n".join(lines) + "\n"
    return "\n".join(lines).rstrip("\n") + f"\n{key}={value}\n"


def plan(
    env_text: str, example_text: str, profile: str, docker: bool, small_gpu: bool
) -> tuple[dict[str, str], dict[str, str]]:
    """回傳（要寫的設定，因為使用者改過而保留的設定 → 方案原本想設的值）。"""
    wanted = dict(PROFILES[profile])
    if not docker:
        wanted["DATABASE_URL"] = ""  # 沒有 Docker：檔案索引＋SQLite，功能相同（ADR 009）
    if small_gpu:
        wanted["SEND_IMAGE"] = "false"  # 4 GB 級顯卡：回答不附圖，每題省約 1,070 token
    current, default = parse_env(env_text), parse_env(example_text)
    changes, kept = {}, {}
    for key, value in wanted.items():
        now = current.get(key)
        if now == value:
            continue
        if now in (None, "") or now == default.get(key):
            changes[key] = value
        else:
            kept[key] = value
    return changes, kept


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--profile", choices=sorted(PROFILES), required=True)
    ap.add_argument("--no-docker", action="store_true", help="沒有 Docker：DATABASE_URL 留空")
    ap.add_argument("--small-gpu", action="store_true", help="4 GB 級顯卡：SEND_IMAGE=false")
    ap.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    ap.add_argument("--env", default=str(REPO / ".env"))
    ap.add_argument("--dry-run", action="store_true", help="只列出會改什麼")
    args = ap.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")

    env_path, example_path = Path(args.env), REPO / ".env.example"
    example = example_path.read_text(encoding="utf-8")
    created = not env_path.exists()
    text = example if created else env_path.read_text(encoding="utf-8")
    changes, kept = plan(text, example, args.profile, not args.no_docker, args.small_gpu)
    for item in args.set:
        key, sep, value = item.partition("=")
        if not sep or not key.strip():
            ap.error(f"--set 要寫成 KEY=VALUE：{item}")
        changes[key.strip()] = value.strip()

    print(f"方案：{args.profile}" + ("（建立新的 .env）" if created else ""))
    for key, value in changes.items():
        print(f"  設定 {key}={value}")
    for key, value in kept.items():
        print(f"  保留你改過的 {key}（方案建議 {key}={value}）")
    if not changes and not kept and not created:
        print("  .env 不用改")
    if args.dry_run:
        return 0
    for key, value in changes.items():
        text = set_key(text, key, value)
    env_path.write_text(text, encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
