"""
test_search.py：搜尋小測驗（階段 4）

讀 eval/search_questions.csv 裡的題目，量三件事：
  1. Top-3 命中率：正確的段落有沒有排進前 3 名，分「沒加分」和「有加分」各量一次
  2. 去重前，前 3 名被「同一段話」重複佔掉的題數（證明去重有沒有必要）
  3. 「找不到」門檻：有答案的題目、知識庫沒有的題目，第 1 名各拿幾分 → 門檻設多少才分得開

題目檔欄位：
  id, question, should_find（是／否）, expected_artwork, expected_topic, expected_snippet, note
  expected_snippet：正確段落裡的一小句原文（| 分隔可以填好幾個），用來認出正確段落
  should_find＝否：知識庫裡沒有答案的題目（用來校正「找不到」門檻），expected_* 留空

結果：畫面上看得到，也會存成
  eval/results/search_latest.txt（教練直接讀這個，不用截圖）
  eval/results/search_<日期時間>.csv（每題的詳細結果，可以用 Excel 開）
"""

import csv
import sys
import time

import search

QUESTIONS = search.ROOT / "eval" / "search_questions.csv"
RESULT_DIR = search.ROOT / "eval" / "results"
LINES: list[str] = []


def say(text: str = "") -> None:
    """印到畫面，同時記下來，最後存成 search_latest.txt。"""
    print(text)
    LINES.append(text)


def is_answer(row: dict, q: dict) -> bool:
    """這一列是不是正確段落。
    有填 expected_snippet（正確段落裡的一小句話，| 分隔可填好幾個）時，看段落文字有沒有包含它：
      這樣不管重複段落去重後留下哪一張、索引重建後段落編號有沒有變，都認得出來。
    沒填時，退回用「作品編號＋段落主題開頭」判斷（舊的示範題用這種）。"""
    p = row["passage"]
    snippets = [x.strip() for x in (q.get("expected_snippet") or "").split("|") if x.strip()]
    if snippets:
        return any(x in p.text for x in snippets)
    arts = [p.artwork_id] + [a["artwork_id"] for a in row.get("also_in", [])]
    topic = q.get("expected_topic") or ""
    return q["expected_artwork"] in arts and (not topic or p.topic.startswith(topic))


def rank_of_answer(rows: list[dict], q: dict) -> int | None:
    """正確段落排第幾名（只看前 3 名）；不在前 3 名就回傳 None。"""
    for i, r in enumerate(rows[: search.TOP_K], start=1):
        if is_answer(r, q):
            return i
    return None


def top3_text(rows: list[dict]) -> str:
    return "；".join(
        f"{r['passage'].title}｜{r['passage'].topic}（{r['score']:.3f}）"
        for r in rows[: search.TOP_K]
    )


def threshold_table(yes: list[float], no: list[float]) -> None:
    """門檻試算：每個候選門檻下，有幾題會判錯。"""
    say("門檻試算（第 1 名分數低於門檻＝判定「找不到」）：")
    say(f"   {'門檻':<6}{'有答案卻說找不到':<14}{'沒答案卻說找到了':<14}")
    for t in [x / 100 for x in range(45, 86, 5)]:
        miss = sum(s < t for s in yes)
        false_found = sum(s >= t for s in no)
        mark = " ← 現在的設定" if abs(t - search.MIN_SCORE) < 1e-9 else ""
        say(f"   {t:<8.2f}{miss:>3} / {len(yes):<12}{false_found:>3} / {len(no):<8}{mark}")
    if yes and no:
        if max(no) < min(yes):
            mid = round((max(no) + min(yes)) / 2, 2)
            say(
                f"→ 兩組分得開：沒答案最高 {max(no):.3f} ＜ 有答案最低 {min(yes):.3f}，建議門檻約 {mid}"
            )
        else:
            say(
                f"→ 兩組有重疊：沒答案最高 {max(no):.3f} ≥ 有答案最低 {min(yes):.3f}，"
                "任何門檻都會判錯幾題，要選「比較能接受哪一種錯」"
            )


def main() -> int:
    s = search.Searcher()
    if s.stale:
        say("⚠ 資料改過但索引是舊的，請先雙擊 build_index.bat")
    with QUESTIONS.open(encoding="utf-8-sig") as f:
        questions = list(csv.DictReader(f))
    findable = [q for q in questions if (q.get("should_find") or "是").strip() != "否"]
    unfindable = [q for q in questions if q not in findable]
    say(
        f"模型：{s.meta['model']}　有答案的題目：{len(findable)}　知識庫沒有的題目：{len(unfindable)}"
    )
    say(f"時間：{time.strftime('%Y-%m-%d %H:%M')}　目前門檻 MIN_SCORE＝{search.MIN_SCORE}")
    say()

    # ── 1. 命中率（兩種都有去重），2. 去重前被重複佔位 ──
    say(f"{'題號':<5}{'沒加分':<7}{'有加分':<7}{'第1名分數':<9}問題")
    rows_out, yes_scores, no_scores = [], [], []
    hits = {"沒加分": 0, "有加分": 0}
    crowded = 0
    misses = []
    for q in findable:
        plain = s.ranked(q["question"], use_bonus=False)
        bonus = s.ranked(q["question"], use_bonus=True)
        raw = s.ranked(q["question"], use_bonus=True, dedup=False)
        texts = [r["passage"].text for r in raw[: search.TOP_K]]
        if len(set(texts)) < len(texts):
            crowded += 1
        r_plain = rank_of_answer(plain, q)
        r_bonus = rank_of_answer(bonus, q)
        hits["沒加分"] += r_plain is not None
        hits["有加分"] += r_bonus is not None
        best = bonus[0]["score"]
        yes_scores.append(best)
        show = lambda r: f"第{r}名" if r else "沒命中"
        say(f"{q['id']:<6}{show(r_plain):<7}{show(r_bonus):<7}{best:<11.3f}{q['question']}")
        if r_bonus is None or r_plain is None:
            misses.append((q, plain, bonus))
        rows_out.append(
            {
                **q,
                "rank_without_bonus": r_plain or "",
                "rank_with_bonus": r_bonus or "",
                "best_score": best,
                "top3_with_bonus": top3_text(bonus),
            }
        )

    for q in unfindable:
        bonus = s.ranked(q["question"], use_bonus=True)
        best = bonus[0]["score"]
        no_scores.append(best)
        say(f"{q['id']:<6}{'（知識庫沒有）':<12}{best:<11.3f}{q['question']}")
        rows_out.append(
            {
                **q,
                "rank_without_bonus": "",
                "rank_with_bonus": "",
                "best_score": best,
                "top3_with_bonus": top3_text(bonus),
            }
        )

    n = len(findable)
    say()
    if n:
        say(
            f"Top-3 命中率　沒加分：{hits['沒加分']}/{n}（{hits['沒加分'] / n:.0%}）"
            f"　有加分：{hits['有加分']}/{n}（{hits['有加分'] / n:.0%}）"
        )
        say(f"去重前，前 3 名裡出現「同一段話」的題數：{crowded}/{n}")
    say()

    # ── 3. 門檻 ──
    threshold_table(yes_scores, no_scores)

    # ── 沒命中的題目：前 3 名是誰（寫失敗分析用）──
    if misses:
        say()
        say("沒命中（至少一種沒命中）的題目，前 3 名是：")
        for q, plain, bonus in misses:
            say(
                f"  {q['id']} {q['question']}（正確：{q['expected_artwork']}｜{q['expected_topic']}｜含「{q.get('expected_snippet', '')}」）"
            )
            say(f"     沒加分：{top3_text(plain)}")
            say(
                f"     有加分：{top3_text(bonus)}　對上的名字：{bonus[0]['matched_keywords'] or '無'}"
            )

    if s.meta["fake"]:
        say()
        say("（注意：這是假向量的結果，只能證明程式會跑，不代表真實效果）")

    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    out = RESULT_DIR / f"search_{time.strftime('%m%d_%H%M')}.csv"
    with out.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows_out[0].keys()))
        w.writeheader()
        w.writerows(rows_out)
    say()
    say(
        f"結果已存到 {out.relative_to(search.ROOT)}（可以用 Excel 開）和 eval\\results\\search_latest.txt"
    )
    (RESULT_DIR / "search_latest.txt").write_text("\n".join(LINES), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
