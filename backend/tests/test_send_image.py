"""問答要不要附圖（Issue #2、docs/adr/024）。conftest 已用 mock embedding 建好索引、LLM 走 mock。

預設和原本相同（送圖）；設定不送時，只有「已辨識或已指定、檢索開著」的問答不送，關檢索的對照組照樣送。
"""

import json

import pytest

from app.core.config import get_models_config, get_settings
from app.services import chat_service


def parse_sse(text: str) -> list[tuple[str, dict]]:
    events = []
    for block in text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        events.append((lines["event"], json.loads(lines["data"])))
    return events


@pytest.fixture
def sent(monkeypatch):
    """記下每次組 prompt 時拿到的圖（bytes 或 None）。"""
    images: list[bytes | None] = []
    real = chat_service.build_messages

    def spy(question, artwork, sources, image_jpeg, *args, **kwargs):
        images.append(image_jpeg)
        return real(question, artwork, sources, image_jpeg, *args, **kwargs)

    monkeypatch.setattr(chat_service, "build_messages", spy)
    return images


def ask(client, **body) -> dict:
    r = client.post("/api/v1/chat", json={"question": "山石用了什麼皴法？", **body})
    assert r.status_code == 200, r.text
    return next(d for e, d in parse_sse(r.text) if e == "done")


def test_default_sends_image_like_before(client, sent):
    assert get_models_config().chat.send_image is True
    done = ask(client, artwork_id="npm-000001")
    assert isinstance(sent[-1], bytes) and done["image_sent"] is True


def test_request_can_skip_image_when_identified_and_retrieval_on(client, sent):
    done = ask(client, artwork_id="npm-000001", send_image=False)
    assert sent[-1] is None and done["image_sent"] is False
    assert done["egress"]["images"] == 0


def test_retrieval_off_baseline_still_sends_image(client, sent):
    """檢索增益的對照組靠圖回答，不能拿掉。"""
    done = ask(client, artwork_id="npm-000001", send_image=False, use_retrieval=False)
    assert isinstance(sent[-1], bytes) and done["image_sent"] is True


def test_drawing_question_can_skip_image(client, sent):
    client.post("/api/v1/auth/switch", json={"account_id": "manager"}).raise_for_status()
    done = ask(client, part_id="mfg-002", send_image=False)
    assert sent[-1] is None and done["image_sent"] is False


def test_env_overrides_models_yaml_and_request_overrides_env(client, sent, monkeypatch):
    monkeypatch.setattr(get_settings(), "send_image", "false")
    ask(client, artwork_id="npm-000001")
    assert sent[-1] is None  # .env 關掉
    ask(client, artwork_id="npm-000001", send_image=True)
    assert isinstance(sent[-1], bytes)  # 請求優先


@pytest.mark.parametrize(
    "env, requested, expected",
    [("", None, True), ("false", None, False), ("off", None, False), ("true", None, True),
     ("false", True, True), ("", False, False)],
)  # fmt: skip
def test_send_image_enabled_priority(monkeypatch, env, requested, expected):
    monkeypatch.setattr(get_settings(), "send_image", env)
    assert chat_service.send_image_enabled(requested) is expected
