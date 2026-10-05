"""干擾段落注入（評估專用，docs/adr/019）。conftest 已用 mock embedding 建好索引、LLM 走 mock。"""

import json

import pytest

from app.core.config import get_settings
from app.services.chat_service import INJECTED_LABEL

FAKE = "這幅畫的作者簽名是在 1962 年由張大千在畫面左上角發現的。"


def parse_sse(text: str) -> list[tuple[str, dict]]:
    events = []
    for block in text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        events.append((lines["event"], json.loads(lines["data"])))
    return events


def sources_of(client, body: dict) -> list[dict]:
    r = client.post("/api/v1/chat", json=body)
    assert r.status_code == 200, r.text
    return next(d for e, d in parse_sse(r.text) if e == "sources")["sources"]


@pytest.fixture
def injection_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "eval_injection", True)


def test_injection_forbidden_by_default(client):
    """正式服務不能讓呼叫端把任意文字塞進 prompt：預設 EVAL_INJECTION=false → 403。"""
    assert get_settings().eval_injection is False
    body = {
        "question": "簽名藏在哪裡？",
        "artwork_id": "npm-000001",
        "inject": [{"kind": "counterfactual", "text": FAKE}],
    }
    r = client.post("/api/v1/chat", json=body)
    assert r.status_code == 403 and r.json()["error"]["code"] == "FORBIDDEN"


def test_counterfactual_goes_first_under_same_artwork(client, injection_on):
    base = {"question": "簽名藏在哪裡？", "artwork_id": "npm-000001"}
    real = sources_of(client, base)
    got = sources_of(
        client, base | {"inject": [{"kind": "counterfactual", "text": FAKE, "topic": "落款與發現"}]}
    )
    first = got[0]
    assert first["injected"] is True and first["injected_kind"] == "counterfactual"
    assert first["chunk_id"] == "inject:0" and first["ref"] == 1
    assert first["artwork_id"] == "npm-000001" and first["topic"] == "落款與發現"
    assert first["text"] == FAKE and first["source_label"] == INJECTED_LABEL
    assert isinstance(first["score"], float)
    # 真正的段落接在後面、重新編號
    assert [s["chunk_id"] for s in got[1:]] == [s["chunk_id"] for s in real]
    assert [s["ref"] for s in got] == list(range(1, len(got) + 1))
    assert not any(s.get("injected") for s in got[1:])


def test_position_last(client, injection_on):
    got = sources_of(
        client,
        {
            "question": "簽名藏在哪裡？",
            "artwork_id": "npm-000001",
            "inject": [{"kind": "counterfactual", "text": FAKE, "position": "last"}],
        },
    )
    assert got[-1]["injected"] is True and got[-1]["ref"] == len(got)
    assert not any(s.get("injected") for s in got[:-1])


def test_other_takes_real_chunk_from_another_artwork(client, injection_on):
    got = sources_of(
        client,
        {"question": "簽名藏在哪裡？", "artwork_id": "npm-000001", "inject": [{"kind": "other"}]},
    )
    other = got[0]
    assert other["injected"] is True and other["injected_kind"] == "other"
    assert other["artwork_id"] != "npm-000001"
    assert other["chunk_id"].startswith("inject:0:")
    real_ids = {s["chunk_id"] for s in got[1:]}
    assert other["chunk_id"].removeprefix("inject:0:") not in real_ids


def test_other_on_drawing_comes_from_another_part(client, injection_on):
    got = sources_of(
        client,
        {"question": "公差要求？", "part_id": "mfg-002", "inject": [{"kind": "other"}]},
    )
    assert got[0]["injected"] is True and got[0]["part_id"] != "mfg-002"


def test_counterfactual_needs_text(client, injection_on):
    body = {"question": "q", "artwork_id": "npm-000001", "inject": [{"kind": "counterfactual"}]}
    assert client.post("/api/v1/chat", json=body).status_code == 422


def test_at_most_five_distractors(client, injection_on):
    body = {"question": "q", "artwork_id": "npm-000001", "inject": [{"kind": "other"}] * 6}
    assert client.post("/api/v1/chat", json=body).status_code == 422
