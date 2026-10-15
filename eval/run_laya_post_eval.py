"""Laya 第 4、6 段的評估：題目（預設 eval/laya_post_blind.jsonl）照 eval/laya_post.py
產生和 art-rag 實際送出一字不差的請求，真的送給本機 Laya（Unsloth Studio 的 Decision API），
和標準答案比對。

- 第 4 段 is_relevant：準確率、精確率、召回率（相關＝標準答案）
- 第 4 段 security_leak_check：有洩密風險的段落抓到幾段、乾淨的段落誤判幾段
- 第 4 段留下與否（相關而且沒洩密才留下）：**有洩密風險卻留下**（最危險）、相關卻被剔除各幾段
- 第 6 段 answerable、compliant 的準確率；閘門（兩題都過才放行）**不該放行卻放行**、
  該放行卻擋下各幾題
第 6 段的輸入是照「第 4、5 段都判對」產生的，量的是第 6 段自己的判斷。門檻用 shared/agent.yaml 的
guard.jev_threshold。不用開後端；要開 Unsloth Studio（.env 的 LAYA_*）。
量的是判得對不對，所以逾時放寬成 --timeout（預設 60 秒）；另外列出每筆花多久、
有幾筆超過後端實際的上限（第 4～6 段：guard.POST_TIMEOUT_S 和 LAYA_TIMEOUT_S 取大的，
超過就當沒判斷）。
結果存 eval/runs/experiments/<run_id>-laya-post.json
（不放 eval/runs/ 根目錄：評估頁會把那裡的 .json 當問答評估讀）。

用法：uv run --project backend python eval/run_laya_post_eval.py [--file laya_post_blind.jsonl]
                                                           [--timeout 60]
"""

import argparse
import asyncio
import functools
import json
import statistics
import uuid
from collections import Counter
from datetime import UTC, datetime

import laya_post

from app.agent import guard, jev
from app.core.config import get_settings

print = functools.partial(print, flush=True)  # noqa: A001 — 背景執行時也能即時看到進度
EVAL = laya_post.ROOT / "eval"


def rate(hits: int, total: int) -> float | None:
    return round(hits / total, 4) if total else None


def pct(x: float | None) -> str:
    return "—" if x is None else f"{x:.0%}"


async def judge(rows: list[dict], th: float) -> tuple[list[dict], str]:
    out, model = [], ""
    for r in rows:
        reply = await jev.ask(r["state"], r["questions"], cloud=False)
        model = reply.model
        probs = {name: reply.noul(name) for name in r["questions"]}
        pred = {name: p >= th for name, p in probs.items()}
        out.append({**r, "probs": probs, "pred": pred, "ms": reply.latency_ms})
        bad = [n for n in pred if pred[n] != r["gold"][n]]
        print(
            f"  {'✓' if not bad else '✗'} {r['id']:<12} 錯 {len(bad)}/{len(pred)} "
            f"{reply.latency_ms} ms {bad[:4]}"
        )
    return out, model


def summarize(rows: list[dict], limit_ms: float) -> dict:
    s4 = [r for r in rows if r["stage"] == 4]
    s6 = [r for r in rows if r["stage"] == 6]
    c: Counter = Counter()
    for r in s4:
        for name in r["questions"]:
            pid, _, kind = name.partition("_")
            c[kind, r["gold"][name], r["pred"][name]] += 1
            if kind == "relevant":
                keep_gold = r["gold"][name] and not r["gold"][f"{pid}_leak"]
                keep_pred = r["pred"][name] and not r["pred"][f"{pid}_leak"]
                c["keep", keep_gold, keep_pred] += 1
                if keep_pred and r["gold"][f"{pid}_leak"]:
                    c["leak_kept"] += 1
    for r in s6:
        for name in ("answerable", "compliant"):
            c[name, r["gold"][name], r["pred"][name]] += 1
        g = r["gold"]["answerable"] and r["gold"]["compliant"]
        p = r["pred"]["answerable"] and r["pred"]["compliant"]
        c["gate", g, p] += 1

    def acc(kind: str) -> float | None:
        ok = c[kind, True, True] + c[kind, False, False]
        return rate(ok, ok + c[kind, True, False] + c[kind, False, True])

    return {
        "stage4_rows": len(s4),
        "stage6_rows": len(s6),
        "relevant_accuracy": acc("relevant"),
        "relevant_precision": rate(
            c["relevant", True, True], c["relevant", True, True] + c["relevant", False, True]
        ),
        "relevant_recall": rate(
            c["relevant", True, True], c["relevant", True, True] + c["relevant", True, False]
        ),
        "leak_caught": f"{c['leak', True, True]}/{c['leak', True, True] + c['leak', True, False]}",
        "leak_false_alarm": f"{c['leak', False, True]}/"
        f"{c['leak', False, True] + c['leak', False, False]}",
        "keep_accuracy": acc("keep"),
        "leaked_but_kept": c["leak_kept"],
        "relevant_but_dropped": c["keep", True, False],
        "answerable_accuracy": acc("answerable"),
        "compliant_accuracy": acc("compliant"),
        "gate_accuracy": acc("gate"),
        "gate_false_pass": c["gate", False, True],
        "gate_false_block": c["gate", True, False],
        "limit_ms": limit_ms,
        **{
            f"stage{n}_{k}": v
            for n, part in ((4, s4), (6, s6))
            if part
            for k, v in (
                ("p50_ms", statistics.median(r["ms"] for r in part)),
                ("max_ms", max(r["ms"] for r in part)),
                ("over_limit", sum(r["ms"] > limit_ms for r in part)),
            )
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", default="laya_post_blind.jsonl", help="eval/ 底下的題目檔")
    parser.add_argument("--timeout", type=float, default=60.0, help="評估時給 Laya 的秒數")
    args = parser.parse_args()
    items = laya_post.load_items(EVAL / args.file)
    if problems := laya_post.check_items(items, laya_post.load_store()):
        print("\n".join(problems))
        return 1
    ok, detail = jev.laya_status()
    if not ok:
        print(f"叫不到 Laya：{detail}")
        return 1
    th = float(guard._cfg()["jev_threshold"])
    settings = get_settings()
    limit_ms = max(guard.POST_TIMEOUT_S, settings.laya_timeout_s) * 1000
    settings.laya_timeout_s = args.timeout
    rows = laya_post.build(items)
    print(f"第 4、6 段評估：{len(items)} 題 → {len(rows)} 筆請求 · {detail} · 門檻 {th}")
    try:
        judged, model = asyncio.run(judge(rows, th))
    except jev.JevUnavailable as e:
        print(f"Laya 回應失敗：{e}")
        return 1
    s = summarize(judged, limit_ms)
    print(f"\n摘要（{model}）")
    print(
        f"  第 4 段 is_relevant：準確率 {pct(s['relevant_accuracy'])} · 精確率 "
        f"{pct(s['relevant_precision'])} · 召回率 {pct(s['relevant_recall'])}"
    )
    print(f"  第 4 段 security_leak_check：抓到 {s['leak_caught']} · 誤判 {s['leak_false_alarm']}")
    print(
        f"  第 4 段留下與否：準確率 {pct(s['keep_accuracy'])} · 有洩密風險卻留下 "
        f"{s['leaked_but_kept']} 段 · 相關卻剔除 {s['relevant_but_dropped']} 段"
    )
    print(
        f"  第 6 段：answerable {pct(s['answerable_accuracy'])} · compliant "
        f"{pct(s['compliant_accuracy'])} · 閘門 {pct(s['gate_accuracy'])}"
        f"（不該放行卻放行 {s['gate_false_pass']} 題、該放行卻擋下 {s['gate_false_block']} 題）"
    )
    for n in (4, 6):
        if f"stage{n}_p50_ms" in s:
            print(
                f"  第 {n} 段耗時：p50 {s[f'stage{n}_p50_ms']} ms"
                f" · 最久 {s[f'stage{n}_max_ms']} ms · 超過後端上限 {limit_ms:.0f} ms 的"
                f" {s[f'stage{n}_over_limit']} 筆（實際會當沒判斷）"
            )
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:4]
    out = EVAL / "runs" / "experiments" / f"{run_id}-laya-post.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "created_at": datetime.now(UTC).isoformat(),
                "file": args.file,
                "model": model,
                "threshold": th,
                "summary": s,
                "rows": [
                    {k: r[k] for k in ("id", "stage", "gold", "pred", "probs", "ms")}
                    for r in judged
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\n已存 {out.relative_to(laya_post.ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
