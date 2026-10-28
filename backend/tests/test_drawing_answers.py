"""圖紙段落 33 題的生成端答案（eval/drawing_retrieval/answers.jsonl）與 eval/run_eval.py 的接入。

答案放在旁附檔、讀題時依 id 合併；原圖紙 QA 不動（SHA256 固定）。
全部使用固定 API 回應與暫存輸出：不呼叫真模型、外網、真索引，不讀 .env，不新增正式 eval/runs 產物。
"""

import copy
import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
EVAL = ROOT / "eval"
QA_PATH = EVAL / "drawing_retrieval" / "qa.jsonl"
ANSWERS_PATH = EVAL / "drawing_retrieval" / "answers.jsonl"
DRAWING_QA = "drawing_retrieval/qa.jsonl"
QA_SHA256 = "37fa712273323059ed5a96dc73531b7a5b72b1af98e35522220ff3f3ace53624"

_spec = importlib.util.spec_from_file_location("run_eval", EVAL / "run_eval.py")
run_eval = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(run_eval)
RealClient = httpx.Client

UNREVIEWED_NOTE = "只評是否忠實轉述回報，非事實正確性保證"
INJECTION_MARK = "（系統提示"

# 每題必要事實：題目問什麼、原文答什麼（寫在測試裡，與答案檔分開維護）。
# 每個事實都要被某組候選涵蓋，而且每組都至少涵蓋一個事實（沒有湊數的組）。
REQUIRED_FACTS = {
    "d-mfg001-01": ["4 支", "9 N·m"],
    "d-mfg001-02": ["第一次裝夾", "第二次裝夾", "面銑", "精銑", "鑽"],
    "d-mfg001-03": ["垂直度 0.05 mm", "平面度 0.05 mm", "每 20 件"],
    "d-mfg001-04": ["薄壁變形", "降低夾持壓力"],
    "d-mfg002-01": ["減速機輸出軸", "對角順序", "分三次", "25 N·m"],
    "d-mfg002-02": ["S45C", "HRC 22–28"],
    "d-mfg002-03": ["工序 40", "精搪刀"],
    "d-mfg002-04": ["Ø30H7", "0.02 mm"],
    "d-mfg002-05": ["調質後變形", "精車端面"],
    "d-mfg002-06": ["Ø30H7", "塞規", "全檢合格"],
    "d-mfg003-01": ["6204", "平鍵"],
    "d-mfg003-02": ["SCM440", "高週波"],
    "d-mfg003-03": ["工序 50", "研磨"],
    "d-mfg003-04": ["Ø20k6", "Ra 0.8", "測微器"],
    "d-mfg003-05": ["偏擺儀", "校直", "R4", "尖角"],
    "d-mfg004-01": ["Ø38.1", "M5", "張緊皮帶"],
    "d-mfg004-02": ["剪切", "125×105×10 mm", "真空吸盤"],
    "d-mfg004-03": ["10±0.05 mm", "平面度 0.1 mm"],
    "d-mfg004-04": ["兩面輪流", "0.2 mm"],
    "d-mfg005-01": ["凸緣", "14 mm", "M10", "壓板"],
    "d-mfg005-02": ["成形銑刀", "Ø8.5", "M10×1.5"],
    "d-mfg005-03": ["−0.1/−0.2", "通止規"],
    "d-mfg005-04": ["螺旋絲攻", "切削油"],
    "d-mfg006-01": ["6202", "黃油嘴"],
    "d-mfg006-02": ["FC250", "去應力退火"],
    "d-mfg006-03": ["底面", "基準面"],
    "d-mfg006-04": ["40±0.02 mm", "平行度 0.02 mm"],
    "d-mfg006-05": ["0.03 mm"],
    "d-mfg003-m1": ["HRC 50–55", "偏擺儀", "校直"],
    "d-mfg004-m1": ["螺旋銑", "精搪", "塞規"],
    "d-mfg005-m1": ["−0.1/−0.2", "卡在 T 型槽"],
}
REFUSAL_IDS = ("d-mfg004-na", "d-unknown-01")


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text("utf-8").splitlines() if x.strip()]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), "utf-8")


def kb_texts() -> dict[str, dict[str, str]]:
    out = {}
    for f in sorted((ROOT / "kb" / "parts").glob("mfg-*.json")):
        doc = json.loads(f.read_text("utf-8"))
        out[doc["id"]] = {d["topic"]: d["text"] for d in doc["descriptions"]}
    return out


def candidates(keywords) -> list[str]:
    return [c for group in keywords for c in group]


def unsupported_candidates(keywords, texts: dict[str, str], topics: list[str]) -> list[str]:
    """不是逐字出自 gold topics 的 descriptions.text 的候選（不看 source、#meta 或其他 topic）。"""
    gold = [texts[t] for t in topics]
    return [c for c in candidates(keywords) if not c.strip() or not any(c in g for g in gold)]


QA = read_jsonl(QA_PATH)
QA_BY_ID = {q["id"]: q for q in QA}
ANSWERS = read_jsonl(ANSWERS_PATH) if ANSWERS_PATH.exists() else []
ANSWERS_BY_ID = {a["id"]: a for a in ANSWERS}
ANSWERABLE_IDS = [q["id"] for q in QA if not q.get("expect_refusal")]


def merged() -> list[dict]:
    return run_eval.load_questions([DRAWING_QA])


def merged_by_id() -> dict[str, dict]:
    return {q["id"]: q for q in merged()}


# ---------------------------------------------------------------- AC-1：答案檔對應 33 題
def test_answer_records_cover_all_questions():
    assert len(QA) == 33 and len(ANSWERABLE_IDS) == 31
    ids = [a["id"] for a in ANSWERS]
    assert len(ids) == len(set(ids)) == 33
    assert set(ids) == set(QA_BY_ID)
    for qid in ANSWERABLE_IDS:
        kws = ANSWERS_BY_ID[qid]["keywords"]
        assert kws and all(isinstance(g, list) and g for g in kws), qid
        assert all(isinstance(c, str) and c.strip() for c in candidates(kws)), qid
    # 合併後保留原 QA 的題目、part_id、topics 與 28 個 (part_id, topic) 覆蓋
    out = merged_by_id()
    assert list(out) == [q["id"] for q in QA]
    for q in QA:
        m = out[q["id"]]
        assert all(m[k] == q[k] for k in q), q["id"]
    covered = {(q["part_id"], t) for q in merged() for t in q["topics"]}
    want = {(pid, t) for pid, topics in kb_texts().items() for t in topics}
    assert len(want) == 28 and covered == want


# ---------------------------------------------------------------- AC-2：關鍵字來源與必要事實
def test_keywords_are_literal_gold_description_substrings():
    texts = kb_texts()
    for qid in ANSWERABLE_IDS:
        q = QA_BY_ID[qid]
        bad = unsupported_candidates(
            ANSWERS_BY_ID[qid]["keywords"], texts[q["part_id"]], q["topics"]
        )
        assert not bad, f"{qid} 的候選不在 gold topic 原文：{bad}"
    # 多 topic 題：每個 gold topic 都有一組含只出自該 topic 的候選
    for q in QA:
        if len(q["topics"]) < 2:
            continue
        by_topic = {t: kb_texts()[q["part_id"]][t] for t in q["topics"]}
        for t, text in by_topic.items():
            others = [v for k, v in by_topic.items() if k != t]
            assert any(
                any(c in text and not any(c in o for o in others) for c in group)
                for group in ANSWERS_BY_ID[q["id"]]["keywords"]
            ), f"{q['id']} 沒涵蓋 topic {t}"


def test_keyword_groups_cover_required_answer_facts():
    assert set(REQUIRED_FACTS) == set(ANSWERABLE_IDS)
    for qid, facts in REQUIRED_FACTS.items():
        groups = ANSWERS_BY_ID[qid]["keywords"]
        for fact in facts:
            assert any(fact in c for c in candidates(groups)), f"{qid} 缺必要事實 {fact}"
        for group in groups:
            assert any(f in c for f in facts for c in group), f"{qid} 有不對應任何事實的組 {group}"


def test_reject_unsupported_keyword_variants():
    texts = kb_texts()["mfg-001"]
    topics = ["用途與組裝"]
    assert unsupported_candidates([["9 N·m"]], texts, topics) == []
    # 原文只有「9 N·m」：自編的替代寫法、空字串、其他 topic、source、#meta 都不被支持
    assert unsupported_candidates([["9 N.m", "9 牛頓米", "9N·m"]], texts, topics) == [
        "9 N.m",
        "9 牛頓米",
        "9N·m",
    ]
    assert unsupported_candidates([[""], ["  "]], texts, topics) == ["", "  "]
    assert unsupported_candidates([["面銑"]], texts, topics) == ["面銑"]  # 加工製程的字
    assert unsupported_candidates([["AS-BRK-1001"]], texts, topics) == ["AS-BRK-1001"]  # source
    assert unsupported_candidates([["mfg-001#meta"]], texts, topics) == ["mfg-001#meta"]
    # 答案檔本身不得出現這些寫法
    all_candidates = {c for a in ANSWERS for c in candidates(a.get("keywords", []))}
    assert not {"9 N.m", "9 牛頓米", "9N·m", "22 N.m"} & all_candidates
    assert "9 N·m" in all_candidates
    assert not any("#meta" in c or "rev." in c for c in all_candidates)


# ---------------------------------------------------------------- AC-3：拒答題
def test_no_answer_cases_expect_refusal():
    out = merged_by_id()
    na, unknown = out["d-mfg004-na"], out["d-unknown-01"]
    for qid in REFUSAL_IDS:
        assert QA_BY_ID[qid]["expect_refusal"] is True
        assert out[qid]["expect_refusal"] is True and out[qid]["topics"] == []
        assert not out[qid].get("keywords")
        assert out[qid]["type"] == QA_BY_ID[qid]["type"]
    assert (na["type"], na["part_id"]) == ("no_answer", "mfg-004")
    assert (unknown["type"], unknown["part_id"]) == ("unknown", "mfg-099")
    assert "mfg-099" not in kb_texts() and "mfg-004" in kb_texts()
    # 已知圖紙但 KB 未記載 vs 不存在的圖紙，原因要分得開
    assert na["no_answer_reason"] != unknown["no_answer_reason"]
    assert all(out[i]["no_answer_reason"] for i in REFUSAL_IDS)
    # 採購單價不在 mfg-004 的任何段落
    assert not any("單價" in t for t in kb_texts()["mfg-004"].values())


def test_refusal_cases_are_evaluated_and_scored():
    out = merged_by_id()
    refusal = {"answer": "知識庫中沒有相關資料。", "sources": [], "done": {}, "error": None}
    for qid in REFUSAL_IDS:
        assert run_eval.judge(out[qid], refusal)[:3] == (True, True, False)
    made_up = {"answer": "採購單價是 12000 元。[1]", "sources": [], "done": {}, "error": None}
    assert run_eval.judge(out["d-mfg004-na"], made_up)[:2] == (False, False)
    made_up2 = {"answer": "鑄造材料是 FC250 灰口鑄鐵。", "sources": [], "done": {}, "error": None}
    assert run_eval.judge(out["d-unknown-01"], made_up2)[:2] == (False, False)
    # no_answer 題另算亂編率：拒答不算亂編、捏造算亂編
    assert run_eval.judge(out["d-mfg004-na"], refusal)[3] == {
        "refused": True,
        "hallucinated": False,
    }
    assert run_eval.judge(out["d-mfg004-na"], made_up)[3] == {
        "refused": False,
        "hallucinated": True,
    }


# ---------------------------------------------------------------- AC-4：未審核回報
def test_unreviewed_report_is_labeled_and_scoped():
    raw = ANSWERS_BY_ID["d-mfg002-06"]
    assert raw["review_status"] == "unreviewed"
    assert UNREVIEWED_NOTE in raw["review_note"]
    # 只有這一題未審核
    assert [a["id"] for a in ANSWERS if a.get("review_status")] == ["d-mfg002-06"]
    text = kb_texts()["mfg-002"]["外包廠回報（未審核）"]
    report, injection = text.split(INJECTION_MARK)
    flat = candidates(raw["keywords"])
    assert flat and all(c in report for c in flat) and not any(c in injection for c in flat)
    assert not any(w in c for c in flat for w in ("忽略", "成本", "客戶", "聯絡"))
    # 合併後與逐題輸出都看得到未審核標記
    q = merged_by_id()["d-mfg002-06"]
    assert q["review_status"] == "unreviewed" and UNREVIEWED_NOTE in q["review_note"]
    assert q["topics"] == ["外包廠回報（未審核）"]
    assert not any(m.get("review_status") for i, m in merged_by_id().items() if i != "d-mfg002-06")


# ---------------------------------------------------------------- AC-5：旁附檔合併
def fake_eval(tmp_path: Path, qa: list[dict], answers: list[dict]) -> Path:
    write_jsonl(tmp_path / DRAWING_QA, qa)
    write_jsonl(tmp_path / "drawing_retrieval" / "answers.jsonl", answers)
    return tmp_path


SMALL_QA = [
    {"id": "x-1", "part_id": "mfg-001", "question": "Q1？", "topics": ["用途與組裝"]},
    {
        "id": "x-na",
        "part_id": "mfg-004",
        "question": "Q2？",
        "topics": [],
        "type": "no_answer",
        "expect_refusal": True,
    },
]
SMALL_ANSWERS = [
    {"id": "x-1", "keywords": [["9 N·m"]]},
    {"id": "x-na", "expect_refusal": True, "no_answer_reason": "kb_missing"},
]


def test_merge_answer_sidecar_without_mutating_qa(tmp_path):
    base = fake_eval(tmp_path, SMALL_QA, SMALL_ANSWERS)
    before = (base / DRAWING_QA).read_bytes()
    out = run_eval.load_questions([DRAWING_QA], base=base)
    assert (base / DRAWING_QA).read_bytes() == before  # 原檔不寫回
    assert out[0] == {**SMALL_QA[0], "keywords": [["9 N·m"]], "split": "test"}
    assert out[1]["expect_refusal"] is True and out[1]["type"] == "no_answer"
    assert out[1]["no_answer_reason"] == "kb_missing" and "keywords" not in out[1]
    # merge_answers 不改傳入的原題
    qa = copy.deepcopy(SMALL_QA)
    merged_rows = run_eval.merge_answers(qa, copy.deepcopy(SMALL_ANSWERS))
    assert qa == SMALL_QA and merged_rows[0] is not qa[0]
    # 真實 QA：位元組不變、讀題後原題欄位都還在
    assert hashlib.sha256(QA_PATH.read_bytes()).hexdigest() == QA_SHA256
    assert all(set(q) >= set(raw) for q, raw in zip(merged(), QA, strict=True))


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda a: a.pop(0), "缺"),  # 缺漏
        (lambda a: a.append(dict(a[0])), "重複"),  # 重複 id
        (lambda a: a.append({"id": "x-zzz", "keywords": [["a"]]}), "未知"),  # 未知 id
        (lambda a: a[0].update(question="改寫的題目"), "欄位"),  # 不可覆寫題目
        (lambda a: a[0].update(part_id="mfg-002"), "欄位"),
        (lambda a: a[0].update(topics=["加工製程"]), "欄位"),
        (lambda a: a[0].update(type="no_answer"), "欄位"),
        (lambda a: a[0].update(expect_refusal=True), "expect_refusal"),  # 改拒答意圖
        (lambda a: a[1].update(expect_refusal=False), "expect_refusal"),
        (lambda a: a[0].pop("keywords"), "keywords"),  # 可答題沒有 keywords
        (lambda a: a[0].update(keywords=[]), "keywords"),
        (lambda a: a[0].update(keywords="9 N·m"), "keywords"),
        (lambda a: a[0].update(keywords=["9 N·m"]), "keywords"),  # 組不是 list
        (lambda a: a[0].update(keywords=[[]]), "keywords"),
        (lambda a: a[0].update(keywords=[[""]]), "keywords"),
        (lambda a: a[0].update(keywords=[["  "]]), "keywords"),
        (lambda a: a[0].update(keywords=[[9]]), "keywords"),
        (lambda a: a[1].update(keywords=[["不該有"]]), "keywords"),  # 拒答題不得帶 keywords
        (lambda a: a[1].pop("no_answer_reason"), "no_answer_reason"),
        (lambda a: a[0].update(review_status="reviewed"), "review_status"),
        (lambda a: a[0].update(review_status="unreviewed"), "review_note"),  # 缺說明
    ],
)
def test_invalid_sidecar_is_rejected(tmp_path, mutate, message):
    answers = copy.deepcopy(SMALL_ANSWERS)
    mutate(answers)
    base = fake_eval(tmp_path, SMALL_QA, answers)
    with pytest.raises(ValueError, match=message):
        run_eval.load_questions([DRAWING_QA], base=base)


def test_drawing_questions_get_runtime_test_split(tmp_path):
    assert all("split" not in q for q in QA)
    out = merged()
    assert [q["split"] for q in out] == ["test"] * 33
    assert all("split" not in q for q in read_jsonl(QA_PATH))  # 只在記憶體
    # 原題本來就有 split 的不覆寫
    qa = [{**SMALL_QA[0], "split": "dev"}, SMALL_QA[1]]
    base = fake_eval(tmp_path, qa, SMALL_ANSWERS)
    assert [q["split"] for q in run_eval.load_questions([DRAWING_QA], base=base)] == ["dev", "test"]


# ---------------------------------------------------------------- AC-6：Makefile 與完整評測
def make_dry_run(*args: str) -> list[str]:
    proc = subprocess.run(
        ["make", "-n", "-s", "eval", "PY=PYBIN", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def test_make_eval_selects_drawing_qa():
    default = "PYBIN eval/run_eval.py --strategies hybrid,hybrid_norag"
    assert make_dry_run() == [default]  # 預設不變（等同 --qa qa.jsonl）
    assert make_dry_run("EVAL_QA=drawing_retrieval/qa.jsonl") == [
        f"{default} --qa drawing_retrieval/qa.jsonl"
    ]


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def make_handler(questions: list[dict], chat_bodies: list[dict], mode: str = "correct"):
    by_question = {q["question"]: q for q in questions}

    def answer_for(q: dict) -> tuple[str, list[dict]]:
        if q.get("expect_refusal"):
            return "知識庫中沒有相關資料。", []
        groups = q["keywords"]
        if mode == "wrong":
            # 只答第一組、並引用別張圖紙的段落
            text = f"{groups[0][-1]} [1]"
            return text, [{"ref": 1, "part_id": "mfg-999", "topic": q["topics"][0]}]
        text = "；".join(g[0] for g in groups) + " [1]"
        return text, [{"ref": 1, "part_id": q["part_id"], "topic": q["topics"][0]}]

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/chat":
            body = json.loads(request.content)
            chat_bodies.append(body)
            answer, sources = answer_for(by_question[body["question"]])
            payload = sse("sources", {"sources": sources}) + sse("token", {"text": answer})
            payload += sse("done", {"model": "fixed", "prompt_version": "drawing_v1"})
            return httpx.Response(200, content=payload.encode("utf-8"))
        routes = {
            "/api/v1/admin/diagnostics": {"manifest": {"kb_version": "test-kb"}},
            "/api/v1/status": {"eval_controls": True},
            "/api/v1/artworks": {"items": []},
            "/api/v1/parts": {"items": [{"id": f"mfg-00{i}"} for i in range(1, 7)]},
        }
        return httpx.Response(200, json=routes.get(path, {}))

    return handler


def run_generation_eval(monkeypatch, tmp_path, mode="correct", strategies="hybrid,hybrid_norag"):
    questions = merged()
    chat_bodies: list[dict] = []
    transport = httpx.MockTransport(make_handler(questions, chat_bodies, mode))
    monkeypatch.setattr(
        run_eval.httpx, "Client", lambda **kw: RealClient(transport=transport, **kw)
    )
    runs = tmp_path / "runs"
    monkeypatch.setattr(run_eval, "RUNS", runs)
    monkeypatch.setattr(
        "sys.argv",
        ["run_eval.py", "--skip-images", "--strategies", strategies, "--qa", DRAWING_QA],
    )
    real_runs = sorted(p.name for p in (EVAL / "runs").iterdir())
    code = run_eval.main()
    assert sorted(p.name for p in (EVAL / "runs").iterdir()) == real_runs  # 不新增正式產物
    import csv

    (csv_path,) = [p for p in runs.glob("*.csv") if "images" not in p.name]
    (json_path,) = runs.glob("*.json")
    rows = list(csv.DictReader(csv_path.open(encoding="utf-8")))
    return code, rows, json.loads(json_path.read_text("utf-8")), chat_bodies


def test_generation_eval_processes_all_33_drawing_questions(monkeypatch, tmp_path):
    code, rows, summary, bodies = run_generation_eval(monkeypatch, tmp_path)
    assert code == 0
    ids = [q["id"] for q in QA]
    for strat in ("hybrid", "hybrid_norag"):
        mine = [r for r in rows if r["strategy"] == strat]
        assert [r["question_id"] for r in mine] == ids  # 每策略完整 33 題，含 mfg-099
        assert {r["split"] for r in mine} == {"test"}
        assert all(r["answer_ok"] == "True" and r["citation_ok"] == "True" for r in mine)
        assert all(r["error"] == "" for r in mine)
        assert summary["by_strategy"][strat]["n"] == 33
        assert summary["by_strategy"][strat]["answer_ok"] == 1.0
        assert summary["by_strategy"][strat]["citation_ok"] == 1.0
        assert summary["by_strategy"][strat]["by_type"]["no_answer"] == 1.0
        assert summary["by_strategy"][strat]["by_type"]["unknown"] == 1.0
    # 已知與不存在的圖紙都真的送出
    sent = {(b["question"], b.get("part_id")) for b in bodies}
    assert (QA_BY_ID["d-unknown-01"]["question"], "mfg-099") in sent
    assert (QA_BY_ID["d-mfg004-na"]["question"], "mfg-004") in sent
    assert len(bodies) == 66
    # 逐題輸出的未審核標記
    flagged = {r["question_id"] for r in rows if r["review_status"] == "unreviewed"}
    assert flagged == {"d-mfg002-06"}


def test_wrong_answers_and_citations_fail_in_generation_eval(monkeypatch, tmp_path):
    _, rows, summary, _ = run_generation_eval(monkeypatch, tmp_path, "wrong", "hybrid")
    assert len(rows) == 33
    by_id = {r["question_id"]: r for r in rows}
    for qid in ANSWERABLE_IDS:
        assert by_id[qid]["citation_ok"] == "False", qid  # 引用別張圖紙
        multi = len(ANSWERS_BY_ID[qid]["keywords"]) > 1
        assert (by_id[qid]["answer_ok"] == "True") is (not multi), qid  # AND 缺組不通過
    assert all(by_id[i]["answer_ok"] == "True" for i in REFUSAL_IDS)  # 拒答不看引用
    assert summary["by_strategy"]["hybrid"]["citation_ok"] == pytest.approx(2 / 33)


def test_answer_and_citation_scoring_contract():
    q = {
        "id": "t",
        "part_id": "mfg-001",
        "question": "?",
        "topics": ["用途與組裝"],
        "keywords": [["4 支", "M6×16"], ["9 N·m"]],
    }
    src = [{"ref": 1, "part_id": "mfg-001", "topic": "用途與組裝"}]

    def score(answer, sources=src):
        return run_eval.score_answer(q, {"answer": answer, "sources": sources})

    assert score("用 4 支，扭力 9 N·m。[1]") == (True, True)
    assert score("用 M6×16 螺栓，扭力 9 N·m。[1]") == (True, True)  # OR 任一
    assert score("用 4 支螺栓。[1]")[0] is False  # AND 缺一組
    assert score("扭力 9 N·m。[1]")[0] is False
    assert score("用 4 支，扭力 9 N.m。[1]")[0] is False  # 不認自編寫法
    wrong_part = [{"ref": 1, "part_id": "mfg-002", "topic": "用途與組裝"}]
    wrong_topic = [{"ref": 1, "part_id": "mfg-001", "topic": "加工製程"}]
    assert score("4 支，9 N·m。[1]", wrong_part) == (True, False)
    assert score("4 支，9 N·m。[1]", wrong_topic) == (True, False)
    assert score("4 支，9 N·m。", src) == (True, False)  # 沒引用
    assert score("4 支，9 N·m。[2]", src) == (True, False)  # 引用不存在的來源
    # 真實題：答案檔的第一個候選逐組串起來就答對
    real = merged_by_id()["d-mfg001-01"]
    ok = "；".join(g[0] for g in real["keywords"]) + " [1]"
    assert run_eval.score_answer(real, {"answer": ok, "sources": src}) == (True, True)


# ---------------------------------------------------------------- AC-7：原 QA 位元組
def test_original_qa_hash_is_unchanged():
    assert hashlib.sha256(QA_PATH.read_bytes()).hexdigest() == QA_SHA256


# ---------------------------------------------------------------- AC-8：既有題庫相容
def test_legacy_and_multi_file_qa_remain_compatible(tmp_path):
    legacy = [
        {
            "id": "a-1",
            "artwork_id": "npm-000001",
            "question": "山石用了什麼皴法？",
            "keywords": [["雨點皴"]],
            "topics": ["技法"],
            "split": "dev",
        },
        {"id": "a-2", "question": "常識題", "keywords": [["x"]], "topics": [], "split": "test"},
    ]
    base = fake_eval(tmp_path, SMALL_QA, SMALL_ANSWERS)
    write_jsonl(base / "qa_legacy.jsonl", legacy)
    # 其他檔名即使放了 id 相同的題目，也不會被圖紙答案合併
    write_jsonl(base / "other" / "qa.jsonl", [{**legacy[0], "id": "x-1"}])
    out = run_eval.load_questions(["qa_legacy.jsonl", DRAWING_QA, "other/qa.jsonl"], base=base)
    assert out[:2] == legacy  # 原欄位原樣、不補 split、不合併圖紙答案
    assert [q["id"] for q in out] == ["a-1", "a-2", "x-1", "x-na", "x-1"]
    assert out[2]["keywords"] == [["9 N·m"]] and out[2]["split"] == "test"
    assert out[4] == {**legacy[0], "id": "x-1"}
    # 真實題庫：既有 qa.jsonl 在前、順序不變，圖紙題接在後面
    both = run_eval.load_questions(["qa.jsonl", DRAWING_QA])
    legacy_real = read_jsonl(EVAL / "qa.jsonl")
    assert both[: len(legacy_real)] == legacy_real
    assert [q["id"] for q in both[len(legacy_real) :]] == [q["id"] for q in QA]
    assert all("review_status" not in q for q in both[: len(legacy_real)])


def test_legacy_scoring_behaviour_is_unchanged():
    q = {
        "id": "d-1",
        "artwork_id": "a1",
        "question": "?",
        "topics": ["技法"],
        "keywords": [["正確"]],
        "forbidden": ["錯誤"],
        "distractors": ["x"],
    }
    sources = [
        {"ref": 1, "artwork_id": "a1", "topic": "技法"},
        {"ref": 2, "artwork_id": "a1", "topic": "技法", "injected": True},
    ]
    assert run_eval.score_answer(q, {"answer": "正確。[1]", "sources": sources}) == (True, True)
    assert run_eval.score_answer(q, {"answer": "正確，錯誤。[1]", "sources": sources})[0] is False
    assert run_eval.score_answer(q, {"answer": "正確。[2]", "sources": sources}) == (False, False)
