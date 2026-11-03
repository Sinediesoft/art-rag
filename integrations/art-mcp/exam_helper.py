"""exam_helper.py：考試小幫手（階段 6 步驟 3、4）

由 exam_A.bat（條件 A）或 exam_B.bat（條件 B）執行。它不會呼叫 Claude，只幫你做兩件手工：
  1. 把下一題的題目放進剪貼簿 → 你在 Claude 按 Ctrl+V 貼上
  2. 你按 Claude 回答下面的「複製」→ 回到這個視窗按 Enter → 自動存成 eval/answers/<A或B>/<題號>.txt

為什麼要它：條件 B 的工具呼叫是用「回答檔的存檔時間」對到題目（score.py 的 build_tool_map），
所以一定要照順序、問完一題就馬上存檔。手動最容易存錯檔名或晚存，小幫手每題都當下存、檔名不會錯。

可以隨時輸入 q 休息，下次再雙擊會從還沒做的那一題繼續（進度記在 _小幫手紀錄.csv）。

這不是 MCP Server，所以可以用 print()。
"""

from __future__ import annotations

import csv
import glob
import json
import os
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
EVAL = ROOT / "eval"
QUESTIONS = EVAL / "questions.csv"
SERVER_LOG = ROOT / "logs" / "server.log"
SERVER_PY = ROOT / "server.py"

COND = {
    "A": {
        "name": "條件 A（不接 MCP）",
        "where": [
            "用瀏覽器打開 claude.ai（免費帳號），每一題都開「無痕對話」",
            "模型選 Sonnet 5.5，思考程度選好之後整個考試都不要改",
            "網頁搜尋（Web search）關掉",
        ],
    },
    "B": {
        "name": "條件 B（接上 art-rag）",
        "where": [
            "用 Claude 桌面版（免費帳號），每一題都按「新對話」",
            "模型、思考程度跟條件 A 一模一樣",
            "網頁搜尋關掉；連接器只開 art-rag",
            "跳出「是否允許使用工具」時，選允許（選「永遠允許」可以少按幾次，不影響結果）",
        ],
    },
}


# ─────────────────────────────────────────────
# 剪貼簿（Windows 內建功能，不用另外裝套件）
# ─────────────────────────────────────────────
class WinClipboard:
    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002

    def __init__(self):
        import ctypes
        import ctypes.wintypes as w

        self.ct = ctypes
        self.u = ctypes.WinDLL("user32", use_last_error=True)
        self.k = ctypes.WinDLL("kernel32", use_last_error=True)
        self.u.OpenClipboard.argtypes = [w.HWND]
        self.u.OpenClipboard.restype = w.BOOL
        self.u.CloseClipboard.restype = w.BOOL
        self.u.EmptyClipboard.restype = w.BOOL
        self.u.GetClipboardData.argtypes = [w.UINT]
        self.u.GetClipboardData.restype = w.HANDLE
        self.u.SetClipboardData.argtypes = [w.UINT, w.HANDLE]
        self.u.SetClipboardData.restype = w.HANDLE
        self.k.GlobalAlloc.argtypes = [w.UINT, ctypes.c_size_t]
        self.k.GlobalAlloc.restype = w.HGLOBAL
        self.k.GlobalLock.argtypes = [w.HGLOBAL]
        self.k.GlobalLock.restype = w.LPVOID
        self.k.GlobalUnlock.argtypes = [w.HGLOBAL]
        self.k.GlobalUnlock.restype = w.BOOL

    def _open(self):
        for _ in range(40):  # 別的程式正在用剪貼簿時，等一下再試
            if self.u.OpenClipboard(None):
                return
            time.sleep(0.05)
        raise RuntimeError("剪貼簿被別的程式占用，等幾秒再按一次 Enter")

    def get(self) -> str:
        self._open()
        try:
            h = self.u.GetClipboardData(self.CF_UNICODETEXT)
            if not h:
                return ""
            p = self.k.GlobalLock(h)
            try:
                return self.ct.wstring_at(p)
            finally:
                self.k.GlobalUnlock(h)
        finally:
            self.u.CloseClipboard()

    def set(self, text: str) -> None:
        data = text.encode("utf-16-le") + b"\x00\x00"
        h = self.k.GlobalAlloc(self.GMEM_MOVEABLE, len(data))
        p = self.k.GlobalLock(h)
        self.ct.memmove(p, data, len(data))
        self.k.GlobalUnlock(h)
        self._open()
        try:
            self.u.EmptyClipboard()
            if not self.u.SetClipboardData(self.CF_UNICODETEXT, h):
                raise RuntimeError("沒辦法把題目放進剪貼簿")
        finally:
            self.u.CloseClipboard()


class FakeClipboard:
    """雲端測試用（不是 Windows 時）：從環境變數指定的檔案讀『複製的回答』。"""

    def __init__(self):
        self.text = ""

    def get(self) -> str:
        return self.text

    def set(self, text: str) -> None:
        self.text = text


# ─────────────────────────────────────────────
# 讀題目、進度
# ─────────────────────────────────────────────
def load_questions() -> list[dict]:
    with QUESTIONS.open(encoding="utf-8-sig", newline="") as f:
        return [
            {
                "id": r["題號"].strip(),
                "type": (r.get("類型") or "").strip(),
                "q": (r.get("問題") or "").strip(),
            }
            for r in csv.DictReader(f)
            if (r.get("題號") or "").strip()
        ]


def log_path(cond: str) -> Path:
    return EVAL / "answers" / cond / "_小幫手紀錄.csv"


def load_done(cond: str) -> list[str]:
    p = log_path(cond)
    if not p.exists():
        return []
    with p.open(encoding="utf-8-sig", newline="") as f:
        done = []
        for r in csv.DictReader(f):
            if r["動作"] == "存檔":
                done.append(r["題號"])
            elif r["動作"] == "重做" and r["題號"] in done:
                done.remove(r["題號"])
        return done


def write_log(cond: str, qid: str, action: str, asked: str, chars: int = 0) -> None:
    p = log_path(cond)
    new = not p.exists()
    with p.open("a", encoding="utf-8-sig" if new else "utf-8", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["題號", "動作", "題目複製時間", "存檔時間", "回答字數"])
        w.writerow([qid, action, asked, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), chars])


def backup_old_A() -> None:
    """條件 A 第一次用小幫手時，把 10/24（付費帳號）的舊回答另存一份，之後拿來比穩定性。"""
    src = EVAL / "answers" / "A"
    dst = EVAL / "answers" / "A_1024付費帳號"
    if dst.exists() or log_path("A").exists():
        return
    olds = [p for p in src.glob("*.txt") if p.stat().st_size > 0]
    if not olds:
        return
    dst.mkdir(parents=True, exist_ok=True)
    for p in olds:
        shutil.copy2(p, dst / p.name)
    print(
        f"✔ 10/24 的舊回答 {len(olds)} 個檔，已另存到 eval\\answers\\A_1024付費帳號\\（之後比穩定性用）"
    )


def record_setting(cond: str) -> None:
    p = EVAL / "answers" / cond / "_設定紀錄.txt"
    if p.exists():
        print("這次的設定（第一次填的）：" + p.read_text(encoding="utf-8").strip().splitlines()[-1])
        return
    print("第一次使用，記一下設定（之後當證據，也確保 A、B 一樣）")
    model = input("用的模型和思考程度是？（例如：Sonnet 5.5 Medium）→ ").strip() or "（未填）"
    p.write_text(
        f"{COND[cond]['name']}，免費帳號\n開始時間：{datetime.now():%Y-%m-%d %H:%M}\n模型／思考程度：{model}\n",
        encoding="utf-8",
    )


# ─────────────────────────────────────────────
# 條件 B 開始前的自動檢查
# ─────────────────────────────────────────────
def check_before_B() -> list[str]:
    warn = []
    local = os.environ.get("LOCALAPPDATA", "")
    cfgs = glob.glob(os.path.join(local, "Claude-Data", "claude_*", "claude_desktop_config.json"))
    # ① Claude 有沒有真的重開：server.log 最後一次「啟動」要晚於 server.py 和 Claude 設定檔的修改時間
    changed_files = [p for p in [SERVER_PY, *map(Path, cfgs)] if p.exists()]
    if SERVER_LOG.exists() and changed_files:
        starts = re.findall(
            r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ INFO 啟動",
            SERVER_LOG.read_text(encoding="utf-8", errors="replace"),
            re.M,
        )
        if starts:
            last = datetime.strptime(starts[-1], "%Y-%m-%d %H:%M:%S")
            newest = max(changed_files, key=lambda p: p.stat().st_mtime)
            changed = datetime.fromtimestamp(newest.stat().st_mtime)
            if last < changed:
                warn.append(
                    f"{newest.name} 在 {changed:%m/%d %H:%M} 改過，但 art-rag 最後一次啟動是 {last:%m/%d %H:%M}："
                    "Claude 可能沒有真的重開（要從右下角系統匣結束）"
                )
    # ② art-rag-hello 應該已經拿掉
    for cfg in cfgs:
        try:
            servers = json.load(open(cfg, encoding="utf-8-sig")).get("mcpServers", {})
            if "art-rag-hello" in servers:
                warn.append("Claude 設定裡還有 art-rag-hello：請先雙擊 remove_hello.bat")
            if "art-rag" not in servers:
                warn.append("Claude 設定裡沒有 art-rag")
        except (OSError, json.JSONDecodeError):
            pass
    return warn


# ─────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────
def main() -> int:
    cond = (sys.argv[1] if len(sys.argv) > 1 else "").upper()
    if cond not in COND:
        print("用法：exam_helper.py A 或 exam_helper.py B（請雙擊 exam_A.bat 或 exam_B.bat）")
        return 1
    clip = WinClipboard() if os.name == "nt" else FakeClipboard()
    qs = load_questions()
    (EVAL / "answers" / cond).mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print(f" 考試小幫手：{COND[cond]['name']}　共 {len(qs)} 題")
    print("=" * 60)
    for line in COND[cond]["where"]:
        print("・" + line)
    print()
    if cond == "A":
        backup_old_A()
    else:
        warns = check_before_B()
        for w_ in warns:
            print("⚠ " + w_)
        if warns:
            ans = (
                input("有警告。先處理比較好；硬要繼續請輸入 go 再按 Enter，其他鍵結束 → ")
                .strip()
                .lower()
            )
            if ans != "go":
                return 1
        else:
            print("✔ 自動檢查通過：Claude 已重開載入最新 server.py、設定裡只有 art-rag")
    record_setting(cond)

    done = load_done(cond)
    todo = [q for q in qs if q["id"] not in done]
    print(f"\n進度：已完成 {len(done)}/{len(qs)} 題")
    if not todo:
        print("全部做完了！接著雙擊 score.bat 計分，或跟教練說一聲。")
        return 0

    i = 0
    while i < len(todo):
        q = todo[i]
        n = len(qs) - len(todo) + i + 1
        clip.set(q["q"])
        asked = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print("\n" + "─" * 60)
        print(f"第 {n}/{len(qs)} 題　{q['id']}（{q['type']}）")
        print(f"題目：{q['q']}")
        print("✔ 題目已經複製好了")
        print("  1. Claude 開新對話 → Ctrl+V 貼上 → Enter")
        print("  2. 等回答完全結束 → 按回答下面的「複製」")
        print("  3. 回到這裡按 Enter 存檔")
        print("  （r＝重新複製題目　q＝先休息，下次從這題繼續）")
        while True:
            cmd = input("→ ").strip().lower()
            if cmd == "q":
                print(f"已休息。下次雙擊 exam_{cond}.bat 會從 {q['id']} 繼續。")
                return 0
            if cmd == "r":
                clip.set(q["q"])
                print("✔ 題目重新複製好了")
                continue
            try:
                text = clip.get().replace("\r\n", "\n").strip()
            except RuntimeError as e:
                print("✘ " + str(e))
                continue
            if not text:
                print("✘ 剪貼簿是空的：請按 Claude 回答下面的「複製」再按 Enter")
                continue
            if text == q["q"]:
                print("✘ 剪貼簿裡還是題目：好像還沒按回答的「複製」")
                continue
            if len(text) < 15:
                ok = (
                    input(
                        f"⚠ 回答只有 {len(text)} 個字，確定是完整回答嗎？（y 存檔／其他鍵重新複製）→ "
                    )
                    .strip()
                    .lower()
                )
                if ok != "y":
                    continue
            break
        path = EVAL / "answers" / cond / f"{q['id']}.txt"
        path.write_text(
            f"# {q['id']}｜{q['q']}\n#｜{COND[cond]['name']}｜小幫手存檔 {datetime.now():%Y-%m-%d %H:%M:%S}\n\n{text}\n",
            encoding="utf-8",
        )
        write_log(cond, q["id"], "存檔", asked, len(text))
        print(
            f"✔ 存好了：eval\\answers\\{cond}\\{q['id']}.txt（{len(text)} 字）開頭：{text[:40].replace(chr(10), ' ')}…"
        )
        i += 1
    print("\n全部做完了！接著雙擊 score.bat 計分，或跟教練說一聲。")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中斷。下次雙擊會從還沒存檔的那一題繼續。")
        sys.exit(0)
