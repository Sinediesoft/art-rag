"""
check_env.py：setup.bat 最後一步會執行這個檔案

1. 確認 Python 和套件都裝好了
2. 依照這台電腦的實際路徑，產生要貼進 Claude 桌面版的兩份設定：
     claude_config_step1_hello.json   第一步：只接 hello 測試工具
     claude_config_step2_full.json    第二步：hello 成功後，換成正式的 art-rag
"""

import json
import platform
import sys
from importlib.metadata import version
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
if not PYTHON.exists():  # 不是 Windows（例如雲端測試）時
    PYTHON = Path(sys.executable)


def server_entry(script: str) -> dict:
    return {
        "command": str(PYTHON),
        "args": [str(ROOT / script)],
        "env": {
            "PYTHONUTF8": "1",  # 讓 Windows 上的 Python 一律用 UTF-8 讀寫中文
            "HF_HUB_OFFLINE": "1",  # Server 執行時不上網下載模型（模型由 build_index.bat 先下載好）
        },
    }


def main() -> int:
    ok = True
    print(f"  Python {platform.python_version()}（{sys.executable}）")
    for pkg in ("mcp", "fastembed", "numpy", "onnxruntime"):
        try:
            print(f"  ✔ {pkg} {version(pkg)}")
        except Exception:
            print(f"  ✘ 找不到 {pkg}")
            ok = False
    if not str(ROOT).isascii():
        print(f"  ✘ 專案路徑有中文或特殊字元：{ROOT}，請把資料夾搬到 C:\\dev\\art-mcp")
        ok = False

    step1 = {"mcpServers": {"art-rag-hello": server_entry("hello_server.py")}}
    step2 = {"mcpServers": {"art-rag": server_entry("server.py")}}
    for name, cfg in (
        ("claude_config_step1_hello.json", step1),
        ("claude_config_step2_full.json", step2),
    ):
        (ROOT / name).write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    print()
    print("  已產生 Claude 桌面版設定（路徑已經幫你填好）：")
    print("    claude_config_step1_hello.json  ← 先用這份")
    print("    claude_config_step2_full.json   ← hello 成功後換這份")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
