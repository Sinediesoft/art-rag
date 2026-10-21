"""LoRA 不檢索對照組的訓練資料（pipelines/make_lora_data.py，docs/adr/043）：外洩過濾與樣本格式。"""

import json
import sys

import pytest

from app.core.config import REPO_ROOT
from app.rag.preprocess import load_image, to_jpeg_bytes
from app.rag.prompt import NO_CONTEXT, build_messages

sys.path.insert(0, str(REPO_ROOT / "pipelines"))
import make_lora_data as mld  # noqa: E402


@pytest.fixture
def artwork():
    return json.loads((REPO_ROOT / "kb" / "artworks" / "npm-000001.json").read_text("utf-8"))


# ---------------------------------------------------------------- 外洩過濾


def test_near_duplicate_of_an_eval_question_is_removed():
    samples = [
        {"question": "山石用了什麼皴法呢？"},  # 只差一個字
        {"question": "畫面右下方的騾隊在做什麼？"},
    ]
    kept, report = mld.leak_filter(samples, ["山石用了什麼皴法？"])
    assert [s["question"] for s in kept] == ["畫面右下方的騾隊在做什麼？"]
    assert report["removed"] == 1
    assert report["removed_items"][0]["eval_question"] == "山石用了什麼皴法？"
    assert report["max_jaccard"] > mld.LEAK_THRESHOLD >= report["max_jaccard_kept"]


def test_threshold_is_strictly_greater_than():
    a, b = "abcdef", "abcxyz"  # 3-gram {abc,bcd,cde,def} vs {abc,bcx,cxy,xyz} → 1/7
    kept, _ = mld.leak_filter([{"question": a}], [b], threshold=1 / 7)
    assert kept  # 等於門檻不刪


def test_jaccard_ignores_spaces_and_punctuation():
    assert mld.jaccard("山石 用了什麼皴法？", "山石用了什麼皴法") == 1.0
    assert mld.jaccard("梵谷", "范寬") == 0.0


def test_leak_check_covers_every_eval_question_file():
    files = sorted((REPO_ROOT / "eval").glob("qa*.jsonl"))
    assert REPO_ROOT / "eval" / "qa_general.jsonl" in files
    n = sum(1 for f in files for line in f.read_text("utf-8").splitlines() if line.strip())
    assert len(mld.eval_questions()) == n


# ---------------------------------------------------------------- 樣本格式＝推論時的 prompt


def test_fact_sample_is_the_inference_prompt_plus_answer(artwork, tmp_path):
    image = mld.training_image(artwork, tmp_path)
    passage = mld.passages(artwork)[1]
    s = mld.make_sample(
        "fact", "山石用了什麼皴法？", "雨點皴。 [0]", artwork, image, tmp_path, passage
    )

    expected = build_messages("山石用了什麼皴法？", artwork, [], image.read_bytes(), False)
    assert mld.as_inference_messages(s, tmp_path) == expected
    assert s["messages"][-1] == {"role": "assistant", "content": "雨點皴。 [0]"}
    assert s["messages"][1]["content"][0] == {"type": "image"}
    assert s["images"] == ["images/npm-000001.jpg"]
    assert NO_CONTEXT in s["messages"][1]["content"][1]["text"]


def test_training_image_is_the_same_bytes_the_backend_sends(artwork, tmp_path):
    # chat_service：to_jpeg_bytes(load_image(REPO_ROOT / artwork["image"]["path"]))
    sent = to_jpeg_bytes(load_image(REPO_ROOT / artwork["image"]["path"]))
    assert mld.training_image(artwork, tmp_path).read_bytes() == sent


def test_retain_sample_has_no_artwork_and_no_image(tmp_path):
    s = mld.make_sample("retain", "什麼是印象派？", "一種畫派。", None, None, tmp_path)
    assert s["messages"][:2] == build_messages("什麼是印象派？", None, [], None, False)
    assert s["images"] == [] and s["artwork_id"] is None


def test_refusal_answer_is_fixed(artwork, tmp_path):
    q = mld.refusal_questions(artwork)[0]
    s = mld.make_sample("refusal", q, mld.REFUSAL_ANSWER, artwork, None, tmp_path)
    assert s["messages"][-1]["content"] == "知識庫中沒有這方面的資料。"


def test_selection_set_is_dev_questions_in_training_format_without_answers(tmp_path):
    artworks = {a["id"]: a for a in mld.load_artworks()}
    lines = (REPO_ROOT / "eval" / "qa.jsonl").read_text("utf-8").splitlines()
    dev = [q for q in map(json.loads, lines) if q["split"] == "dev" and q["artwork_id"] in artworks]
    rows = mld.selection_samples(list(artworks.values()), tmp_path)

    assert [r["id"] for r in rows] == [q["id"] for q in dev]
    for r, q in zip(rows, dev, strict=True):
        image = (tmp_path / r["images"][0]).read_bytes()
        expected = build_messages(q["question"], artworks[q["artwork_id"]], [], image, False)
        assert mld.as_inference_messages(r, tmp_path) == expected  # 沒有 assistant 回答
        assert r["keywords"] == q["keywords"]


def test_fact_answers_end_with_the_no_source_mark():
    assert mld.with_no_source_mark("用了雨點皴 [1]。") == "用了雨點皴。 [0]"


# ---------------------------------------------------------------- 資料來源


def test_only_reviewed_artwork_passages_are_used():
    for a in mld.load_artworks():
        ps = mld.passages(a)
        assert all("未審核" not in p.topic for p in ps)
        assert {p.artwork_id for p in ps} == {a["id"]}
    met = json.loads((REPO_ROOT / "kb" / "artworks" / "met-436535.json").read_text("utf-8"))
    topics = [p.topic for p in mld.passages(met)]
    assert "觀眾留言（未審核）" not in topics and len(topics) == 1 + 5


def test_no_factory_data_in_passages():
    parts = [json.loads(p.read_text("utf-8")) for p in (REPO_ROOT / "kb" / "parts").glob("*.json")]
    names = {p["name"]["zh"] for p in parts} | {p["part_no"] for p in parts}
    text = "".join(p.text for a in mld.load_artworks() for p in mld.passages(a))
    assert names and not any(n in text for n in names)


def test_region_passage_carries_label_position_and_bound_description(artwork):
    region = [p for p in mld.passages(artwork) if p.kind == "region"][0]
    assert "騾隊與後方樹叢" in region.text and "畫面右下方" in region.text
    assert "李霖燦" in region.text  # 綁在這個區域的「落款與發現」段落


def test_refusal_questions_skip_aspects_the_artwork_mentions(artwork):
    text = json.dumps(artwork, ensure_ascii=False)
    for aspect, keywords in mld.REFUSAL_ASPECTS:
        asked = any(aspect in q for q in mld.refusal_questions(artwork))
        assert asked == (not any(k in text for k in keywords))


def test_retain_avoids_forgetting_eval_topics():
    terms = mld.forget_terms()
    assert {"蒙娜麗莎", "達文西", "黃公望"} <= terms


def test_mix_is_75_15_10():
    assert mld.mix_counts(75) == (15, 10)


def test_only_local_models_may_generate_data():
    with pytest.raises(SystemExit):
        mld.LocalLLM("https://api.openai.com/v1", "gpt")
    mld.LocalLLM("http://localhost:11434/v1", "qwen3-vl:4b-instruct")
