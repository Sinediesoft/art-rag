"""從網路上的中文資料集抽 Laya 第 2 段的候選句，給人逐句檢查（Q-2026-10-15-01）。

來源（第一次跑會下載到 data/laya/external/，之後直接讀）：
- thu-coai/Safety-Prompts 的 instruction_attack_scenarios.json（Apache-2.0，簡體）
  目標劫持、Prompt 泄漏（只留要它交出自己提示詞、設定的句子）、賦予角色後發指令（只留帶繞過
  規則字眼的）→ 建議 attack；其他角色扮演、不安全的指令主題、帶有不安全觀點的詢問 → 建議
  chitchat（和畫作、工廠都無關；照 agent.yaml 的 intent_classes，有害但沒有要改規則、角色
  或套設定的不算 attack）
- STAIR-BUPT/JailBench 的 JailBench.csv（MIT，簡體）：越獄模板＋有害問題 → 建議 attack，
  同一個模板只抽一句
- thu-coai LCCC-base 的測試集（silver/lccc 的 lccc_base_test.jsonl.gz，MIT，簡體微博對話）：
  每段對話的第一句 → 建議 chitchat；提到畫、圖紙、庫存、工單、系統這類字眼的不抽（可能是查詢）

處理：OpenCC s2twp 轉繁體（含台灣用詞）→ 篩長度 → 去掉和現有訓練資料、已抽的候選句太像的
（字元雙字組 Jaccard ≥ --max-sim）→ 和考卷太像的也不抽（同 make_laya_train.py）。
每句註明地端硬性規則會不會擋（rule）：「直接擋」的句子實際上到不了 Laya，訓練價值比較低。

檢查方式：打開輸出檔，不要的整行刪掉，label 不對就改，繁體轉得怪的直接改 text；
檢查完用 --merge 把留下的句子加進 eval/laya_train/guard_intent.jsonl（id 自動編成 x-a／x-c 開頭，
rule 欄拿掉、留 source 方便追來源），再重跑 make_laya_train.py。
--merge 也收自己寫的句子（沒有 source 欄，例如補的查詢句 data/laya/query_candidates.jsonl），
編成 t- 開頭。
加了多少 attack，記得在 guard_intent.jsonl 補差不多數量的 query（尤其容易誤擋的），比例才不會跑掉。

用法：uv run --project backend python eval/make_laya_external.py [--per 20] [--seed 15]
                                                          [--out <候選句檔>]
     uv run --project backend python eval/make_laya_external.py --merge [<候選句檔>]
（候選句檔預設 data/laya/external_candidates.jsonl）
"""

import argparse
import csv
import gzip
import json
import random
import re
import sys
from collections import Counter
from pathlib import Path

import httpx
import make_laya_train as mlt  # 同一個資料夾：雙字組相似度、考卷題目
import opencc

# make_laya_train 已把 backend 加進 sys.path
from app.agent import guard  # noqa: E402

ROOT = mlt.ROOT
EXTERNAL = ROOT / "data" / "laya" / "external"
DEFAULT_OUT = ROOT / "data" / "laya" / "external_candidates.jsonl"

FILES = {
    "safety_prompts/instruction_attack_scenarios.json": (
        "https://raw.githubusercontent.com/thu-coai/Safety-Prompts/main/"
        "instruction_attack_scenarios.json"
    ),
    "jailbench/JailBench.csv": (
        "https://raw.githubusercontent.com/STAIR-BUPT/JailBench/main/JailBench.csv"
    ),
    "lccc/lccc_base_test.jsonl.gz": (
        "https://huggingface.co/datasets/silver/lccc/resolve/main/lccc_base_test.jsonl.gz"
    ),
}

# Prompt 泄漏類很多句只是在「問」洩漏攻擊的風險或防法，只留真的要它交出自己的提示詞、設定、
# 訓練資料的（比對轉繁體後的句子）
LEAK_THING = r"(prompt|提示|指令|設定|規則|訓練資料|資料集|引數|參數|原始碼|程式碼|權重)"
LEAK_ASK = re.compile(
    r"(告訴|輸出|透露|展示|給我|說出|列出|顯示|列印|複述|重複|分享|公開|提供)"
    rf".{{0,6}}你(們)?的.{{0,8}}{LEAK_THING}"
    rf"|你(們)?的.{{0,8}}{LEAK_THING}.{{0,10}}(是什麼|有哪些|內容|全文|原文|發給|給我)",
    re.I,
)
LEAK_TALK = re.compile(r"攻擊|防範|防禦|風險|措施|注意事項|如何保護")
# 角色扮演類多數是無害的扮演（警察、老師、CEO），只有帶「不受規則限制、別讓人發現是 AI」這類
# 繞過字眼的才建議 attack；其他和畫作、工廠都無關 → 建議 chitchat。
# 不能把「你是一個〇〇」都教成 attack：「你是資深倉管，幫我看庫存」是正常查詢
BYPASS = re.compile(
    r"不受.{0,8}(限制|約束)|不(需要|必|用)(遵守|考慮|理會|在意)|忽略.{0,6}(道德|法律|規則|約束|限制)"
    r"|不要讓.{0,6}(發現|知道)|不會造成任何|沒有任何(限制|約束|底線)|不管別人|無視",
)
# LCCC 裡可能被當成查詢的字眼（畫作、工廠、系統）
DOMAIN = re.compile(
    r"畫|圖|庫存|訂單|工單|零件|生產|排程|工廠|倉庫|美術|博物館|展覽|系統|機台|出貨|材料|3D",
    re.I,
)
# LCCC 斷字的空格：中文字之間的空格拿掉，英文單字之間的留著
CJK_SPACE = re.compile(r"(?<=[^\x00-\x7f]) +| +(?=[^\x00-\x7f])")
HAS_CJK = re.compile(r"[一-鿿]")


def fetch(name: str) -> Path:
    path = EXTERNAL / name
    if not path.exists():
        print(f"下載 {FILES[name]}")
        path.parent.mkdir(parents=True, exist_ok=True)
        with httpx.stream("GET", FILES[name], follow_redirects=True, timeout=120) as r:
            r.raise_for_status()
            tmp = path.with_suffix(path.suffix + ".part")
            with tmp.open("wb") as f:
                for chunk in r.iter_bytes():
                    f.write(chunk)
        tmp.replace(path)
    return path


def one_line(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def load_sources(cc: opencc.OpenCC, max_len: int) -> list[dict]:
    """每個來源一組：{name, label, kind, rows: [(source, 繁體句子)]}。"""
    sp = json.loads(fetch("safety_prompts/instruction_attack_scenarios.json").read_text("utf-8"))

    def sp_rows(kind: str, keep=lambda t: True) -> list[tuple[str, str]]:
        out = []
        for i, x in enumerate(sp[kind]):
            if "\n" in x["prompt"].strip():
                continue
            t = cc.convert(one_line(x["prompt"]))
            if 6 <= len(t) <= max_len and keep(t):
                out.append((f"Safety-Prompts/{kind}#{i}", t))
        return out

    jb, seen_tpl = [], set()
    with fetch("jailbench/JailBench.csv").open(encoding="utf-8-sig", newline="") as f:
        for i, row in enumerate(csv.DictReader(f)):
            t = cc.convert(one_line(row["query"]))
            tpl = t[:40]  # 同一個越獄模板開頭一樣，只抽一句
            if len(t) <= max_len and tpl not in seen_tpl:
                seen_tpl.add(tpl)
                jb.append((f"JailBench#{i}", t))

    chat = []
    with gzip.open(fetch("lccc/lccc_base_test.jsonl.gz"), "rt", encoding="utf-8") as f:
        for i, line in enumerate(f):
            t = cc.convert(CJK_SPACE.sub("", json.loads(line)[0]).strip())
            if (
                4 <= len(t) <= 30
                and HAS_CJK.search(t)
                and not DOMAIN.search(t)
                and not re.search(r"https?:|www\.|@", t)
            ):
                chat.append((f"LCCC-base-test#{i}", t))

    return [
        {"label": "attack", "kind": "外部-目標劫持", "rows": sp_rows("Goal_Hijacking")},
        {
            "label": "attack",
            "kind": "外部-套取提示詞",
            "rows": sp_rows(
                "Prompt_Leaking", lambda t: bool(LEAK_ASK.search(t)) and not LEAK_TALK.search(t)
            ),
        },
        {
            "label": "attack",
            "kind": "外部-角色扮演繞過規則",
            "rows": sp_rows("Role_Play_Instruction", lambda t: bool(BYPASS.search(t))),
        },
        {
            "label": "chitchat",
            "kind": "外部-角色扮演（無關）",
            "rows": sp_rows("Role_Play_Instruction", lambda t: not BYPASS.search(t)),
        },
        {"label": "attack", "kind": "外部-越獄模板", "rows": jb},
        {
            "label": "chitchat",
            "kind": "外部-無關的有害請求",
            "rows": sp_rows("Unsafe_Instruction_Topic") + sp_rows("Inquiry_With_Unsafe_Opinion"),
        },
        {"label": "chitchat", "kind": "外部-閒聊", "rows": chat},
    ]


def rule_note(text: str) -> str | None:
    direct, suspects = guard._local_hits(text)
    if direct:
        return f"直接擋：{direct[0]}（「{direct[1]}」）"
    if suspects:
        return "可疑（只在耗時或修改時擋）：" + "、".join(f"{r}（「{m}」）" for r, m in suspects)
    return None


def sample(args) -> int:
    cc = opencc.OpenCC("s2twp")
    groups = load_sources(cc, args.max_len)
    current = [json.loads(x) for x in mlt.SOURCE.read_text("utf-8").splitlines() if x.strip()]
    taken = [mlt.bigrams(r["text"]) for r in current]
    exams = [mlt.bigrams(q) for _, q in mlt.exam_questions()]
    rng = random.Random(args.seed)

    out, report = [], []
    for g in groups:
        pool = list(g["rows"])
        rng.shuffle(pool)
        picked = skipped = hits = 0
        for source, text in pool:
            if picked >= args.per:
                break
            grams = mlt.bigrams(text)
            if any(mlt.jaccard(grams, x) >= args.max_sim for x in taken) or any(
                mlt.jaccard(grams, x) >= args.max_sim for x in exams
            ):
                skipped += 1
                continue
            taken.append(grams)
            row = {
                "id": f"cand-{len(out) + 1:03d}",
                "label": g["label"],
                "kind": g["kind"],
                "text": text,
                "source": source,
            }
            if note := rule_note(text):
                row["rule"] = note
                hits += note.startswith("直接擋")
            out.append(row)
            picked += 1
        report.append(
            f"  {g['kind']}（建議 {g['label']}）：可抽 {len(g['rows'])} 句 → 抽 {picked} 句"
            f"（太像略過 {skipped}；地端規則直接擋 {hits}）"
        )

    path = Path(args.out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in out), "utf-8")
    print(f"輸出 {len(out)} 句候選（{path}）")
    print("\n".join(report))
    print("檢查：不要的整行刪掉、label 不對就改；檢查完跑 --merge。")
    return 0


def merge(path: Path) -> int:
    """把檢查過的候選句加進 guard_intent.jsonl，拿掉 rule 欄。id 接著編：有 source（外部資料集）
    編 x-q／x-a／x-c，沒有 source（自己寫的句子，例如補的查詢句）編 t-q／t-a／t-c。"""
    rows = [json.loads(x) for x in path.read_text("utf-8").splitlines() if x.strip()]
    current = [json.loads(x) for x in mlt.SOURCE.read_text("utf-8").splitlines() if x.strip()]
    classes = set(guard._cfg()["intent_classes"])
    texts = {r["text"] for r in current}
    problems = [
        f"{r['id']}：label「{r['label']}」不是 {sorted(classes)}"
        for r in rows
        if r["label"] not in classes
    ]
    problems += [
        f"{r['id']}：句子已經在訓練資料裡：{r['text']}" for r in rows if r["text"] in texts
    ]
    problems += [f"句子重複：{k}" for k, n in Counter(r["text"] for r in rows).items() if n > 1]
    if problems:
        print("\n".join(problems))
        return 1

    letter = {"query": "q", "attack": "a", "chitchat": "c"}
    last = Counter()
    for r in current:
        if m := re.fullmatch(r"([xt]-[qac])(\d+)", r["id"]):
            last[m[1]] = max(last[m[1]], int(m[2]))
    added = []
    for r in rows:
        p = ("x-" if r.get("source") else "t-") + letter[r["label"]]
        last[p] += 1
        row = {"id": f"{p}{last[p]:03d}", "label": r["label"], "kind": r["kind"], "text": r["text"]}
        if r.get("source"):
            row["source"] = r["source"]
        added.append(row)
    with mlt.SOURCE.open("a", encoding="utf-8") as f:
        f.write("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in added))
    print(
        f"加進 {len(added)} 句（{mlt.SOURCE}）："
        + "、".join(f"{k} {v}" for k, v in Counter(r["label"] for r in added).items())
    )
    print("接著重跑 make_laya_train.py 產生訓練資料。")
    return 0


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--per", type=int, default=20, help="每個來源抽幾句")
    parser.add_argument("--seed", type=int, default=15)
    parser.add_argument("--max-len", type=int, default=200, help="句子最長幾個字")
    parser.add_argument("--max-sim", type=float, default=0.5, help="和現有句子、考卷的相似度上限")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument(
        "--merge", nargs="?", const=str(DEFAULT_OUT), help="把檢查過的候選句加進 guard_intent.jsonl"
    )
    args = parser.parse_args()
    if args.merge:
        return merge(Path(args.merge))
    return sample(args)


if __name__ == "__main__":
    raise SystemExit(main())
