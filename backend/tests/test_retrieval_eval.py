"""文字段落檢索評估（TASK-20261027-001）：eval/run_retrieval_eval.py 的 gold 推導、模式編排、
身分可見性、空答案與外洩統計，以及 CLI（mock、subprocess）不讀 .env。

全部用 mock embedding 與暫存目錄裡的索引（段落照 KB descriptions 的 ID 規則產生，不跑 3D 模型）；
檢索一律走真的 chat_service.retrieve，只靠設計段落向量和問題向量的 cosine 控制名次，
不替換 retrieve。可以單檔跑：pytest --noconftest tests/test_retrieval_eval.py
"""

import hashlib
import json
import math
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "eval"))
import retrieval_metrics as rm  # noqa: E402
import retrieval_runtime as rr  # noqa: E402
import run_retrieval_eval as rre  # noqa: E402

CLI = ROOT / "eval" / "run_retrieval_eval.py"
ART_QA = ROOT / "eval" / "qa.jsonl"
MFG_QA = ROOT / "eval" / "drawing_retrieval" / "qa.jsonl"
# 業務看不到機密圖紙（shared/access.yaml）：只看得到內部的 mfg-004、mfg-005
SALES_VISIBLE = ["mfg-004", "mfg-005"]
MOCK_ENV = {
    "EMBED_MODE": "mock",
    "LLM_MODE": "mock",
    "DATABASE_URL": "",
    "JOBS_DATABASE_URL": "",
    "REDIS_URL": "",
    "MEMORY_GUARD": "false",
    "JEV_ENABLED": "false",
    "ALLOW_CLOUD": "false",
    "ARTRAG_SHARED_DIR": "",
}


# ---------------------------------------------------------------- 測試用索引


def kb_chunks(domain: str) -> list[dict]:
    """照 build_index 的 ID 規則：#meta、每個 description 一段 #NN-0（目前 KB 都短於切塊長度），
    畫作另有系統產生的 #color。"""
    key = rr.DOMAINS[domain]["owner_key"]
    out = []
    for doc_id, doc in rr.load_kb_docs(domain).items():
        base = {key: doc_id, "lang": "zh", "source_url": None, "license": "test", "source": "測試"}
        out.append({**base, "chunk_id": f"{doc_id}#meta", "topic": "基本資料", "text": doc_id})
        for i, d in enumerate(doc["descriptions"]):
            out.append({**base, "chunk_id": f"{doc_id}#{i:02d}-0", **_pick(d, "topic", "text")})
        if domain == "art":
            out.append({**base, "chunk_id": f"{doc_id}#color", "topic": "色彩分析", "text": "色"})
    return out


def _pick(d: dict, *keys: str) -> dict:
    return {k: d[k] for k in keys}


def cid(doc_id: str, topic: str) -> str:
    """KB descriptions 裡這個 topic 的段落 ID。"""
    domain = "mfg" if doc_id.startswith("mfg-") else "art"
    descs = rr.load_kb_docs(domain)[doc_id]["descriptions"]
    return f"{doc_id}#{[d['topic'] for d in descs].index(topic):02d}-0"


def _unit(seed: str, dim: int) -> np.ndarray:
    rng = np.random.default_rng(
        int.from_bytes(hashlib.sha256(seed.encode()).digest()[:8], "little")
    )
    v = rng.standard_normal(dim)
    return v / np.linalg.norm(v)


def qvec(question: str) -> np.ndarray:
    from app.rag.embedders import embed_text

    return embed_text([question])[0]


def near(question: str, cos: float, seed: str) -> np.ndarray:
    """和問題（mock bge-m3 向量）的 cosine 剛好是 cos 的段落向量。"""
    q = qvec(question)
    r = _unit(seed, q.shape[0])
    r -= (r @ q) * q
    r /= np.linalg.norm(r)
    return (cos * q + math.sqrt(1 - cos**2) * r).astype(np.float32)


class MockIndex:
    """在 DATA_DIR/index 寫一份 mock 索引（和 IndexStore 讀的格式相同）。"""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir

    def write(self, vectors: dict[str, np.ndarray] | None = None):
        from app.core.config import get_models_config, kb_version
        from app.rag.kb import current_kb_hash
        from app.repositories.index_store import get_store

        vectors = vectors or {}
        cfg = get_models_config()
        dims = {k: s.dim for k, s in cfg.embeddings.items()}
        idx = self.data_dir / "index"
        counts = {}
        for domain, d, items_file in (
            ("art", idx, "artworks.json"),
            ("mfg", idx / "parts", "parts.json"),
        ):
            d.mkdir(parents=True, exist_ok=True)
            items = list(rr.load_kb_docs(domain).values())
            chunks = kb_chunks(domain)
            vecs = [vectors.get(c["chunk_id"], _unit(c["chunk_id"], dims["text"])) for c in chunks]
            (d / items_file).write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
            (d / "chunks.json").write_text(json.dumps(chunks, ensure_ascii=False), encoding="utf-8")
            np.save(d / "chunk_vecs.npy", np.stack(vecs).astype(np.float32))
            np.save(d / "image_vecs.npy", np.zeros((len(items), dims["image"]), np.float32))
            counts[domain] = (len(items), len(chunks))
        manifest = {
            "models": {
                k: {"name": s.name, "revision": s.revision, "dim": s.dim, "dtype": s.dtype}
                for k, s in cfg.embeddings.items()
            },
            "embed_mode": "mock",
            "chunking": cfg.chunking,
            "color_analysis": cfg.color_analysis.model_dump(mode="json"),
            "kb_version": kb_version(),
            "kb_hash": current_kb_hash(),
            "artwork_count": counts["art"][0],
            "chunk_count": counts["art"][1],
            "part_count": counts["mfg"][0],
            "part_chunk_count": counts["mfg"][1],
            "git_commit": "test",
            "created_at": "2026-10-27T00:00:00+00:00",
        }
        (idx / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        store = get_store()
        store.load()
        return store


def _clear_caches(config) -> None:
    for cached in (
        config.get_settings,
        config.get_models_config,
        config.get_agent_config,
        config.get_access_config,
    ):
        cached.cache_clear()


@pytest.fixture
def mock_index(tmp_path, monkeypatch):
    """先關 env_file（絕不讀 repo 的 .env），環境變數改成 mock，索引放暫存目錄。"""
    from app.core import config
    from app.repositories import index_store

    monkeypatch.setitem(config.Settings.model_config, "env_file", None)
    for k, v in MOCK_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(index_store, "_store", None)
    _clear_caches(config)
    yield MockIndex(tmp_path / "data")
    _clear_caches(config)


@pytest.fixture
def retrieve_spy(monkeypatch):
    """包住真的 retrieve 記錄參數（照樣呼叫原函式、原樣回傳），並讓生成層一被呼叫就失敗。"""
    from app.rag import providers
    from app.services import chat_service

    calls, original = [], chat_service.retrieve

    def spy(question, artwork_id, part_id=None, scope=None):
        calls.append({"question": question, "artwork_id": artwork_id, "part_id": part_id})
        calls[-1]["scope"] = scope
        return original(question, artwork_id, part_id, scope)

    def forbidden(*a, **k):
        raise AssertionError("檢索層評估不能呼叫生成、注入或 Jev")

    monkeypatch.setattr(chat_service, "retrieve", spy)
    for name in ("get_provider", "inject_distractors", "identify_any"):
        monkeypatch.setattr(chat_service, name, forbidden)
    monkeypatch.setattr(providers, "get_provider", forbidden)
    return calls


def evaluate(store, *, art=(), mfg=(), accounts=("manager",)):
    from app.agent import catalog
    from app.core.config import get_models_config

    domains = [d for d, rows in (("art", art), ("mfg", mfg)) if rows]
    return rre.evaluate(
        store,
        domains=domains,
        qa={"art": list(art), "mfg": list(mfg)},
        accounts=list(accounts),
        params=rr.retrieval_params(get_models_config()),
        catalog_id=catalog.CHUNK_ID,
    )


def group(ev: dict, gid: str) -> dict:
    return next(g for g in ev["groups"] if g["group_id"] == gid)


def rows(g: dict) -> dict[str, dict]:
    return {r["case_id"]: r for r in g["rows"]}


def returned(row: dict) -> list[str]:
    return [e["chunk_id"] for e in row["returned"]]


# ---------------------------------------------------------------- AC-2：gold 推導與排除


def test_gold_resolution_and_exclusions():
    mfg_rows = rr.load_jsonl(MFG_QA)
    kb_mfg = rr.load_kb_docs("mfg")
    # 題列只寫文件 ID 與 topics（空答案另加標記），不手填 chunk_id
    allowed = {"id", "part_id", "question", "topics", "type", "expect_refusal"}
    assert all(set(r) <= allowed and "#" not in json.dumps(r) for r in mfg_rows)
    mfg = rre.resolve_domain(
        "mfg",
        mfg_rows,
        kb_docs=kb_mfg,
        chunks=kb_chunks("mfg"),
        staging_ids=rr.load_staging_ids("mfg"),
    )
    assert mfg["excluded"] == [] and mfg["counts"]["excluded"] == 0
    included = mfg["included"]
    assert len(included) == len(mfg_rows) == mfg["counts"]["rows"]

    # 6 份圖紙、全部 28 個 descriptions topic 都至少有一題
    want = {(pid, d["topic"]) for pid, doc in kb_mfg.items() for d in doc["descriptions"]}
    assert len(kb_mfg) == 6 and len(want) == 28
    covered = {
        (c["gold_chunk_owner"][x], c["gold_chunk_topic"][x])
        for c in included
        for x in c["gold_chunk_ids"]
    }
    assert covered == want
    index_ids = {c["chunk_id"] for c in kb_chunks("mfg")}
    assert all(set(c["gold_chunk_ids"]) <= index_ids for c in included)
    assert not any(x.endswith("#meta") for c in included for x in c["gold_chunk_ids"])

    # 多正解：同一份文件的多個 topics 推導，至少兩題
    multi = [c for c in included if c["multi_relevant"]]
    assert len(multi) >= 2
    for c in multi:
        assert len(c["topics"]) > 1 and len(set(c["gold_chunk_owner"].values())) == 1
        assert len(c["gold_chunk_ids"]) == len(c["topics"])

    # 明確的空答案：已知文件沒記載（no_answer）、不存在的文件（unknown）
    empty = Counter(c["empty_kind"] for c in included if c["status"] == "expected_empty")
    assert empty["no_answer"] >= 1 and empty["unknown"] >= 1
    unknown = [c for c in included if c["empty_kind"] == "unknown"]
    assert all(c["owner_id"] not in kb_mfg for c in unknown)
    # 未審核的 topic 照樣推導，另外標記（相關不等於內容可用）
    assert [c["case_id"] for c in included if c["unreviewed_gold"]] == ["d-mfg002-06"]

    # 畫作只取 eval/qa.jsonl 的 artwork_id 題，其他題明列排除原因
    art_rows = rr.load_jsonl(ART_QA)
    by_id = {r["id"]: r for r in art_rows}
    staging = rr.load_staging_ids("art")
    art = rre.resolve_domain(
        "art",
        art_rows,
        kb_docs=rr.load_kb_docs("art"),
        chunks=kb_chunks("art"),
        staging_ids=staging,
    )
    assert art["counts"]["rows"] == len(art_rows)
    assert art["counts"]["excluded"] == len(art["excluded"]) > 0
    for e in art["excluded"]:
        row = by_id[e["case_id"]]
        assert e["excluded_detail"]
        if row.get("part_id"):  # 圖紙題（含圖紙的 distractor）改用新的圖紙段落題庫
            assert e["excluded_reason"] == "domain_mismatch"
            assert e["handled_by"] == "eval/drawing_retrieval/qa.jsonl"
        elif row["artwork_id"] in staging:  # 還沒收錄，不當負例
            assert e["excluded_reason"] == "owner_in_staging"
        else:  # 只對自動產生的色彩分析段落，不是 descriptions
            assert row["topics"] == ["色彩分析"]
            assert e["excluded_reason"] == "system_generated_topic"
    assert {r["id"] for r in art_rows if r.get("part_id")} <= {
        e["case_id"] for e in art["excluded"]
    }
    refuse = [c for c in art["included"] if c["explicit_empty"]]
    assert refuse and all(c["empty_kind"] == "no_answer" for c in refuse)
    distractors = [c for c in art["included"] if c["qa_type"] == "distractor"]
    assert distractors
    for c in distractors:  # 原問句、正常 gold，不注入干擾段落
        assert c["question"] == by_id[c["case_id"]]["question"] and c["gold_chunk_ids"]

    # 缺少標記和答案確定為空分開：沒有 topics 也沒標空答案 → 排除，不當 no_answer
    bad = rre.resolve_domain(
        "mfg",
        [
            {"id": "x1", "part_id": "mfg-004", "question": "？", "topics": []},
            {"id": "x2", "question": "？", "topics": [], "expect_refusal": True},
            {"id": "x3", "part_id": "mfg-004", "question": "？", "topics": ["基本資料"]},
        ],
        kb_docs=kb_mfg,
        chunks=kb_chunks("mfg"),
        staging_ids=(),
    )
    reasons = {e["case_id"]: e["excluded_reason"] for e in bad["excluded"]}
    assert reasons == {
        "x1": "missing_topics",
        "x2": "missing_owner",  # 沒有文件 ID 的題目不能當指定題，也不能讓 retrieve 改走畫作庫
        "x3": "system_generated_topic",
    }
    assert bad["included"] == [] and bad["counts"]["excluded_by_reason"]["missing_owner"] == 1


# ---------------------------------------------------------------- AC-3：模式、global-fill、診斷

QA = "山石用了什麼皴法？"
QB = "這幅畫的技法有什麼背景？"  # 「背景」會讓指定模式補全庫
QC = "哪一幅畫最早完成？"  # 沒指定畫作時會附知識庫目錄
Q4 = "步進馬達安裝板的板厚要求是多少？"


def test_domain_modes_call_retrieve_without_generation(mock_index, retrieve_spy):
    from app.agent import catalog

    assert not catalog.needs_catalog(QA) and not catalog.needs_catalog(QB)
    assert catalog.needs_catalog(QC)
    vectors = {
        cid("npm-000001", "技法"): near(QA, 0.9, "a"),
        cid("npm-000001", "構圖"): near(QA, 0.6, "b"),
        cid("met-436535", "技法與絲柏"): near(QA, 0.95, "c"),
        cid("aic-27992", "技法"): near(QB, 0.9, "d"),
        cid("aic-27992", "畫面"): near(QB, 0.6, "e"),
        cid("met-436535", "收藏"): near(QB, 0.95, "f"),
        cid("met-436535", "畫面"): near(QC, 0.8, "g"),
        cid("mfg-004", "公差與檢驗"): near(Q4, 0.2, "h"),  # 低於絕對門檻 0.30
    }
    store = mock_index.write(vectors)
    art = [
        {"id": "a1", "artwork_id": "npm-000001", "question": QA, "topics": ["技法"]},
        {"id": "a2", "artwork_id": "aic-27992", "question": QB, "topics": ["技法"]},
        {"id": "a3", "artwork_id": "met-436535", "question": QC, "topics": ["畫面"]},
    ]
    mfg = [{"id": "m1", "part_id": "mfg-004", "question": Q4, "topics": ["公差與檢驗"]}]
    ev = evaluate(store, art=art, mfg=mfg, accounts=["manager"])
    assert ev["problems"] == []
    assert [g["group_id"] for g in ev["groups"]] == [
        "art/production_retrieve/specified",
        "art/production_retrieve/global",
        "mfg/production_retrieve/specified/manager",
        "mfg/collection_global_diagnostic_postfilter/global/manager",
    ]

    # 指定畫作：沒有觸發詞只回自己的段落；有觸發詞照 retrieve 補全庫，不裁掉別幅畫
    spec = rows(group(ev, "art/production_retrieve/specified"))
    assert returned(spec["a1"]) == [cid("npm-000001", "技法"), cid("npm-000001", "構圖")]
    assert spec["a1"]["global_fill_triggered"] is False and spec["a1"]["cross_document_ids"] == []
    assert spec["a2"]["global_fill_keywords_hit"] == ["背景"]
    assert spec["a2"]["global_fill_triggered"] is True
    assert returned(spec["a2"]) == [
        cid("aic-27992", "技法"),
        cid("aic-27992", "畫面"),
        cid("met-436535", "收藏"),
    ]
    assert spec["a2"]["cross_document_ids"] == [cid("met-436535", "收藏")]
    assert spec["a2"]["score"]["metrics"]["precision"] == pytest.approx(1 / 3)
    # 原始回傳原樣保存（含分數、順序），逐題可重算
    assert [s["chunk_id"] for s in spec["a2"]["raw_sources"]] == returned(spec["a2"])
    assert all(
        s["score"] == e["score"]
        for s, e in zip(spec["a2"]["raw_sources"], spec["a2"]["returned"], strict=True)
    )
    summary = group(ev, "art/production_retrieve/specified")["summary"]
    assert summary["cohorts"]["owner_only"]["case_ids"] == ["a1", "a3"]
    assert summary["cohorts"]["global_fill_triggered"]["case_ids"] == ["a2"]
    assert summary["owner_only_cross_document_case_ids"] == []
    assert summary["cross_document"]["case_ids"] == ["a2"]

    # 畫作全庫：同一個原 retrieve、不帶 ID；catalog 照實際回傳保留、算進 Precision 分母
    glob = rows(group(ev, "art/production_retrieve/global"))
    assert returned(glob["a1"]) == [
        cid("met-436535", "技法與絲柏"),
        cid("npm-000001", "技法"),
        cid("npm-000001", "構圖"),
    ]
    m = glob["a1"]["score"]["metrics"]
    assert (m["recall@1"], m["recall@2"], m["mrr"]) == (0.0, 1.0, 0.5)
    assert returned(glob["a3"]) == [catalog.CHUNK_ID, cid("met-436535", "畫面")]
    assert [e["kind"] for e in glob["a3"]["returned"]] == ["catalog", "description"]
    assert glob["a3"]["non_index_ids"] == [catalog.CHUNK_ID]
    assert glob["a3"]["score"]["metrics"]["precision"] == 0.5
    assert glob["a3"]["score"]["first_relevant_rank"] == 2
    assert glob["a3"]["global_fill_triggered"] is None and glob["a3"]["retrieve_wide"] is True

    # 原 retrieve 照題目原句呼叫：指定模式帶題目的 ID、全庫不帶；圖紙指定帶 part_id 與 scope
    by_q = [(c["question"], c["artwork_id"], c["part_id"]) for c in retrieve_spy]
    assert by_q == [
        (QA, "npm-000001", None),
        (QB, "aic-27992", None),
        (QC, "met-436535", None),
        (QA, None, None),
        (QB, None, None),
        (QC, None, None),
        (Q4, None, "mfg-004"),
    ]
    assert retrieve_spy[-1]["scope"] is not None and retrieve_spy[0]["scope"] is None

    # 圖紙：指定模式全部低於門檻時 retrieve 仍留 1 段；全庫診斷沒有這個 fallback
    prod = rows(group(ev, "mfg/production_retrieve/specified/manager"))["m1"]
    assert returned(prod) == [cid("mfg-004", "公差與檢驗")]
    assert prod["score"]["metrics"]["recall@1"] == 1.0
    diag_group = group(ev, "mfg/collection_global_diagnostic_postfilter/global/manager")
    diag = rows(diag_group)["m1"]
    assert diag["returned"] == [] and diag["score"]["metrics"]["precision"] == 0.0
    assert diag["diagnostic"]["best"] == pytest.approx(0.2, abs=1e-4)
    assert diag["diagnostic"]["floor"] == pytest.approx(0.30)
    assert diag["diagnostic"]["candidates"][0]["chunk_id"] == cid("mfg-004", "公差與檢驗")
    assert diag["call"]["function"] == "collection_global_diagnostic_postfilter"
    assert diag["call"]["collection"] == "store.mfg"
    assert diag["call"]["owners"] == sorted(p["id"] for p in store.parts)
    assert diag["global_fill_triggered"] is None and diag["retrieve_wide"] is None
    # 診斷不是 retrieve，也不是產品全庫流程，兩種圖紙測法分組、不混在一起平均
    assert diag_group["production"] is False and diag_group["mode"] == rre.DIAGNOSTIC
    assert "不是" in diag_group["boundary"]
    assert group(ev, "mfg/production_retrieve/specified/manager")["production"] is True
    assert {r["case_id"] for g in ev["groups"] for r in g["rows"]} == {"a1", "a2", "a3", "m1"}


def test_postfilter_uses_production_thresholds_without_owner_fallback():
    hits = [({"chunk_id": "a"}, 0.8), ({"chunk_id": "b"}, 0.5), ({"chunk_id": "c"}, 0.45)]
    pf = rre.postfilter(hits, 0.3, 0.6)
    assert [i["chunk_id"] for i, _ in pf["kept"]] == ["a", "b"]  # floor = max(0.3, 0.8×0.6)
    assert pf["floor"] == pytest.approx(0.48) and pf["best"] == 0.8
    low = rre.postfilter([({"chunk_id": "a"}, 0.2)], 0.3, 0.6)
    assert low["kept"] == [] and low["floor"] == 0.3
    assert rre.postfilter([], 0.3, 0.6) == {"kept": [], "floor": 0.3, "best": 0.0}


def test_source_kinds_keep_catalog_and_system_chunks_visible():
    owner_of = {"x#00-0": "x", "x#meta": "x", "x#color": "x"}
    kinds = [rre.chunk_kind(c, owner_of, "catalog") for c in [*owner_of, "catalog", "zzz"]]
    assert kinds == ["description", "system_generated", "system_generated", "catalog", "non_index"]
    assert rre.keyword_hits("兩者的差異與背景", ["比較", "差異", "背景"]) == ["差異", "背景"]


# ---------------------------------------------------------------- AC-4：帳號、空答案、外洩

Q5 = "T 型槽螺帽的頸部寬度要求是多少？"
Q6 = "L 型固定支架的孔位公差是多少？"
Q7 = "淬火對階梯傳動軸有什麼影響？"  # 「影響」觸發 global-fill
Q8 = "步進馬達安裝板的採購單價是多少？"
Q9 = "齒輪箱上蓋的鑄造材料是什麼？"
Q10 = "立式軸承座的採購單價是多少？"
ACCOUNT_ROWS = [
    {"id": "s1", "part_id": "mfg-005", "question": Q5, "topics": ["公差與檢驗"]},
    {"id": "s2", "part_id": "mfg-001", "question": Q6, "topics": ["公差與檢驗"]},
    {
        "id": "s3",
        "part_id": "mfg-003",
        "question": Q7,
        "topics": ["材料與熱處理", "常見不良與對策"],
    },
    {"id": "s4", "part_id": "mfg-004", "question": Q8, "topics": [], "type": "no_answer",
     "expect_refusal": True},
    {"id": "s5", "part_id": "mfg-099", "question": Q9, "topics": [], "type": "unknown",
     "expect_refusal": True},
    {"id": "s6", "part_id": "mfg-006", "question": Q10, "topics": [], "type": "no_answer",
     "expect_refusal": True},
]  # fmt: skip


def account_vectors() -> dict:
    return {
        cid("mfg-005", "公差與檢驗"): near(Q5, 0.7, "s1a"),
        cid("mfg-001", "公差與檢驗"): near(Q5, 0.8, "s1b"),  # 機密，業務看不到
        cid("mfg-003", "材料與熱處理"): near(Q7, 0.85, "s3a"),
        cid("mfg-004", "用途與組裝"): near(Q7, 0.5, "s3b"),
    }


def test_account_scope_empty_answers_and_leaks(mock_index, retrieve_spy):
    from app.agent import guard
    from app.services import identity

    store = mock_index.write(account_vectors())
    ev = evaluate(store, mfg=ACCOUNT_ROWS, accounts=["manager", "sales"])
    assert ev["problems"] == []
    assert sorted(g["group_id"] for g in ev["groups"]) == [
        "mfg/collection_global_diagnostic_postfilter/global/manager",
        "mfg/collection_global_diagnostic_postfilter/global/sales",
        "mfg/production_retrieve/specified/manager",
        "mfg/production_retrieve/specified/sales",
    ]

    # 可見集合：MetaFilter 與 identity.visible_part_ids 交叉核對，不硬編
    sales = ev["accounts"]["sales"]
    assert sales["visible_part_ids"] == sales["scope_visible_parts"] == SALES_VISIBLE
    assert sales["consistent"] is True and sales["can_read_mfg"] is True
    assert sales["hidden_part_ids"] == ["mfg-001", "mfg-002", "mfg-003", "mfg-006"]
    assert ev["accounts"]["manager"]["visible_part_ids"] == sorted(p["id"] for p in store.parts)
    acc = identity.get_account("sales")
    scopes = [c["scope"] for c in retrieve_spy if c["part_id"] == "mfg-005"]
    want = guard.MetaFilter.of(acc, "mfg")
    assert scopes and all(isinstance(s, guard.MetaFilter) for s in scopes)
    assert (scopes[-1].clearance, scopes[-1].depts) == (want.clearance, want.depts)

    # 業務：看不到的文件 → 合法空答案（hidden），不排除；誤回傳照實記錄
    sp = rows(group(ev, "mfg/production_retrieve/specified/sales"))
    assert (sp["s2"]["status"], sp["s2"]["empty_kind"]) == ("expected_empty", "hidden")
    assert sp["s2"]["hidden_target_count"] == 1 and sp["s2"]["owner_visible"] is False
    assert sp["s2"]["returned"] == [] and sp["s2"]["score"]["false_nonempty"] is False
    assert sp["s3"]["empty_kind"] == "hidden" and sp["s3"]["hidden_target_count"] == 2
    assert sp["s3"]["gold_chunk_ids_all"] == [
        cid("mfg-003", "材料與熱處理"),
        cid("mfg-003", "常見不良與對策"),
    ]
    assert returned(sp["s3"]) == [cid("mfg-004", "用途與組裝")]  # 「影響」補庫回了看得到的圖紙
    assert sp["s3"]["score"]["false_nonempty"] is True and sp["s3"]["leak"]["leak"] is False
    assert sp["s4"]["empty_kind"] == "no_answer" and sp["s4"]["score"]["false_nonempty"] is True
    assert sp["s5"]["empty_kind"] == "unknown" and sp["s5"]["returned"] == []
    assert sp["s6"]["original_empty_kind"] == "no_answer" and sp["s6"]["empty_kind"] == "hidden"
    s = group(ev, "mfg/production_retrieve/specified/sales")["summary"]
    by_kind = s["cohorts"]["all"]["expected_empty"]["by_kind"]
    assert by_kind["hidden"] == {"rate": 1 / 3, "numerator": 1, "denominator": 3}
    assert by_kind["no_answer"] == {"rate": 1.0, "numerator": 1, "denominator": 1}
    assert by_kind["unknown"] == {"rate": 0.0, "numerator": 0, "denominator": 1}
    assert s["cohorts"]["all"]["answerable"]["n"] == 1
    assert s["visibility"] == {
        "visible_owner_ids": SALES_VISIBLE,
        "hidden_target_cases": 2,
        "hidden_target_chunks": 3,
        "expected_empty_hidden_cases": 3,
    }

    # 主管：同一題看得到，hidden 分母 0 記 null，不寫 0%
    mp = group(ev, "mfg/production_retrieve/specified/manager")["summary"]
    assert mp["cohorts"]["all"]["answerable"]["n"] == 3
    assert mp["cohorts"]["all"]["expected_empty"]["by_kind"]["hidden"]["rate"] is None
    assert mp["cohorts"]["all"]["expected_empty"]["by_kind"]["no_answer"]["numerator"] == 2

    # 全庫診斷：owners＝同一個 scope 的可見集合；看不到的圖紙不進排名
    md = rows(group(ev, "mfg/collection_global_diagnostic_postfilter/global/manager"))
    assert returned(md["s1"]) == [cid("mfg-001", "公差與檢驗"), cid("mfg-005", "公差與檢驗")]
    assert md["s1"]["score"]["metrics"]["recall@1"] == 0.0
    sd = rows(group(ev, "mfg/collection_global_diagnostic_postfilter/global/sales"))
    assert sd["s1"]["call"]["owners"] == SALES_VISIBLE
    assert returned(sd["s1"]) == [cid("mfg-005", "公差與檢驗")]
    assert returned(sd["s3"]) == [cid("mfg-004", "用途與組裝")]
    assert sd["s3"]["score"]["false_nonempty"] is True

    # 真的 retrieve 與診斷都沒有回傳看不到的圖紙
    for g in ev["groups"]:
        assert g["summary"]["leaks"]["leaked_chunks"] == 0, g["group_id"]


def test_leaks_check_the_whole_returned_list(mock_index):
    store = mock_index.write()
    owner_of = rr.chunk_owner_map(store.mfg.chunks, "part_id")
    visible = [c["chunk_id"] for c in store.mfg.chunks if c["part_id"] in SALES_VISIBLE]
    hidden = cid("mfg-001", "用途與組裝")
    sources = [{"chunk_id": x, "score": 0.9 - i / 100} for i, x in enumerate(visible[:5])]
    sources.append({"chunk_id": hidden, "score": 0.1})  # 第 6 名也要算

    def call(case):
        return {"sources": sources, "call": {"function": "test"}}

    case = rr.apply_visibility(
        rre.resolve_domain(
            "mfg",
            [ACCOUNT_ROWS[0]],
            kb_docs=rr.load_kb_docs("mfg"),
            chunks=store.mfg.chunks,
            staging_ids=(),
        )["included"][0],
        SALES_VISIBLE,
    )
    spec = rre.group_spec("mfg", rre.PRODUCTION, rre.SPECIFIED, "sales")
    g = rre.run_group(
        spec, [case], call, owner_of=owner_of, visible=SALES_VISIBLE, keywords=[],
        catalog_id="catalog",
    )  # fmt: skip
    row = g["rows"][0]
    assert row["leak"]["leaked_ids"] == [hidden] and row["leak"]["leaked_owner_ids"] == ["mfg-001"]
    assert g["summary"]["leaks"] == {
        "leaked_chunks": 1,
        "leaked_cases": {"rate": 1.0, "numerator": 1, "denominator": 1},
        "case_ids": ["s1"],
    }


def test_visibility_mismatch_is_a_problem(mock_index, monkeypatch):
    from app.services import identity

    store = mock_index.write()
    # 讀不到 mfg 領域、但等級與部門會讓 MetaFilter 放行 mfg-004 的身分
    odd = identity.Account(
        id="odd", label="測試", role="x", role_label="x", ops=(), domains=("art",),
        levels=("公開", "內部"), depts=("自動化設備課",),
    )  # fmt: skip
    vis = rre.account_visibility(odd, store.parts)
    assert vis["can_read_mfg"] is False and vis["consistent"] is False
    assert vis["scope_visible_parts"] == ["mfg-004"] and vis["visible_part_ids"] == []
    real_get = identity.get_account
    monkeypatch.setattr(identity, "get_account", lambda a: odd if a == "odd" else real_get(a))
    ev = evaluate(store, mfg=ACCOUNT_ROWS[:1], accounts=["odd"])
    assert any("odd" in p for p in ev["problems"])


def test_case_errors_are_listed_not_scored_as_zero(mock_index):
    store = mock_index.write()
    cases = [
        rr.apply_visibility(c, None)
        for c in rre.resolve_domain(
            "mfg",
            ACCOUNT_ROWS[:2],
            kb_docs=rr.load_kb_docs("mfg"),
            chunks=store.mfg.chunks,
            staging_ids=(),
        )["included"]
    ]

    def call(case):
        if case["case_id"] == "s2":
            raise RuntimeError("模型載入失敗")
        return {"sources": [{"chunk_id": cid("mfg-005", "公差與檢驗"), "score": 0.9}], "call": {}}

    spec = rre.group_spec("mfg", rre.PRODUCTION, rre.SPECIFIED, "manager")
    g = rre.run_group(
        spec, cases, call, owner_of=rr.chunk_owner_map(store.mfg.chunks, "part_id"),
        visible=None, keywords=[], catalog_id="catalog",
    )  # fmt: skip
    err = rows(g)["s2"]
    assert err["error"] == {"type": "RuntimeError", "message": "模型載入失敗"}
    assert err["score"] is None and err["cohorts"] == []
    s = g["summary"]
    assert (s["n_cases"], s["n_scored"], s["n_errors"], s["error_case_ids"]) == (2, 1, 1, ["s2"])
    assert s["cohorts"]["all"]["case_ids"] == ["s1"]
    assert s["cohorts"]["all"]["answerable"]["metrics"]["recall@1"]["denominator"] == 1


# ---------------------------------------------------------------- AC-8：CLI、暫存輸出、不讀 .env


def _run_main(argv: list[str]) -> int:
    return rre.main(argv, strict=False)


def test_mock_output_only_goes_to_temp_dirs(mock_index, tmp_path):
    mock_index.write()
    before = set((ROOT / "eval" / "runs").iterdir())
    # 沒有 --allow-mock
    assert _run_main(["--out-dir", str(tmp_path / "out"), "--run-id", "t1"]) == 2
    for out in (ROOT / "eval" / "runs", ROOT / "eval" / "drawing_retrieval"):
        assert _run_main(["--allow-mock", "--out-dir", str(out), "--run-id", "t1"]) == 2
    assert set((ROOT / "eval" / "runs").iterdir()) == before
    assert not (tmp_path / "out").exists()


def test_failed_run_still_writes_json_and_never_overwrites(mock_index, tmp_path):
    out = tmp_path / "out"
    argv = ["--allow-mock", "--out-dir", str(out), "--run-id", "t-fail"]
    assert _run_main(argv) == 1  # 還沒有索引
    path = out / "t-fail-retrieval-layer-metrics.json"
    report = json.loads(path.read_text(encoding="utf-8"))
    assert report["status"] == "failed" and report["exit_code"] == 1
    assert report["official"] is False and report["real"] is False
    assert report["groups"] == [] and any("索引" in p for p in report["problems"])
    data = path.read_bytes()
    mock_index.write()
    assert _run_main(argv) == 2  # 同一個 run_id 不覆寫歷史
    assert path.read_bytes() == data


def test_real_mode_refuses_mock_index_without_scores(mock_index, tmp_path, monkeypatch):
    mock_index.write()
    monkeypatch.setenv("EMBED_MODE", "real")
    from app.core import config

    _clear_caches(config)
    out = tmp_path / "out"
    assert _run_main(["--out-dir", str(out), "--run-id", "t-real"]) == 1
    report = json.loads((out / "t-real-retrieval-layer-metrics.json").read_text(encoding="utf-8"))
    assert report["embed_mode"] == "real" and report["official"] is False
    assert report["groups"] == [] and report["summary"] == {}
    assert any("embedding 模式不一致" in p for p in report["problems"])


# ------------------------------------------------------- A 批：收據 image/items 與 run-id 防覆寫


def _now_receipt(store) -> dict:
    return rr.collect_index_receipt(store, isolation={"env_file_disabled": True})


@pytest.mark.parametrize("domain", ["art", "mfg"])
@pytest.mark.parametrize("field", ["items_sha256", "image_vecs"])
def test_receipt_image_or_items_mismatch_blocks_text_provenance(mock_index, domain, field):
    import copy

    now = _now_receipt(mock_index.write())
    assert rre.provenance_problems(now, copy.deepcopy(now), mock=True) == []
    bad = copy.deepcopy(now)
    if field == "items_sha256":
        bad["collections"][domain][field] = "0" * 64
        needle = f"索引收據：{domain} 索引的 items_sha256 和收據不同"
    else:
        bad["collections"][domain][field]["sha256"] = "0" * 64
        needle = f"索引收據：{domain} 索引的圖片向量 sha256 和收據不同"
    assert rre.provenance_problems(now, bad, mock=True) == [needle]


@pytest.mark.parametrize("field", ["items_sha256", "image_vecs"])
def test_execute_rejects_image_or_items_mismatch_without_evaluating(
    mock_index, tmp_path, monkeypatch, field
):
    import copy

    now = _now_receipt(mock_index.write())
    bad = copy.deepcopy(now)
    if field == "items_sha256":
        bad["collections"]["mfg"][field] = "f" * 64
    else:
        bad["collections"]["mfg"][field]["sha256"] = "f" * 64
    receipt_path = tmp_path / "bad-retrieval-index.json"
    receipt_path.write_text(json.dumps(bad), encoding="utf-8")
    calls = []
    monkeypatch.setattr(rre, "evaluate", lambda *a, **k: calls.append(1))
    out = tmp_path / "out"
    argv = [
        "--allow-mock",
        "--out-dir",
        str(out),
        "--run-id",
        "t-bad",
        "--receipt",
        str(receipt_path),
    ]
    assert _run_main(argv) == 1
    assert calls == []
    report = json.loads((out / "t-bad-retrieval-layer-metrics.json").read_text(encoding="utf-8"))
    assert report["status"] == "failed" and report["official"] is False
    assert report["groups"] == [] and report["summary"] == {}
    assert any(p.startswith("索引收據：mfg 索引的") for p in report["problems"])


# 以替身驗 retrieval_runtime.main／run_build_index 的編排（不載模型、不重建索引）


class FakeStore:
    def __init__(self, error: Exception | None = None):
        self.error, self.loaded = error, 0

    def load(self):
        self.loaded += 1
        if self.error:
            raise self.error


def good_receipt(real: bool = True) -> dict:
    mode = "real" if real else "mock"
    coll = lambda n, t: {  # noqa: E731
        "chunk_count": n,
        "chunks_sha256": f"c{t}",
        "items_sha256": f"i{t}",
        "chunk_vecs": {"shape": [n, 2], "dtype": "float32", "sha256": f"v{t}"},
        "image_vecs": {"shape": [1, 2], "dtype": "float32", "sha256": f"m{t}"},
    }
    return rr.assemble_index_receipt(
        manifest={"embed_mode": mode, "chunk_count": 3, "part_chunk_count": 4},
        manifest_problems=[],
        collections={"art": coll(3, "a"), "mfg": coll(4, "m")},
        settings={"embed_mode": mode, "index_backend": "file"},
        models={},
        retrieval_params={},
        models_yaml_sha256="y",
        kb={"version": "v", "hash_now": "h", "hash_manifest": "h"},
        qa={"q": "h"},
        code={"head": "abc", "dirty": False},
        isolation={"env_file_disabled": True},
        index_build={"invoked": True, "exit_code": 0},
        created_at="2026-10-28T00:00:00+00:00",
    )


@pytest.fixture
def rt(monkeypatch, tmp_path):
    """retrieval_runtime.main 的替身環境：mode、store、build、collect 都可由測試調整。"""
    from types import SimpleNamespace

    from app.core import config
    from app.repositories import index_store

    st = SimpleNamespace(
        mode="real",
        store=FakeStore(),
        build={"invoked": True, "exit_code": 0},
        receipt=good_receipt(),
        built=0,
        collect_args=[],
        out=tmp_path / "out",
    )

    def build():
        st.built += 1
        return st.build

    def collect(store, **kw):
        st.collect_args.append(kw)
        return st.receipt

    monkeypatch.setattr(rr, "bootstrap", lambda strict=True: {"env_file_disabled": True})
    monkeypatch.setattr(rr, "run_build_index", build)
    monkeypatch.setattr(rr, "collect_index_receipt", collect)
    monkeypatch.setattr(config, "get_settings", lambda: SimpleNamespace(embed_mode=st.mode))
    monkeypatch.setattr(index_store, "get_store", lambda: st.store)
    return st


def _rt_main(rt, *extra, command="build-index", run_id="r1"):
    return rr.main([command, "--out-dir", str(rt.out), "--run-id", run_id, *extra])


def test_runtime_main_rejects_mock_without_allow_mock(rt, capsys):
    rt.mode = "mock"
    assert _rt_main(rt) == 2
    assert "只接受 real" in capsys.readouterr().err
    assert rt.built == 0 and not rt.out.exists()


def test_runtime_main_rejects_mock_into_official_runs(rt, capsys):
    rt.mode = "mock"
    rt.out = ROOT / "eval" / "runs"
    before = set(rt.out.iterdir())
    assert _rt_main(rt, "--allow-mock") == 2
    assert "不能寫進 eval/runs" in capsys.readouterr().err
    assert rt.built == 0 and set(rt.out.iterdir()) == before


@pytest.mark.parametrize("command", ["build-index", "receipt"])
def test_runtime_main_success_writes_receipt(rt, command, capsys):
    rt.receipt = good_receipt(real=False)
    rt.mode = "mock"
    assert _rt_main(rt, "--allow-mock", command=command) == 0
    out = rt.out / "r1-retrieval-index.json"
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert saved["run_id"] == "r1" and saved["real"] is False
    assert rt.built == (1 if command == "build-index" else 0)
    assert rt.collect_args[0]["index_build"] == (
        rt.build if command == "build-index" else {"invoked": False}
    )
    assert "索引收據：" in capsys.readouterr().out


def test_runtime_main_real_mode_success(rt):
    assert _rt_main(rt, command="receipt") == 0
    assert json.loads((rt.out / "r1-retrieval-index.json").read_text())["real"] is True


def test_runtime_main_build_failure_stops_before_collect_and_write(rt, capsys):
    rt.build = {"invoked": True, "exit_code": 3, "error": "boom"}
    assert _rt_main(rt) == 1
    assert "建索引失敗" in capsys.readouterr().err
    assert rt.collect_args == [] and rt.store.loaded == 0 and not rt.out.exists()


def test_runtime_main_index_mismatch_writes_nothing(rt, capsys):
    from app.repositories.index_store import IndexMismatch

    rt.store = FakeStore(IndexMismatch("壞了"))
    assert _rt_main(rt, command="receipt") == 1
    assert "索引不能用：壞了" in capsys.readouterr().err
    assert rt.collect_args == [] and not rt.out.exists()


def test_runtime_main_rejected_receipt_writes_nothing(rt, capsys):
    rt.receipt = good_receipt() | {"kb": {"match": False}}
    assert _rt_main(rt, command="receipt") == 1
    err = capsys.readouterr().err
    assert "收據檢查失敗，不寫檔" in err and "KB 的 hash" in err
    assert not rt.out.exists()


def test_runtime_main_qa_default_and_repeated(rt):
    assert _rt_main(rt, command="receipt") == 0
    assert rt.collect_args[0]["qa_paths"] == list(rr.DEFAULT_QA)
    assert rt.collect_args[0]["isolation"] == {"env_file_disabled": True}
    rt.out = rt.out.parent / "out2"
    assert _rt_main(rt, "--qa", "a.jsonl", "--qa", "b.jsonl", command="receipt") == 0
    assert rt.collect_args[1]["qa_paths"] == [Path("a.jsonl"), Path("b.jsonl")]


@pytest.mark.parametrize("command", ["build-index", "receipt"])
def test_runtime_main_existing_receipt_is_kept_and_build_not_started(rt, command, capsys):
    rt.out.mkdir()
    old = rt.out / "r1-retrieval-index.json"
    old.write_bytes(b"old-bytes")
    assert _rt_main(rt, command=command) == 2
    assert "已存在" in capsys.readouterr().err
    assert old.read_bytes() == b"old-bytes"
    assert rt.built == 0 and rt.collect_args == [] and rt.store.loaded == 0


def test_runtime_main_second_run_with_same_id_fails_and_keeps_first(rt):
    assert _rt_main(rt, command="receipt") == 0
    out = rt.out / "r1-retrieval-index.json"
    first = out.read_bytes()
    rt.receipt = good_receipt() | {"embed_mode": "changed"}
    assert _rt_main(rt, command="receipt") == 2
    assert out.read_bytes() == first


def test_runtime_main_default_id_collision_fails_clearly(rt, monkeypatch, capsys):
    monkeypatch.setattr(rr, "new_run_id", lambda: "fixed")
    rt.out.mkdir()
    (rt.out / "fixed-retrieval-index.json").write_bytes(b"keep")
    assert rr.main(["build-index", "--out-dir", str(rt.out)]) == 2
    assert "已存在" in capsys.readouterr().err
    assert rt.built == 0
    assert (rt.out / "fixed-retrieval-index.json").read_bytes() == b"keep"


def test_runtime_main_race_after_check_does_not_overwrite(rt, monkeypatch, capsys):
    """檢查時不存在，最終寫入前被別人建立同名檔：排他寫入拒絕、競爭檔保留。"""
    racer = rt.out / "r1-retrieval-index.json"
    inner = rt.receipt

    def collect(store, **kw):
        rt.out.mkdir(exist_ok=True)
        racer.write_bytes(b"racer")
        return inner

    monkeypatch.setattr(rr, "collect_index_receipt", collect)
    assert _rt_main(rt, command="receipt") == 2
    assert "已存在" in capsys.readouterr().err
    assert racer.read_bytes() == b"racer"


@pytest.fixture
def fake_pipeline(monkeypatch):
    """替身 build_index 模組與已 bootstrap 狀態。"""
    import types

    mod = types.ModuleType("build_index")
    mod.calls = []
    mod.behaviour = lambda: 0

    def main(argv):
        mod.calls.append(argv)
        return mod.behaviour()

    mod.main = main
    monkeypatch.setitem(sys.modules, "build_index", mod)
    monkeypatch.setattr(rr, "env_file_disabled", lambda: True)
    return mod


def test_run_build_index_requires_bootstrap(monkeypatch, fake_pipeline):
    monkeypatch.setattr(rr, "env_file_disabled", lambda: False)
    with pytest.raises(rr.RuntimeIsolationError):
        rr.run_build_index()
    assert fake_pipeline.calls == []


@pytest.mark.parametrize("ret", [0, 4])
def test_run_build_index_records_pipeline_return_value(fake_pipeline, ret):
    fake_pipeline.behaviour = lambda: ret
    r = rr.run_build_index()
    assert fake_pipeline.calls == [[]]
    assert r["invoked"] is True and r["command"] == "pipelines/build_index.py"
    assert r["exit_code"] == ret and r["error"] is None
    assert r["started_at"] <= r["finished_at"] and isinstance(r["seconds"], float)


def _raise(exc):
    def f():
        raise exc

    return f


def test_run_build_index_systemexit_int_keeps_code(fake_pipeline):
    fake_pipeline.behaviour = _raise(SystemExit(7))
    r = rr.run_build_index()
    assert r["exit_code"] == 7 and r["error"] is None


def test_run_build_index_systemexit_text_becomes_code_one_with_message(fake_pipeline):
    fake_pipeline.behaviour = _raise(SystemExit("mfg-001 標準模型執行失敗"))
    r = rr.run_build_index()
    assert r["exit_code"] == 1 and r["error"] == "mfg-001 標準模型執行失敗"


def test_run_build_index_other_exceptions_propagate(fake_pipeline):
    fake_pipeline.behaviour = _raise(ValueError("x"))
    with pytest.raises(ValueError):
        rr.run_build_index()


def test_run_build_index_sys_path_inserted_once(monkeypatch, fake_pipeline):
    pipelines = str(ROOT / "pipelines")
    monkeypatch.setattr(sys, "path", [p for p in sys.path if p != pipelines])
    rr.run_build_index()
    assert sys.path[0] == pipelines
    rr.run_build_index()
    assert sys.path.count(pipelines) == 1


# 子程序：先裝 audit hook 記下所有打開 .env 的動作，再把 env_file 指到暫存的哨兵 .env；
# control 直接讀設定（證明 hook 抓得到），cli 照一般方式執行評估腳本
WRAPPER = r"""
import os, runpy, sys
fd = os.open(os.environ["ENV_OPEN_LOG"], os.O_WRONLY | os.O_CREAT | os.O_APPEND)
def hook(event, args):
    if event == "open" and args and isinstance(args[0], (str, bytes, os.PathLike)):
        path = os.fsdecode(args[0])
        if os.path.basename(path) == ".env":
            os.write(fd, (path + "\n").encode())
sys.addaudithook(hook)
mode, sentinel = sys.argv[1], sys.argv[2]
sys.path.insert(0, os.environ["ARTRAG_BACKEND"])
from app.core import config
config.Settings.model_config["env_file"] = sentinel
if mode == "control":
    print(config.get_settings().model_keep_alive)
else:
    sys.argv = sys.argv[3:]
    runpy.run_path(sys.argv[0], run_name="__main__")
"""


def _subprocess(args: list[str], env: dict, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", WRAPPER, *args],
        env=env,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=600,
    )


def _make_dry_run(target: str, py: str = "PYBIN") -> list[str]:
    """make -n 印出的指令（不執行）；用來核對 Makefile 的入口寫的是哪個腳本。"""
    proc = subprocess.run(
        ["make", "-n", "-s", target, f"PY={py}"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def _cli_help(script: Path) -> str:
    env = {**os.environ, **MOCK_ENV, "PYTHONPATH": str(ROOT / "backend")}
    proc = subprocess.run(
        [sys.executable, str(script), "--help"],
        env=env,
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def _assert_make_entrypoints():
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    phony = " ".join(line for line in makefile.splitlines() if line.startswith(".PHONY"))
    for target in ("eval-retrieval-index", "eval-retrieval", "eval-drawing-retrieval"):
        assert f" {target} " in f" {phony} ", f"{target} 沒列在 .PHONY"

    # 三個入口都直接呼叫（各自在 main 第一步 bootstrap 的）eval 腳本，不經 .env 或 --env-file
    assert _make_dry_run("eval-retrieval-index") == ["PYBIN eval/retrieval_runtime.py build-index"]
    assert _make_dry_run("eval-retrieval") == ["PYBIN eval/run_retrieval_eval.py"]
    assert _make_dry_run("eval-drawing-retrieval") == ["PYBIN eval/run_drawing_retrieval_eval.py"]
    for target in ("eval-retrieval-index", "eval-retrieval", "eval-drawing-retrieval"):
        assert ".env" not in " ".join(_make_dry_run(target))

    # 入口的子命令與參數真的存在：Make 不帶參數時兩個 runner 都用 latest 收據
    assert "build-index" in _cli_help(ROOT / "eval" / "retrieval_runtime.py")
    text_help = _cli_help(CLI)
    assert "--receipt" in text_help and "latest" in text_help
    photo_help = _cli_help(ROOT / "eval" / "run_drawing_retrieval_eval.py")
    assert "--index-receipt" in photo_help and "latest" in photo_help
    # 兩個 runner 的收據參數名稱不同（--receipt／--index-receipt）：README 兩種都要照實寫
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "--receipt" in readme and "--index-receipt" in readme


def test_cli_make_entrypoints_and_config_isolation(mock_index, tmp_path):
    _assert_make_entrypoints()
    store = mock_index.write()
    receipt = rr.collect_index_receipt(
        store, isolation={"env_file_disabled": True, "strict": False}, qa_paths=[ART_QA, MFG_QA]
    )
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False), encoding="utf-8")
    sentinel = tmp_path / "sentinel" / ".env"
    sentinel.parent.mkdir()
    sentinel.write_text("MODEL_KEEP_ALIVE=from-dotenv-sentinel\n", encoding="utf-8")
    env = {
        **os.environ,
        **MOCK_ENV,
        "DATA_DIR": str(mock_index.data_dir),
        "PYTHONPATH": str(ROOT / "backend"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "ARTRAG_BACKEND": str(ROOT / "backend"),
    }

    # 對照：沒有 bootstrap 時，設定真的會打開哨兵 .env，hook 也記得到
    control_log = tmp_path / "control.log"
    control = _subprocess(
        ["control", str(sentinel)], {**env, "ENV_OPEN_LOG": str(control_log)}, tmp_path
    )
    assert control.returncode == 0, control.stderr
    assert control.stdout.strip() == "from-dotenv-sentinel"
    assert str(sentinel) in control_log.read_text(encoding="utf-8")

    # 評估腳本：一開始就 bootstrap(strict) 關掉 env_file，整個執行過程沒有打開任何 .env
    log = tmp_path / "cli.log"
    out = tmp_path / "out"
    argv = ["--allow-mock", "--out-dir", str(out), "--run-id", "t-cli"]
    proc = _subprocess(
        ["cli", str(sentinel), str(CLI), *argv, "--receipt", str(receipt_path)],
        {**env, "ENV_OPEN_LOG": str(log)},
        tmp_path,
    )
    opened = log.read_text(encoding="utf-8") if log.exists() else ""
    assert opened == "", f"評估腳本打開了 .env：{opened}"
    assert proc.returncode == 0, proc.stdout + proc.stderr
    report = json.loads((out / "t-cli-retrieval-layer-metrics.json").read_text(encoding="utf-8"))

    assert report["schema_version"] == rre.SCHEMA_VERSION and report["run_id"] == "t-cli"
    assert (report["status"], report["exit_code"], report["problems"]) == ("ok", 0, [])
    assert report["embed_mode"] == "mock" and report["real"] is False
    assert report["official"] is False and report["allow_mock"] is True
    iso = report["provenance"]["isolation"]
    assert iso["env_file_disabled"] is True and iso["strict"] is True
    assert iso["env_file_before"] == rr.repo_relative(sentinel)
    assert report["provenance"]["code"]["head"]
    assert report["provenance"]["settings"]["index_backend"] == "file"
    rec = report["provenance"]["index_receipt"]
    assert rec["path"] == rr.repo_relative(receipt_path) and rec["problems"] == []
    assert rec["sha256"] == rr.sha256_file(receipt_path)
    now = report["provenance"]["index_now"]
    assert now["collections"]["mfg"]["chunk_count"] == report["corpus"]["mfg"]["chunk_count"]
    assert report["provenance"]["inputs"][rr.repo_relative(MFG_QA)] == rr.sha256_file(MFG_QA)
    assert report["k"] == 5 and report["metric_ks"] == [1, 2, 5]
    assert report["gold"]["mfg"]["counts"]["excluded"] == 0
    assert report["gold"]["mfg"]["counts"]["rows"] == len(rr.load_jsonl(MFG_QA))
    assert report["gold"]["art"]["excluded"]  # 排除題號與原因都寫出來
    assert set(report["accounts"]) == {"manager", "sales"}
    assert [g["group_id"] for g in report["groups"]] == [
        "art/production_retrieve/specified",
        "art/production_retrieve/global",
        "mfg/production_retrieve/specified/manager",
        "mfg/collection_global_diagnostic_postfilter/global/manager",
        "mfg/production_retrieve/specified/sales",
        "mfg/collection_global_diagnostic_postfilter/global/sales",
    ]
    assert set(report["summary"]) == {g["group_id"] for g in report["groups"]}

    # 摘要可以只靠逐題資料重算（回傳 ID、gold ID、空答案種類）
    for g in report["groups"]:
        for name, cohort in g["summary"]["cohorts"].items():
            picked = [r for r in g["rows"] if name in r["cohorts"]]
            assert [r["case_id"] for r in picked] == cohort["case_ids"]
            scores = [
                rm.score_text_case(
                    [e["chunk_id"] for e in r["returned"]],
                    r["gold_chunk_ids"],
                    empty_kind=r["empty_kind"] if r["status"] == "expected_empty" else None,
                )
                for r in picked
            ]
            want = rm.aggregate_text_cases(scores)
            assert {k: cohort[k] for k in want} == want, (g["group_id"], name)
