"""Claude 設定檔診斷（只讀，不修改任何東西）。

用途：hello 工具沒出現時，找出 Claude 桌面版「真正讀的」是哪一份設定檔、格式對不對。
你的 Claude 是從 Microsoft Store 安裝的（MSIX 版），電腦上可能同時有兩份設定檔：
  1. %APPDATA%\\Claude\\claude_desktop_config.json            （一般位置）
  2. %LOCALAPPDATA%\\Packages\\Claude_*\\LocalCache\\Roaming\\Claude\\claude_desktop_config.json
     （Store 版 Claude 自己實際讀寫的位置）
這支程式把兩份（以及其他可能位置）都檢查一次，結果印出來，由 check_claude_config.bat 存到 logs\\claude_config_check.txt。
為了隱私，只列出最外層有哪些欄位名稱和 mcpServers 的內容，不會印出 preferences 等其他內容。
"""

import glob
import json
import os
import sys
from datetime import datetime

LOG_TAIL_LINES = 25


def candidate_dirs():
    dirs = []
    appdata = os.environ.get("APPDATA")
    local = os.environ.get("LOCALAPPDATA")
    home = os.environ.get("USERPROFILE")
    if appdata:
        dirs.append(("一般位置（%APPDATA%）", os.path.join(appdata, "Claude")))
    if local:
        for pkg in sorted(glob.glob(os.path.join(local, "Packages", "Claude_*"))):
            dirs.append(
                (
                    "Store 版位置（" + os.path.basename(pkg) + "）",
                    os.path.join(pkg, "LocalCache", "Roaming", "Claude"),
                )
            )
        for d in sorted(glob.glob(os.path.join(local, "Claude-Data", "claude_*"))):
            dirs.append(("Claude-Data 位置（" + os.path.basename(d) + "）", d))
        dirs.append(("其他可能位置 1", os.path.join(local, "Claude")))
        dirs.append(("其他可能位置 2", os.path.join(local, "AnthropicClaude")))
    if home:
        dirs.append(("其他可能位置 3", os.path.join(home, ".claude")))
    return dirs


def dir_state(path):
    """回傳資料夾狀態；跟 os.path.isdir 不同，權限不足時會說出來，不會假裝不存在。"""
    try:
        names = os.listdir(path)
        return True, f"存在，裡面有 {len(names)} 個項目：" + "、".join(sorted(names)[:30])
    except FileNotFoundError:
        return False, "不存在"
    except PermissionError:
        return False, "存在但沒有權限讀取（PermissionError）"
    except OSError as e:
        return False, f"讀不到：{type(e).__name__} {e}"


def search_packages():
    """在 Store 版 Claude 的整個 Packages 資料夾裡找設定檔和 mcp 紀錄檔（最多往下找 7 層）。"""
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        return
    roots = sorted(glob.glob(os.path.join(local, "Packages", "Claude_*")))
    roots += sorted(glob.glob(os.path.join(local, "Claude-Data", "claude_*")))
    for pkg in roots:
        print()
        print(f"■ 在 {os.path.basename(pkg)} 裡搜尋設定檔與 mcp 紀錄檔")
        found = 0
        base_depth = pkg.rstrip("\\/").count(os.sep)
        for root, subdirs, files in os.walk(pkg, onerror=lambda e: None):
            subdirs[:] = [
                d
                for d in subdirs
                if d
                not in ("claude-code", "Cache", "cache", "node_modules", "scratch", "workspaces")
            ]
            if root.count(os.sep) - base_depth >= 7:
                subdirs[:] = []
            for f in files:
                if f == "claude_desktop_config.json" or (
                    f.startswith("mcp") and f.endswith(".log")
                ):
                    fp = os.path.join(root, f)
                    print(f"  找到：{fp}（{os.path.getsize(fp)} bytes，{fmt_time(fp)}）")
                    found += 1
        if not found:
            print("  一個都沒找到")


def fmt_time(path):
    return datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y-%m-%d %H:%M:%S")


def check_config(path):
    if not os.path.exists(path):
        print("  設定檔：不存在")
        return
    raw = open(path, "rb").read()
    print(f"  設定檔：存在，{len(raw)} bytes，最後修改 {fmt_time(path)}")
    if raw.startswith(b"\xef\xbb\xbf"):
        print("  ✘ 檔案開頭有 BOM（隱藏字元），Claude 可能會讀不懂整份設定檔")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        print(f"  ✘ 不是 UTF-8 編碼：{e}")
        return
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        print(f"  ✘ JSON 格式錯誤：第 {e.lineno} 行第 {e.colno} 個字附近：{e.msg}")
        lines = text.splitlines()
        lo, hi = max(0, e.lineno - 2), min(len(lines), e.lineno + 1)
        for i in range(lo, hi):
            print(f"     {i + 1:>3}| {lines[i][:120]}")
        return
    print("  ✔ JSON 格式正確")
    if not isinstance(data, dict):
        print("  ✘ 最外層不是 { }")
        return
    print("  最外層欄位：" + "、".join(data.keys()))
    servers = data.get("mcpServers")
    if servers is None:
        print("  ✘ 沒有 mcpServers → Claude 不會啟動任何 MCP Server")
        return
    if not isinstance(servers, dict):
        print("  ✘ mcpServers 不是 { }")
        return
    print(f"  mcpServers 裡有 {len(servers)} 個：" + ("、".join(servers.keys()) or "（空的）"))
    for name, cfg in servers.items():
        cmd = (cfg or {}).get("command", "")
        args = (cfg or {}).get("args", [])
        print(f"   - {name}")
        print(
            f"       command = {cmd}  → {'✔ 檔案存在' if os.path.exists(cmd) else '✘ 找不到這個檔案'}"
        )
        for a in args:
            mark = "✔ 檔案存在" if os.path.exists(a) else "（不是檔案路徑或找不到）"
            print(f"       args    = {a}  → {mark}")


def check_logs(base):
    log_dir = os.path.join(base, "logs")
    if not os.path.isdir(log_dir):
        print("  logs 資料夾：不存在")
        return
    files = sorted(glob.glob(os.path.join(log_dir, "mcp*.log")))
    if not files:
        print("  logs：沒有 mcp 開頭的紀錄檔（代表 Claude 從來沒有嘗試啟動 MCP Server）")
        return
    print("  mcp 紀錄檔：")
    for f in files:
        print(f"   - {os.path.basename(f)}（{os.path.getsize(f)} bytes，{fmt_time(f)}）")
    for f in files:
        name = os.path.basename(f)
        if name == "mcp.log" or "art-rag" in name:
            print(f"  --- {name} 最後 {LOG_TAIL_LINES} 行 ---")
            with open(f, encoding="utf-8", errors="replace") as fh:
                for line in fh.readlines()[-LOG_TAIL_LINES:]:
                    print("   " + line.rstrip()[:300])


def main():
    print("Claude 設定檔診斷  " + datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    dirs = candidate_dirs()
    if not dirs:
        print("找不到 APPDATA / LOCALAPPDATA 環境變數")
        return 1
    for label, base in dirs:
        ok, state = dir_state(base)
        print()
        print(f"■ {label}")
        print(f"  資料夾：{base}  → {state}")
        if ok:
            check_config(os.path.join(base, "claude_desktop_config.json"))
            check_logs(base)
    search_packages()
    print()
    print("診斷結束（這支程式沒有修改任何檔案）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
