"""參考資料矛盾檢查（docs/adr/028）。

解析判斷、不到 2 段不檢查、失敗不擋回答、有矛盾時提醒接在參考資料後面。
"""

import asyncio
import json

from app.rag import conflict_check
from app.rag.conflict_check import NOTE, check, note, parse_verdict
from app.rag.prompt import build_messages
from app.services import chat_service

SOURCES = [
    {"ref": 1, "title": "谿山行旅圖", "topic": "落款與發現", "text": "1962 年張大千發現款。"},
    {"ref": 2, "title": "谿山行旅圖", "topic": "落款與發現", "text": "1958 年李霖燦發現款。"},
]
ARTWORK = {
    "title": {"zh": "谿山行旅圖"},
    "artist": {"zh": "范寬"},
    "date_text": "北宋",
    "medium": "絹本水墨",
    "collection": "國立故宮博物院",
}


def test_parse_verdict():
    assert parse_verdict('{"conflict": true, "refs": [2, 1]}', {1, 2}) == [1, 2]
    assert parse_verdict('好的：{"conflict": false, "refs": []}', {1, 2}) == []
    assert parse_verdict('{"conflict": true, "refs": [1]}', {1, 2}) is None  # 矛盾至少兩段
    assert parse_verdict('{"conflict": true, "refs": [1, 5]}', {1, 2}) is None  # 不存在的段落
    assert parse_verdict('{"conflict": "yes"}', {1, 2}) is None
    assert parse_verdict("有矛盾", {1, 2}) is None


def test_note_lists_refs():
    assert note([1, 3]).startswith("（系統比對：參考資料 [1]、[3] 對這個問題的說法不一致")
    assert "參考資料的說法不一致" in NOTE


def test_skips_single_source_and_mock():
    assert asyncio.run(check("q", SOURCES[:1])) is None
    assert asyncio.run(check("q", SOURCES, strategy="mock")) is None


def test_conflict_found(monkeypatch):
    async def judge(question, sources):
        return '{"conflict": true, "refs": [1, 2]}'

    monkeypatch.setattr(conflict_check, "_judge", judge)
    info = asyncio.run(check("誰發現簽名？", SOURCES))
    assert info["conflict"] is True and info["refs"] == [1, 2] and info["fallback"] is None


def test_unreadable_output_falls_back(monkeypatch):
    async def judge(question, sources):
        return "我覺得兩段都對"

    monkeypatch.setattr(conflict_check, "_judge", judge)
    info = asyncio.run(check("誰發現簽名？", SOURCES))
    assert info["conflict"] is False and info["fallback"].startswith("模型輸出看不懂")


def test_note_goes_after_context_only_with_retrieval():
    text = build_messages("誰發現簽名？", ARTWORK, SOURCES, None, note=note([1, 2]))[-1]["content"]
    user = text[-1]["text"]
    assert user.index("1958 年李霖燦發現款") < user.index("系統比對") < user.index("誰發現簽名？")
    no_rag = build_messages("誰發現簽名？", ARTWORK, SOURCES, None, False, note=note([1, 2]))
    assert "系統比對" not in no_rag[-1]["content"][-1]["text"]


def test_chat_adds_note_when_conflict_found(monkeypatch):
    """請求開 conflict_check：sources 事件帶檢查結果，送給模型的參考資料後面多一句提醒。"""

    async def judge(question, sources):
        return json.dumps({"conflict": True, "refs": [s["ref"] for s in sources[:2]]})

    monkeypatch.setattr(conflict_check, "_judge", judge)
    seen = {}
    real = chat_service.build_messages

    def spy(*args, **kw):
        seen["note"] = kw.get("note")
        return real(*args, **kw)

    monkeypatch.setattr(chat_service, "build_messages", spy)

    async def run(flag):
        # 關掉段落篩選（mock 模型的篩選結果不固定）；測試用的 mock embedding 只檢索得到 1 段，
        # 注入一段手寫段落湊滿 2 段（直接呼叫 chat_stream 不經過 EVAL_INJECTION 檢查）
        events = [
            e
            async for e in chat_service.chat_stream(
                "是誰發現了作者的簽名？",
                f"req_cc_{flag}",
                artwork_id="npm-000001",
                rearrange=False,
                inject=[{"kind": "counterfactual", "text": SOURCES[0]["text"], "topic": "落款"}],
                conflict_check=flag,
            )
        ]
        sources = next(e for e in events if e.startswith("event: sources"))
        return json.loads(sources.split("data: ", 1)[1])

    sources = asyncio.run(run(True))
    assert len(sources["sources"]) >= 2
    assert sources["conflict_check"]["conflict"] is True
    assert seen["note"] and seen["note"].startswith("（系統比對")

    sources = asyncio.run(run(False))
    assert sources["conflict_check"] is None and seen["note"] is None
