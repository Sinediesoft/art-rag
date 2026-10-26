"""把 art-rag-hello 和 art-rag 兩個 MCP Server 加進 Claude 桌面版的設定檔（Store 版位置）。

診斷結果（10/23）：Claude 真正讀的設定檔在
  %LOCALAPPDATA%\\Claude-Data\\claude_pzs8sxrjxfjjc\\claude_desktop_config.json
裡面只有 coworkUserFilesPath、preferences，沒有 mcpServers，所以 Claude 不會啟動任何 MCP Server。
做法：先備份，再只「加入」mcpServers，原本的內容一個字都不動。由 fix_claude_config.bat 在 Claude 關閉時執行。
"""

import glob
import json
import os
import shutil
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
# 10/27：階段 6 起不再加 art-rag-hello（已用 remove_hello.py 拿掉），只加正式的 art-rag
TEMPLATES = ["claude_config_step2_full.json"]


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

    # 要加入的兩個 Server，直接從 setup 產生的範本讀，不另外手寫路徑
    servers = {}
    for name in TEMPLATES:
        with open(os.path.join(HERE, name), encoding="utf-8") as f:
            servers.update(json.load(f)["mcpServers"])
    for name, cfg in servers.items():
        if not os.path.exists(cfg["command"]):
            print(f"✘ {name} 要用的程式不存在：{cfg['command']}，先不修改")
            return 1

    backup = path + ".bak_" + datetime.now().strftime("%Y%m%d_%H%M%S")
    shutil.copy2(path, backup)
    print(f"✔ 已備份：{backup}")

    merged = dict(data.get("mcpServers") or {})
    merged.update(servers)
    new = {"mcpServers": merged}
    new.update({k: v for k, v in data.items() if k != "mcpServers"})

    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:  # UTF-8、不加 BOM
        json.dump(new, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

    with open(path, encoding="utf-8") as f:  # 重新讀一次，確認寫進去的是對的
        check = json.load(f)
    lost = [k for k in data if k not in check]
    if lost or set(check["mcpServers"]) < set(servers):
        print(f"✘ 驗證失敗（少了：{lost}），已從備份還原")
        shutil.copy2(backup, path)
        return 1
    print("✔ 最外層欄位：" + "、".join(check.keys()))
    print("✔ mcpServers：" + "、".join(check["mcpServers"].keys()))
    print("✔ 原本的欄位都還在，修改完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
