"""
score.py：評估計分（階段 6-3）

由 score.bat 執行。用數字回答一個問題：「Claude 接上我的 MCP 之後，有沒有比較好？」

讀進來的東西
  eval/questions.csv        題目（你出的題）：題號、類型、問題、答案關鍵字、應該用的工具、是否應拒答…
  eval/answers/A/<題號>.txt  條件 A（不接 MCP）的回答，一題一個檔
  eval/answers/B/<題號>.txt  條件 B（接上 MCP）的回答，一題一個檔
  logs/tool_calls.jsonl     server.py 自動記的工具呼叫（登記簿）
  data/artworks/、data/draft/  知識庫：用來判斷回答裡的網址是不是真的出處

算出來的 6 個數字（A、B 各算一次，工具相關的只有 B 有）
  1 關鍵字命中率   答案關鍵字有幾個出現在回答裡
  2 出處           回答有沒有附「知識庫裡真的有」的出處網址；網址對不上的，可能是 AI 編的
  3 該拒答時拒答   知識庫沒有的東西，有沒有老實說找不到
  4 不該拒答卻拒答 明明有資料，卻說找不到
  5 工具選對率     （只有 B）有沒有用到這題應該用的工具
  6 參數正確率     （只有 B）工具參數裡有沒有正確的作品編號

產出（都在 eval/results/）
  report.html          結果表＋長條圖，雙擊用瀏覽器打開
  score_summary.md     同樣的結果，純文字版（可以貼進報告或 README）
  per_question.csv     每一題的細節（Excel 打得開）
  tool_map_B.csv       每一題對到哪幾次工具呼叫（可以手動修正，見下面說明）
  score_output.txt     這次執行畫面上印出的文字（Claude 可以直接讀）

工具呼叫怎麼對到題目？
  登記簿只記「幾點幾分呼叫了哪個工具」，不知道是哪一題。
  所以用「回答檔的存檔時間」來切：第 N 題的工具呼叫 = 第 N-1 題存檔之後、第 N 題存檔之前發生的那些。
  前提：條件 B 照順序一題一題做，問完馬上把回答貼進檔案存檔。
  第一次算完會存成 tool_map_B.csv，之後都用這個檔（就算你之後又改了回答檔，對應也不會亂掉）。
  發現對錯了：直接改 tool_map_B.csv；想全部重新對：刪掉 tool_map_B.csv 再雙擊一次。

這不是 MCP Server，所以可以用 print()。
"""

from __future__ import annotations

import csv
import html
import json
import re
import sys
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parent
EVAL = ROOT / "eval"
QUESTIONS = EVAL / "questions.csv"
ANSWERS = {"A": EVAL / "answers" / "A", "B": EVAL / "answers" / "B"}
RESULTS = EVAL / "results"
TOOL_LOG = ROOT / "logs" / "tool_calls.jsonl"
KB_DIRS = [ROOT / "data" / "artworks", ROOT / "data" / "draft"]
if not KB_DIRS[0].exists() and (ROOT.parent.parent / "kb" / "artworks").exists():
    KB_DIRS = [ROOT.parent.parent / "kb" / "artworks"]  # 放在小組 repo 時，出處對照小組知識庫
TOOL_MAP = RESULTS / "tool_map_B.csv"
FIRST_WINDOW = timedelta(minutes=15)  # 第一題往前看多久的工具呼叫

COND_NAME = {"A": "條件 A（不接 MCP）", "B": "條件 B（接上 MCP）"}
HEADER_HINT = "# 把 Claude 的回答整段貼在這一行下面，然後存檔。（這一行和上一行不會被計分）"

# 「找不到」的說法：該拒答的題，回答裡有這些就算有拒答（會把對到的字印出來，方便你人工確認）
REFUSAL_PATTERNS = [
    r"找不到",
    r"查不到",
    r"沒有找到",
    r"未能找到",
    r"搜尋不到",
    r"沒有(相關|這筆|這件|此)",
    r"(資料庫|知識庫|館藏|資料)(中|裡|內)?(沒有|並未|未)",
    r"未收錄",
    r"沒有收錄",
    r"不在(資料庫|知識庫|館藏|收錄)",
    r"無法(確認|確定|提供|回答|找到|查到|查證)",
    r"沒有.{0,6}(資料|紀錄|記錄|資訊)",
    r"沒(有)?辦法(確認|確定|查到|找到|提供)",  # 10/27 加：A 的 N18、N19 寫「無法確定」「沒有辦法確定」沒被認出來
]
# 異體字：AI 常寫「台灣」「剪粘」，答案寫「臺灣」「剪黏」，算同一個字
VARIANTS = str.maketrans({"臺": "台", "粘": "黏", "戬": "戩", "裏": "裡", "峯": "峰"})
MUSEUM_NAMES = ["國立臺灣歷史博物館", "臺史博", "國家文化記憶庫", "文化部典藏網"]

OUT_LINES: list[str] = []


def say(msg: str = "") -> None:
    print(msg)
    OUT_LINES.append(msg)


# ─────────────────────────────────────────────
# 共用的小工具
# ─────────────────────────────────────────────
def norm(text: str) -> str:
    """比對前先統一寫法：全形半形一致、去掉空白、英文小寫、異體字換成同一個。"""
    text = unicodedata.normalize("NFKC", text).translate(VARIANTS).lower()
    # 10/27 加：引號、粗體符號也去掉（回答寫「壽」字，關鍵字是 壽字，要算對到）
    return re.sub(r"[\s「」『』“”‘’\"'*]+", "", text)


def norm_url(url: str) -> str:
    """網址比對前統一：解開 %3D 這類編碼、去掉結尾斜線。"""
    return unquote(url.strip()).rstrip("/").lower()


def split_multi(cell: str) -> list[str]:
    """「a｜b｜c」切成 ['a','b','c']（全形｜和半形|都可以）。"""
    return [x.strip() for x in re.split(r"[｜|]", cell or "") if x.strip()]


def read_text(path: Path) -> str:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "cp950"):  # 新版記事本存 UTF-8；舊版可能存成 Big5（cp950）
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


COMMENT_RE = re.compile(r"^#\s*([A-Z]+\d+)?｜")  # 說明行：「# B01｜題目」「#｜小幫手…」


def answer_body(path: Path) -> str:
    """回答檔去掉說明行（# 題號｜…、#｜…、貼上提示），剩下的才是回答。
    10/27 改：以前所有 # 開頭的行都去掉，但 Claude 的回答常有「## 小標題」，會被誤刪，所以只去掉說明行。"""
    keep = [
        l
        for l in read_text(path).splitlines()
        if not (COMMENT_RE.match(l.strip()) or l.strip() == HEADER_HINT)
    ]
    return "\n".join(keep).strip()


def pct(x: float | None) -> str:
    return "—" if x is None else f"{x * 100:.0f}%"


def ratio(hit: int, total: int) -> str:
    return "—" if total == 0 else f"{hit}/{total}（{hit / total * 100:.0f}%）"


# ─────────────────────────────────────────────
# 1. 讀題目、建立空白回答檔
# ─────────────────────────────────────────────
def load_questions() -> list[dict]:
    if not QUESTIONS.exists():
        raise SystemExit(f"找不到 {QUESTIONS.relative_to(ROOT)}：請先出題")
    with QUESTIONS.open(encoding="utf-8-sig", newline="") as f:
        rows = [r for r in csv.DictReader(f) if (r.get("題號") or "").strip()]
    qs = []
    for r in rows:
        qs.append(
            {
                "id": r["題號"].strip(),
                "type": (r.get("類型") or "").strip() or "未分類",
                "question": (r.get("問題") or "").strip(),
                "keywords": split_multi(r.get("正確答案關鍵字（用｜分隔）", "")),
                "source": split_multi(r.get("答案出自哪一筆", "")),  # 可以填好幾筆，用｜分隔
                "tools": split_multi(r.get("應該用的工具（用｜分隔）", "")),
                "param": (r.get("正確參數") or "").strip(),
                "refuse": (r.get("是否應拒答") or "").strip() == "是",
            }
        )
    return qs


def make_answer_templates(qs: list[dict]) -> int:
    """每一題、每個條件各準備一個空白回答檔（已經有的不動）。"""
    made = 0
    for folder in ANSWERS.values():
        folder.mkdir(parents=True, exist_ok=True)
        for q in qs:
            p = folder / f"{q['id']}.txt"
            if not p.exists():
                p.write_text(f"# {q['id']}｜{q['question']}\n{HEADER_HINT}\n\n", encoding="utf-8")
                made += 1
    return made


# ─────────────────────────────────────────────
# 2. 知識庫裡有哪些出處網址
# ─────────────────────────────────────────────
def load_kb() -> tuple[set[str], dict[str, set[str]]]:
    all_urls: set[str] = set()
    by_id: dict[str, set[str]] = {}
    for d in KB_DIRS:
        for p in sorted(d.glob("*.json")):
            rec = json.loads(p.read_text(encoding="utf-8"))
            urls = {rec.get("source_url", ""), rec.get("image", {}).get("source_url", "")}
            urls |= {x.get("source_url", "") for x in rec.get("descriptions", [])}
            urls = {norm_url(u) for u in urls if u}
            by_id[rec["id"]] = urls
            all_urls |= urls
    return all_urls, by_id


URL_RE = re.compile(r"https?://[^\s<>\"'）)」』】，。、；]+")


def check_sources(text: str, kb_urls: set[str], right_urls: set[str]) -> dict:
    urls = [norm_url(u.rstrip(".,;:")) for u in URL_RE.findall(text)]
    in_kb = [u for u in urls if u in kb_urls]
    return {
        "kb_url": bool(in_kb),  # 附了知識庫裡真的有的網址
        "right_url": bool(right_urls)
        and any(u in right_urls for u in urls),  # 附的正好是答案那一筆
        "unknown_urls": [u for u in urls if u not in kb_urls],  # 對不上知識庫的網址（可能是編的）
        "museum_name": any(n in text for n in MUSEUM_NAMES),  # 至少提到典藏單位名稱
    }


# ─────────────────────────────────────────────
# 3. 工具呼叫對到題目（只有條件 B）
# ─────────────────────────────────────────────
def load_tool_calls() -> list[dict]:
    calls = []
    if TOOL_LOG.exists():
        for line in TOOL_LOG.read_text(encoding="utf-8").splitlines():
            try:
                c = json.loads(line)
                c["_t"] = datetime.fromisoformat(c["time"])
                calls.append(c)
            except (json.JSONDecodeError, KeyError, ValueError):
                continue
    return calls


def build_tool_map(answered_b: list[tuple[str, Path]], calls: list[dict]) -> dict[str, list[dict]]:
    """用回答檔的存檔時間切出每一題的時間範圍，範圍內的工具呼叫就算這一題的。"""
    ordered = sorted(answered_b, key=lambda x: x[1].stat().st_mtime)
    mapping: dict[str, list[dict]] = {}
    prev = None
    for qid, path in ordered:
        end = datetime.fromtimestamp(path.stat().st_mtime)
        start = prev if prev is not None else end - FIRST_WINDOW
        mapping[qid] = [c for c in calls if start < c["_t"] <= end]
        prev = end
    return mapping


def save_tool_map(mapping: dict[str, list[dict]]) -> None:
    with TOOL_MAP.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["題號", "工具（依時間，用｜分隔）", "參數（依時間，用｜分隔）", "呼叫時間"])
        for qid, cs in mapping.items():
            w.writerow(
                [
                    qid,
                    "｜".join(c["tool"] for c in cs),
                    "｜".join(json.dumps(c.get("args", {}), ensure_ascii=False) for c in cs),
                    "｜".join(c["_t"].strftime("%m/%d %H:%M:%S") for c in cs),
                ]
            )


def load_tool_map() -> dict[str, dict]:
    m = {}
    with TOOL_MAP.open(encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            m[r["題號"].strip()] = {
                "tools": split_multi(r.get("工具（依時間，用｜分隔）", "")),
                "args": r.get("參數（依時間，用｜分隔）", ""),
            }
    return m


# ─────────────────────────────────────────────
# 4. 每一題計分
# ─────────────────────────────────────────────
def refusal_match(text: str) -> str:
    for pat in REFUSAL_PATTERNS:
        m = re.search(pat, text)
        if m:
            return m.group(0)
    return ""


def scored_keywords(q: dict) -> tuple[list[str], list[str]]:
    """題目裡已經出現的關鍵字不算分（10/27 加：B09 題目就有「西施」，照抄題目也會得分）。
    回傳（要計分的關鍵字, 被排除的關鍵字）。"""
    qn = norm(q["question"])
    use = [k for k in q["keywords"] if norm(k) not in qn]
    return use, [k for k in q["keywords"] if k not in use]


def score_answer(q: dict, text: str, kb_urls: set[str], kb_by_id: dict[str, set[str]]) -> dict:
    t = norm(text)
    kws, _ = scored_keywords(q)
    hits = [k for k in kws if norm(k) in t]
    kw = len(hits) / len(kws) if kws and not q["refuse"] else None
    refusal = refusal_match(text)
    right = set().union(*(kb_by_id.get(i, set()) for i in q["source"])) if q["source"] else set()
    src = check_sources(text, kb_urls, right)
    return {
        "kw": kw,
        "hits": hits,
        "missed": [k for k in kws if k not in hits],
        "refusal": refusal,
        "refused_ok": (bool(refusal) if q["refuse"] else None),
        "false_refusal": (bool(refusal) and kw == 0)
        if not q["refuse"] and kw is not None
        else None,
        **src,
    }


def score_tools(q: dict, tm: dict | None) -> dict:
    if tm is None:
        return {"tools_used": [], "tool_ok": None, "param_ok": None}
    used = tm["tools"]
    tool_ok = None
    if q["tools"]:
        tool_ok = (not used) if q["tools"] == ["無"] else any(t in used for t in q["tools"])
    param_ok = (q["param"] in tm["args"]) if q["param"] else None
    return {"tools_used": used, "tool_ok": tool_ok, "param_ok": param_ok}


# ─────────────────────────────────────────────
# 5. 加總
# ─────────────────────────────────────────────
def summarize(rows: list[dict]) -> dict:
    def mean(vals):
        vals = [v for v in vals if v is not None]
        return (sum(vals) / len(vals)) if vals else None

    def count(key):
        vals = [r[key] for r in rows if r[key] is not None]
        return sum(bool(v) for v in vals), len(vals)

    answerable = [r for r in rows if not r["q"]["refuse"]]
    by_type: dict[str, float | None] = {}
    for typ in sorted({r["q"]["type"] for r in rows}):
        by_type[typ] = mean(r["kw"] for r in rows if r["q"]["type"] == typ)
    return {
        "n": len(rows),
        "kw_mean": mean(r["kw"] for r in rows),
        "kw_full": (
            sum(1 for r in rows if r["kw"] == 1),
            sum(1 for r in rows if r["kw"] is not None),
        ),
        "kb_url": (sum(r["kb_url"] for r in answerable), len(answerable)),
        "right_url": (
            sum(r["right_url"] for r in answerable if r["q"]["source"]),
            sum(1 for r in answerable if r["q"]["source"]),
        ),
        "museum": (sum(r["museum_name"] for r in answerable), len(answerable)),
        "unknown_urls": sum(len(r["unknown_urls"]) for r in rows),
        "refused_ok": count("refused_ok"),
        "false_refusal": count("false_refusal"),
        "tool_ok": count("tool_ok"),
        "param_ok": count("param_ok"),
        "by_type": by_type,
    }


# ─────────────────────────────────────────────
# 6. 輸出：純文字、CSV、HTML（含長條圖）
# ─────────────────────────────────────────────
def frac(pair: tuple[int, int]) -> float | None:
    return pair[0] / pair[1] if pair[1] else None


def summary_rows(S: dict) -> list[tuple[str, str, str, str]]:
    """（指標, A, B, 這個數字的意思）"""
    a, b = S.get("A"), S.get("B")
    g = lambda s, f: f(s) if s else "—"
    return [
        ("回答了幾題", g(a, lambda s: str(s["n"])), g(b, lambda s: str(s["n"])), "有貼回答的題數"),
        (
            "關鍵字命中率（平均）",
            g(a, lambda s: pct(s["kw_mean"])),
            g(b, lambda s: pct(s["kw_mean"])),
            "每題答案關鍵字出現幾成，再平均（該拒答的題不算）",
        ),
        (
            "關鍵字全部答對",
            g(a, lambda s: ratio(*s["kw_full"])),
            g(b, lambda s: ratio(*s["kw_full"])),
            "關鍵字一個都沒漏的題數",
        ),
        (
            "附上知識庫出處網址",
            g(a, lambda s: ratio(*s["kb_url"])),
            g(b, lambda s: ratio(*s["kb_url"])),
            "回答裡的網址，是知識庫裡真的有的出處",
        ),
        (
            "附的正好是答案那一筆",
            g(a, lambda s: ratio(*s["right_url"])),
            g(b, lambda s: ratio(*s["right_url"])),
            "出處網址就是「答案出自哪一筆」那件作品",
        ),
        (
            "提到典藏單位名稱",
            g(a, lambda s: ratio(*s["museum"])),
            g(b, lambda s: ratio(*s["museum"])),
            "沒附網址，但至少寫了臺史博／國家文化記憶庫",
        ),
        (
            "網址對不上知識庫",
            g(a, lambda s: str(s["unknown_urls"])),
            g(b, lambda s: str(s["unknown_urls"])),
            "回答裡出現、但知識庫沒有的網址數（可能是 AI 編的，要人工看）",
        ),
        (
            "該拒答時有拒答",
            g(a, lambda s: ratio(*s["refused_ok"])),
            g(b, lambda s: ratio(*s["refused_ok"])),
            "知識庫沒有的題目，回答有說找不到",
        ),
        (
            "不該拒答卻拒答",
            g(a, lambda s: ratio(*s["false_refusal"])),
            g(b, lambda s: ratio(*s["false_refusal"])),
            "明明有資料，卻說找不到而且一個關鍵字都沒答到",
        ),
        (
            "工具選對率",
            "—",
            g(b, lambda s: ratio(*s["tool_ok"])),
            "有用到這題應該用的工具（只有條件 B）",
        ),
        (
            "參數正確率",
            "—",
            g(b, lambda s: ratio(*s["param_ok"])),
            "工具參數裡有正確的作品編號（只有條件 B）",
        ),
    ]


def write_markdown(S: dict, per_type: list[str], fails: list[str]) -> None:
    L = [
        "# 評估結果",
        "",
        f"> 產生時間：{datetime.now():%Y-%m-%d %H:%M}（score.py 自動產生，重新執行會覆蓋）",
        "",
        "| 指標 | 條件 A（不接 MCP） | 條件 B（接上 MCP） | 意思 |",
        "| --- | --- | --- | --- |",
    ]
    L += [f"| {m} | {a} | {b} | {d} |" for m, a, b, d in summary_rows(S)]
    L += [
        "",
        "## 各類型題目的關鍵字命中率",
        "",
        "| 類型 | A | B |",
        "| --- | --- | --- |",
    ] + per_type
    L += ["", "## 可以挑來寫失敗分析的題目（條件 B 沒有全對的）", ""] + (
        fails or ["- 沒有（條件 B 每題都全對，或還沒有 B 的回答）"]
    )
    L += [
        "",
        "## 這些數字的限制",
        "",
        "- 每題只問一次。AI 每次的回答可能不同，同一題再問一次分數可能會變（原流程 6-3 的選做：挑 5 題多跑 2 次看穩不穩定）。",
        "- 關鍵字命中只看「字有沒有出現」，不看句子對不對；寫錯但剛好提到關鍵字也會算命中，所以失敗分析要人工讀回答。",
        "- 拒答是用「找不到、查不到…」這類字判斷的，對到的字列在 per_question.csv，請人工確認。",
        "- 工具呼叫是用回答檔的存檔時間對到題目的，對應結果在 tool_map_B.csv，可以人工修正。",
    ]
    (RESULTS / "score_summary.md").write_text("\n".join(L) + "\n", encoding="utf-8")


def write_csv(all_rows: dict[str, list[dict]]) -> None:
    with (RESULTS / "per_question.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "條件",
                "題號",
                "類型",
                "關鍵字命中",
                "答到的關鍵字",
                "漏掉的關鍵字",
                "附知識庫出處",
                "出處正好是答案那筆",
                "對不上知識庫的網址",
                "該拒答",
                "回答裡的拒答字眼",
                "用到的工具",
                "工具選對",
                "參數正確",
            ]
        )
        yn = lambda v: "—" if v is None else ("是" if v else "否")
        for cond, rows in all_rows.items():
            for r in rows:
                q = r["q"]
                w.writerow(
                    [
                        cond,
                        q["id"],
                        q["type"],
                        pct(r["kw"]),
                        "、".join(r["hits"]),
                        "、".join(r["missed"]),
                        yn(r["kb_url"]),
                        yn(r["right_url"] if q["source"] else None),
                        " ".join(r["unknown_urls"]),
                        "是" if q["refuse"] else "否",
                        r["refusal"],
                        "｜".join(r["tools_used"]),
                        yn(r["tool_ok"]),
                        yn(r["param_ok"]),
                    ]
                )


CHART_METRICS = [
    ("關鍵字命中率", "kw_mean"),
    ("附上知識庫出處", "kb_url"),
    ("該拒答時有拒答", "refused_ok"),
]


def chart_svg(S: dict) -> str:
    """A、B 兩組並排的橫條圖（同一個 0～100% 的刻度）。顏色：A＝藍、B＝橘。"""
    rows = []
    for label, key in CHART_METRICS:
        vals = {}
        for c in ("A", "B"):
            s = S.get(c)
            v = None if not s else (s[key] if key == "kw_mean" else frac(s[key]))
            vals[c] = v
        rows.append((label, vals))
    W, left, right, top = 640, 150, 56, 28
    bar_h, gap, group_gap = 18, 2, 22
    group_h = bar_h * 2 + gap
    H = top + len(rows) * (group_h + group_gap) + 24
    plot_w = W - left - right
    x = lambda v: left + plot_w * v
    out = [
        f'<svg viewBox="0 0 {W} {H}" width="100%" role="img" aria-label="條件 A 與 B 的比較長條圖" class="chart">'
    ]
    for t in (0, 0.25, 0.5, 0.75, 1):  # 淡淡的刻度線
        out.append(
            f'<line x1="{x(t):.1f}" y1="{top - 6}" x2="{x(t):.1f}" y2="{H - 24}" class="grid"/>'
        )
        out.append(
            f'<text x="{x(t):.1f}" y="{H - 8}" class="axis" text-anchor="middle">{int(t * 100)}%</text>'
        )
    y = top
    for label, vals in rows:
        out.append(
            f'<text x="{left - 10}" y="{y + group_h / 2 + 4:.1f}" class="label" text-anchor="end">{html.escape(label)}</text>'
        )
        for i, c in enumerate(("A", "B")):
            v = vals[c]
            by = y + i * (bar_h + gap)
            if v is None:
                out.append(
                    f'<text x="{left + 4}" y="{by + bar_h - 5}" class="muted">{c}：沒有資料</text>'
                )
                continue
            w = max(plot_w * v, 4)
            tip = f"{COND_NAME[c]}｜{label}：{v * 100:.0f}%"
            # 右端圓角 4px、左端貼齊基線：用 path 畫
            r = min(4, w / 2)
            d = (
                f"M{left},{by} H{left + w - r} Q{left + w},{by} {left + w},{by + r} "
                f"V{by + bar_h - r} Q{left + w},{by + bar_h} {left + w - r},{by + bar_h} H{left} Z"
            )
            out.append(f'<path d="{d}" class="bar-{c}"><title>{html.escape(tip)}</title></path>')
            out.append(
                f'<text x="{left + w + 6:.1f}" y="{by + bar_h - 5}" class="value">{v * 100:.0f}%</text>'
            )
        y += group_h + group_gap
    out.append(f'<line x1="{left}" y1="{top - 6}" x2="{left}" y2="{H - 24}" class="base"/>')
    out.append("</svg>")
    return "\n".join(out)


def write_html(S: dict, per_type_rows: list[tuple[str, str, str]], fails: list[str]) -> None:
    rows = "\n".join(
        f"<tr><td>{html.escape(m)}</td><td class='num'>{html.escape(a)}</td>"
        f"<td class='num'>{html.escape(b)}</td><td class='muted'>{html.escape(d)}</td></tr>"
        for m, a, b, d in summary_rows(S)
    )
    trows = "\n".join(
        f"<tr><td>{html.escape(t)}</td><td class='num'>{a}</td><td class='num'>{b}</td></tr>"
        for t, a, b in per_type_rows
    )
    flist = (
        "\n".join(f"<li>{html.escape(f[2:])}</li>" for f in fails)
        or "<li>沒有（條件 B 每題都全對，或還沒有 B 的回答）</li>"
    )
    page = f"""<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>評估結果</title>
<style>
:root{{--surface:#fcfcfb;--page:#f9f9f7;--ink:#0b0b0b;--ink2:#52514e;--muted:#898781;--grid:#e1e0d9;--base:#c3c2b7;--a:#2a78d6;--b:#eb6834;--border:rgba(11,11,11,.10)}}
@media (prefers-color-scheme:dark){{:root{{--surface:#1a1a19;--page:#0d0d0d;--ink:#f0efec;--ink2:#c3c2b7;--muted:#898781;--grid:#2c2c2a;--base:#383835;--a:#3987e5;--b:#d95926;--border:rgba(255,255,255,.10)}}}}
body{{margin:0;background:var(--page);color:var(--ink);font-family:"Microsoft JhengHei","PingFang TC","Noto Sans TC",sans-serif;line-height:1.6}}
main{{max-width:880px;margin:0 auto;padding:24px 16px}}
section{{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:16px 20px;margin:16px 0}}
h1{{font-size:22px;margin:0 0 4px}} h2{{font-size:17px;margin:0 0 10px}} .note{{color:var(--ink2);font-size:13px}}
table{{border-collapse:collapse;width:100%;font-size:14px}} th,td{{border-bottom:1px solid var(--grid);padding:6px 8px;text-align:left;vertical-align:top}}
th{{color:var(--ink2);font-weight:600}} .num{{white-space:nowrap;font-variant-numeric:tabular-nums}} .muted{{color:var(--ink2);font-size:13px}}
.legend{{display:flex;gap:16px;font-size:13px;color:var(--ink2);margin-bottom:6px}} .sw{{display:inline-block;width:12px;height:12px;border-radius:3px;margin-right:6px;vertical-align:-1px}}
.chart .grid{{stroke:var(--grid);stroke-width:1}} .chart .base{{stroke:var(--base);stroke-width:1}}
.chart .axis{{fill:var(--muted);font-size:11px}} .chart .label{{fill:var(--ink);font-size:13px}} .chart .value{{fill:var(--ink2);font-size:12px}} .chart .muted{{fill:var(--muted);font-size:12px}}
.chart .bar-A{{fill:var(--a)}} .chart .bar-B{{fill:var(--b)}} .chart path:hover{{opacity:.85}}
</style></head><body><main>
<h1>評估結果：接上 MCP 前後的比較</h1>
<div class="note">產生時間 {datetime.now():%Y-%m-%d %H:%M}｜score.py 自動產生｜每題只問一次，限制見最下方</div>
<section><h2>三個主要數字</h2>
<div class="legend"><span><span class="sw" style="background:var(--a)"></span>條件 A（不接 MCP）</span><span><span class="sw" style="background:var(--b)"></span>條件 B（接上 MCP）</span></div>
{chart_svg(S)}
<div class="note">滑鼠移到長條上可以看數字；完整數字在下面的表。</div></section>
<section><h2>全部指標</h2><table><tr><th>指標</th><th>條件 A</th><th>條件 B</th><th>意思</th></tr>
{rows}</table></section>
<section><h2>各類型題目的關鍵字命中率</h2><table><tr><th>類型</th><th>A</th><th>B</th></tr>
{trows}</table></section>
<section><h2>可以挑來寫失敗分析的題目</h2><ul>{flist}</ul></section>
<section><h2>這些數字的限制</h2><ul class="muted">
<li>每題只問一次，AI 每次的回答可能不同。</li>
<li>關鍵字命中只看字有沒有出現，不看句子對不對；失敗分析要人工讀回答。</li>
<li>拒答用「找不到、查不到…」這類字判斷，請對照 per_question.csv 人工確認。</li>
<li>工具呼叫用回答檔的存檔時間對到題目，對應結果在 tool_map_B.csv，可以人工修正。</li></ul></section>
</main></body></html>
"""
    (RESULTS / "report.html").write_text(page, encoding="utf-8")


# ─────────────────────────────────────────────
# 主程式
# ─────────────────────────────────────────────
def main() -> int:
    RESULTS.mkdir(parents=True, exist_ok=True)
    qs = load_questions()
    made = make_answer_templates(qs)
    say(f"題目 {len(qs)} 題（eval/questions.csv）")
    for q in qs:
        _, dropped = scored_keywords(q)
        if dropped:
            say(f"  {q['id']}：關鍵字「{'、'.join(dropped)}」題目裡就有，不計分")
    if made:
        say(f"建立了 {made} 個空白回答檔（eval/answers/A、B），把回答貼進去存檔就好")
    kb_urls, kb_by_id = load_kb()
    say(f"知識庫出處網址 {len(kb_urls)} 個（用來判斷回答的出處是不是真的）")

    answered = {
        c: [
            (q["id"], ANSWERS[c] / f"{q['id']}.txt")
            for q in qs
            if answer_body(ANSWERS[c] / f"{q['id']}.txt")
        ]
        for c in ANSWERS
    }
    for c in ANSWERS:
        say(f"{COND_NAME[c]}：已貼回答 {len(answered[c])}/{len(qs)} 題")

    # 工具呼叫對到題目（只有 B）
    calls = load_tool_calls()
    tool_map: dict[str, dict] = {}
    if answered["B"]:
        if TOOL_MAP.exists():
            tool_map = load_tool_map()
            missing = [(qid, p) for qid, p in answered["B"] if qid not in tool_map]
            if missing:
                fresh = build_tool_map(answered["B"], calls)
                for qid, _ in missing:
                    tool_map[qid] = {
                        "tools": [c["tool"] for c in fresh.get(qid, [])],
                        "args": "｜".join(
                            json.dumps(c.get("args", {}), ensure_ascii=False)
                            for c in fresh.get(qid, [])
                        ),
                    }
                say(
                    f"tool_map_B.csv 已存在，沿用；另外補上 {len(missing)} 題新的對應（沒有寫回檔案）"
                )
            else:
                say("tool_map_B.csv 已存在，沿用裡面的對應")
        else:
            fresh = build_tool_map(answered["B"], calls)
            save_tool_map(fresh)
            tool_map = {
                q: {
                    "tools": [c["tool"] for c in cs],
                    "args": "｜".join(
                        json.dumps(c.get("args", {}), ensure_ascii=False) for c in cs
                    ),
                }
                for q, cs in fresh.items()
            }
            say(
                f"依存檔時間把 {sum(len(v) for v in fresh.values())} 次工具呼叫對到題目，存成 eval/results/tool_map_B.csv"
            )
    if answered["A"] and calls:  # 條件 A 應該完全沒有工具呼叫
        a_map = build_tool_map(answered["A"], calls)
        leaked = [q for q, cs in a_map.items() if cs]
        if leaked:
            say(
                f"⚠ 條件 A 期間有工具呼叫（{'、'.join(leaked)}）：A 應該關掉 art-rag，這幾題的 A 可能不乾淨"
            )

    # 計分
    qmap = {q["id"]: q for q in qs}
    all_rows: dict[str, list[dict]] = {}
    S: dict[str, dict] = {}
    for c in ANSWERS:
        rows = []
        for qid, path in answered[c]:
            q = qmap[qid]
            r = {
                "q": q,
                **score_answer(q, answer_body(path), kb_urls, kb_by_id),
                **score_tools(q, tool_map.get(qid) if c == "B" else None),
            }
            rows.append(r)
        all_rows[c] = rows
        if rows:
            S[c] = summarize(rows)

    types = sorted({q["type"] for q in qs})
    per_type_rows = [
        (
            t,
            pct(S["A"]["by_type"].get(t)) if "A" in S else "—",
            pct(S["B"]["by_type"].get(t)) if "B" in S else "—",
        )
        for t in types
    ]
    per_type_md = [f"| {t} | {a} | {b} |" for t, a, b in per_type_rows]
    fails = []
    for r in all_rows.get("B", []):
        q = r["q"]
        why = []
        if r["kw"] is not None and r["kw"] < 1:
            why.append(f"漏了關鍵字：{'、'.join(r['missed'])}")
        if r["refused_ok"] is False:
            why.append("該拒答卻沒拒答")
        if r["false_refusal"]:
            why.append(f"不該拒答卻說「{r['refusal']}」")
        if r["tool_ok"] is False:
            why.append(
                f"工具用錯（用了：{'、'.join(r['tools_used']) or '沒用工具'}；應該用：{'或'.join(q['tools'])}）"
            )
        if r["unknown_urls"]:
            why.append(f"有 {len(r['unknown_urls'])} 個網址對不上知識庫")
        if why:
            fails.append(f"- {q['id']}（{q['type']}）{q['question'][:30]}…：{'；'.join(why)}")

    write_markdown(S, per_type_md, fails)
    write_csv(all_rows)
    write_html(S, per_type_rows, fails)

    say("")
    say(f"{'指標':<16}{'A':>14}{'B':>14}")
    for m, a, b, _ in summary_rows(S):
        say(f"{m:<16}{a:>14}{b:>14}")
    say("")
    say(
        "結果在 eval/results/：report.html（雙擊用瀏覽器看）、score_summary.md、per_question.csv、tool_map_B.csv"
    )
    (RESULTS / "score_output.txt").write_text("\n".join(OUT_LINES) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
