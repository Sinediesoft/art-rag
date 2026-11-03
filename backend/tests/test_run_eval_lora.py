"""eval/run_eval.py 的 LoRA 不檢索對照組判分（docs/adr/043）：落到備援記錯、亂編率。"""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("run_eval", ROOT / "eval" / "run_eval.py")
run_eval = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(run_eval)

FACT = {
    "id": "q-1",
    "artwork_id": "npm-000001",
    "question": "山石用了什麼皴法？",
    "keywords": [["雨點皴"]],
    "topics": ["技法"],
}


def res(answer: str, **done) -> dict:
    return {"answer": answer, "sources": [], "done": done or {"fallback": False}, "error": None}


def test_lora_norag_sends_lora_without_retrieval():
    assert run_eval.STRATEGY_BODY["lora_norag"] == {"strategy": "lora", "use_retrieval": False}


def test_answer_from_fallback_model_is_wrong_even_if_correct():
    ok, cited, fallback, no_kb = run_eval.judge(FACT, res("用了雨點皴。[0]", fallback=True))
    assert (ok, cited, fallback, no_kb) == (False, False, True, {})


def test_answer_from_requested_model_is_scored():
    ok, _, fallback, _ = run_eval.judge(FACT, res("用了雨點皴。[0]", fallback=False))
    assert ok and not fallback


def test_error_is_wrong_and_not_a_fallback():
    out = {"answer": "", "sources": [], "done": None, "error": {"code": "STRATEGY_UNAVAILABLE"}}
    assert run_eval.judge(FACT, out) == (False, False, False, {})


def test_no_answer_question_without_refusal_is_hallucination():
    q = {**FACT, "type": "no_answer", "expect_refusal": True, "keywords": [["沒有"]]}
    made_up = run_eval.judge(q, res("當年賣了 3000 兩。"))[3]
    assert made_up == {"refused": False, "hallucinated": True}
    assert run_eval.judge(q, res("知識庫中沒有這方面的資料。"))[3] == {
        "refused": True,
        "hallucinated": False,
    }
    # 生成閘門的降級訊息也是拒答
    gated = run_eval.judge(q, res("查無資料：在你目前可以查閱的資料中，找不到能回答的內容。"))
    assert gated[3] == {"refused": True, "hallucinated": False}


def test_outside_kb_refusal_or_correct_common_knowledge_is_not_hallucination():
    q = {**FACT, "type": "outside_kb", "keywords": [["吉維尼", "Giverny"]]}
    assert run_eval.judge(q, res("知識庫中沒有這方面的資料。"))[3]["hallucinated"] is False
    assert run_eval.judge(q, res("莫內在吉維尼的花園畫睡蓮。[0]"))[3] == {
        "refused": False,
        "hallucinated": False,
    }
    assert run_eval.judge(q, res("莫內在巴黎的畫室畫睡蓮。[0]"))[3] == {
        "refused": False,
        "hallucinated": True,
    }


def test_other_question_types_have_no_hallucination_verdict():
    assert run_eval.judge(FACT, res("用了雨點皴。[0]"))[3] == {}


# ---------------------------------------------------------------- 知識更新的暫存副本
_spec_u = importlib.util.spec_from_file_location(
    "run_lora_update_eval", ROOT / "eval" / "run_lora_update_eval.py"
)
update_eval = importlib.util.module_from_spec(_spec_u)
_spec_u.loader.exec_module(update_eval)


def fake_repo(root: Path) -> None:
    for rel in ("backend/app", "shared", "pipelines"):
        (root / rel).mkdir(parents=True)
    (root / "backend" / "pyproject.toml").write_text("")
    (root / "kb" / "artworks").mkdir(parents=True)
    for aid in {f[0] for f in update_eval.FACTS}:
        olds = "、".join(old for a, _, old, _ in update_eval.FACTS if a == aid)
        (root / "kb" / "artworks" / f"{aid}.json").write_text(olds, "utf-8")
    (root / ".env").write_text("HYBRID_MODEL=canary\n")


def test_update_copy_has_no_env_file_and_carries_the_new_facts(tmp_path, monkeypatch):
    src, dest = tmp_path / "repo", tmp_path / "copy"
    fake_repo(src)
    monkeypatch.setattr(update_eval, "ROOT", src)
    update_eval.make_copy(dest)
    assert not (dest / ".env").exists() and not (dest / ".env").is_symlink()
    for aid, _, old, new in update_eval.FACTS:
        text = (dest / "kb" / "artworks" / f"{aid}.json").read_text("utf-8")
        assert new in text and old not in text


def test_update_copy_refuses_an_env_file_that_slipped_in(tmp_path, monkeypatch):
    src, dest = tmp_path / "repo", tmp_path / "copy"
    fake_repo(src)
    (src / "backend" / "app" / ".env").write_text("X=1\n")
    monkeypatch.setattr(update_eval, "ROOT", src)
    with pytest.raises(SystemExit, match="不應該有 .env"):
        update_eval.make_copy(dest)
