"""
diag_search.py：步驟 7「找錯的例子」用的實驗（階段 4，10/26）

不會改任何資料、卡櫃或 search.py；只是用不同方式搜搜看，把結果存成文字檔給教練讀。

實驗 A（Q09b「隱逸和田園生活」沒命中）——一次只改一件事：
  A-1 只改「問題」：原題／改成完整句子／加作品名／前面加 bge 官方提示句
      → 看正確的卡（山水版印花條・用途與寓意）排第幾、幾分
  A-2 只改「卡」：同一個問題，跟「整張卡」比、跟「只留最後一句」比、跟「只留前半段」比
      → 如果只留最後一句的分數高很多，就證明「意思被稀釋」

實驗 B（Q01a 去重漏掉「差一個字」的段落）：
  現在的去重：文字一字不差才算重複
  試驗的去重：文字有 97% 以上相同就算重複
  → 看 Q01a 的前 3 名變成什麼，並用全部 20 題確認命中率沒有變差

結果：eval/results/diag_latest.txt
"""

import csv
import difflib
import sys
import time

import search

RESULT = search.ROOT / "eval" / "results" / "diag_latest.txt"
QUESTIONS = search.ROOT / "eval" / "search_questions.csv"
LINES: list[str] = []
NEAR_DUP = 0.97  # 試驗：文字相同比例 ≥ 97% 就當成重複
BGE_PREFIX = "為這個句子生成表示以用於檢索相關文章："


def say(text: str = "") -> None:
    print(text)
    LINES.append(text)


def find_rank(rows: list[dict], artwork_id: str, snippet: str) -> tuple[int | None, float | None]:
    """在完整排名裡找正確的卡：排第幾名、幾分。"""
    for i, r in enumerate(rows, start=1):
        p = r["passage"]
        if p.artwork_id == artwork_id and snippet in p.text:
            return i, r["score"]
    return None, None


def short(r: dict) -> str:
    p = r["passage"]
    return f"{p.title}｜{p.topic}（{r['score']:.3f}）"


def near_dedup(rows: list[dict]) -> list[dict]:
    """試驗的去重：文字有 NEAR_DUP 以上相同，就只留分數高的那張。"""
    kept: list[dict] = []
    for r in rows:
        t = r["passage"].text
        if any(
            abs(len(t) - len(k["passage"].text)) <= 20
            and difflib.SequenceMatcher(None, t, k["passage"].text).ratio() >= NEAR_DUP
            for k in kept
        ):
            continue
        kept.append(r)
    return kept


def is_answer(p, q: dict) -> bool:
    snippets = [x.strip() for x in (q.get("expected_snippet") or "").split("|") if x.strip()]
    return any(x in p.text for x in snippets)


def experiment_a(s: search.Searcher) -> None:
    art, snip = "nmth-20030080150", "隱逸"
    say("═" * 60)
    say("實驗 A-1：只改「問題」，卡櫃和程式都不變")
    say("  正確的卡：山水版印花條｜用途與寓意（含「隱逸以及田園生活」）")
    say(f"  門檻 MIN_SCORE＝{search.MIN_SCORE}（第 1 名低於它＝找不到）")
    say("═" * 60)
    variants = [
        ("原題（7 個字）", "隱逸和田園生活", False),
        ("改成完整句子（不加作品名）", "哪一件作品的寓意象徵隱逸和田園生活？", False),
        ("原題＋作品名", "山水版印花條：隱逸和田園生活", False),
        ("原題，前面加 bge 官方提示句", "隱逸和田園生活", True),
    ]
    for label, q, prefix in variants:
        query = BGE_PREFIX + q if prefix else q
        rows = s.ranked(query, use_bonus=True)
        rank, score = find_rank(rows, art, snip)
        top = rows[0]
        verdict = "命中" if rank and rank <= search.TOP_K else "沒命中"
        say(f"\n【{label}】{q}")
        say(f"  正確的卡：第 {rank} 名（共 {len(rows)} 張，去重後）　分數 {score:.3f}　→ {verdict}")
        say(
            f"  第 1 名：{short(top)}　→ {'交出 3 張卡' if top['score'] >= search.MIN_SCORE else '判定找不到'}"
        )
        say(f"  前 3 名：{'；'.join(short(r) for r in rows[:3])}")

    say()
    say("═" * 60)
    say("實驗 A-2：只改「卡」，問題固定是「隱逸和田園生活」")
    say("  （卡的前面一樣加上「作品名（作者）｜主題｜」，跟建索引時的做法相同）")
    say("═" * 60)
    p = next(x for x in s.passages if x.artwork_id == art and snip in x.text)
    head = f"{p.title}（{p.artist}）｜{p.topic}｜"
    last = "山水與漁、樵、耕、讀常成為古代畫家的繪畫主題，以之象徵隱逸以及田園生活之恬淡境界。"
    first = "版印花條多做為糊紙的配件，用於屋宇類之裝飾，如靈厝、神祇宮殿等。"
    cards = [
        (f"整張卡（{len(p.text)} 字，就是卡櫃裡那張）", p.text),
        (f"只留最後一句（{len(last)} 字，講隱逸的那句）", last),
        (f"只留第一句（{len(first)} 字，講糊紙、靈厝）", first),
    ]
    vecs = s.embedder.embed([head + t for _, t in cards] + ["隱逸和田園生活"])
    q = vecs[-1]
    for (label, _), v in zip(cards, vecs[:-1]):
        say(f"  {label}：跟問題的相似度 {float(v @ q):.3f}")
    say("  （對照：成績單上這題的第 1 名，韓愈過秦嶺，是 0.507）")


def experiment_b(s: search.Searcher) -> None:
    say()
    say("═" * 60)
    say(f"實驗 B：去重的規則——現在「一字不差」 vs 試驗「{NEAR_DUP:.0%} 以上相同」")
    say("═" * 60)
    q01a = "陳天乞出生在哪裡？什麼時候來臺的？是在哪裡開始學剪黏？"
    now = s.ranked(q01a, use_bonus=True)
    trial = near_dedup(s.ranked(q01a, use_bonus=True, dedup=False))
    say(f"\n【Q01a】{q01a}")
    say(f"  現在的去重，前 3 名：{'；'.join(short(r) for r in now[:3])}")
    say(f"  試驗的去重，前 3 名：{'；'.join(short(r) for r in trial[:3])}")

    with QUESTIONS.open(encoding="utf-8-sig") as f:
        qs = [q for q in csv.DictReader(f) if (q.get("should_find") or "是").strip() != "否"]
    hit_now = hit_trial = crowd_now = crowd_trial = 0
    changed = []
    for q in qs:
        a = s.ranked(q["question"], use_bonus=True)[:3]
        b = near_dedup(s.ranked(q["question"], use_bonus=True, dedup=False))[:3]
        hit_now += any(is_answer(r["passage"], q) for r in a)
        hit_trial += any(is_answer(r["passage"], q) for r in b)

        def crowded(rows):  # 前 3 名裡有沒有兩張「幾乎一樣」（97% 以上相同）
            t = [r["passage"].text for r in rows]
            return any(
                difflib.SequenceMatcher(None, t[i], t[j]).ratio() >= NEAR_DUP
                for i in range(len(t))
                for j in range(i + 1, len(t))
            )

        crowd_now += crowded(a)
        crowd_trial += crowded(b)
        if [r["passage"].passage_id for r in a] != [r["passage"].passage_id for r in b]:
            changed.append(q["id"])
    n = len(qs)
    say()
    say(f"  全部 {n} 題 Top-3 命中率：現在 {hit_now}/{n}　試驗 {hit_trial}/{n}")
    say(f"  去重後前 3 名仍有「幾乎一樣」的卡：現在 {crowd_now}/{n} 題　試驗 {crowd_trial}/{n} 題")
    say(f"  前 3 名有變動的題目：{'、'.join(changed) if changed else '無'}")


def main() -> int:
    s = search.Searcher()
    if s.stale:
        say("⚠ 資料改過但索引是舊的，請先雙擊 build_index.bat")
    say(f"診斷實驗（步驟 7）　時間：{time.strftime('%Y-%m-%d %H:%M')}　模型：{s.meta['model']}")
    experiment_a(s)
    experiment_b(s)
    RESULT.parent.mkdir(parents=True, exist_ok=True)
    say()
    say("結果已存到 eval\\results\\diag_latest.txt，跟教練說「跑完了」就好")
    RESULT.write_text("\n".join(LINES), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
