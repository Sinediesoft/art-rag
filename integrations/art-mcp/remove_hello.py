"""把 art-rag-hello 從 Claude 桌面版設定檔拿掉（階段 6 步驟 2）。

為什麼：hello 是階段 2 用來確認「連線本身通不通」的測試工具，任務已經完成。
留著的話，條件 B 每一題 Claude 都會多讀一份 hello 的工具說明（多花額度），也多一個可能選錯的工具。
做法跟 fix_claude_config.py 一樣：先備份 → 只刪 mcpServers 裡的 art-rag-hello → 重新讀一次確認。由 remove_hello.bat 在 Claude 關閉時執行。
"""

import glob
import json
import os
import shutil
import sys
from datetime import datetime


def main():
    local = os.environ.get("LOCALAPPDATA", "")
    found = glob.glob(os.path.join(local, "Claude-Data", "claude_*", "claude_desktop_config.json"))
    if len(found) != 1:
        print(f"✘ 找到 {len(found)} 份設定檔（應該剛好 1 份），先不修改：{found}")
        return 1
    path = found[0]
    print(f"設定檔：{path}")
    with open(path, encoding="utf-8-sig") as f:
        data = json.load(f)
    servers = data.get("mcpServers") or {}
    if "art-rag-hello" not in servers:
        print("✔ 設定裡本來就沒有 art-rag-hello，不用改")
        print("✔ mcpServers：" + "、".join(servers.keys()))
        return 0
    backup = path + ".bak_" + datetime.now().strftime("%Y%m%d_%H%M%S")
    shutil.copy2(path, backup)
    print(f"✔ 已備份：{backup}")
    del servers["art-rag-hello"]
    data["mcpServers"] = servers
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    with open(path, encoding="utf-8") as f:
        check = json.load(f)
    if (
        "art-rag-hello" in check.get("mcpServers", {})
        or "art-rag" not in check.get("mcpServers", {})
        or set(check) != set(data)
    ):
        print("✘ 驗證失敗，已從備份還原")
        shutil.copy2(backup, path)
        return 1
    print("✔ mcpServers：" + "、".join(check["mcpServers"].keys()))
    print("✔ 已拿掉 art-rag-hello，其他內容都還在")
    return 0


if __name__ == "__main__":
    sys.exit(main())
