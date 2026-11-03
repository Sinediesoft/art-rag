"""問題前提檢查（docs/adr/037 補）。

解析判斷、沒有段落不檢查、失敗不擋回答、說錯了時更正提醒接在參考資料後面。
"""

import asyncio
import json

from app.rag import conflict_check, premise_check
from app.rag.premise_check import check, note, parse_verdict
from app.services import chat_service

SOURCES = [
    {"ref": 1, "title": "谿山行旅圖", "topic": "落款與發現", "text": "1958 年李霖燦發現款。"},
]


def test_parse_verdict():
    ok = '{"mismatch": true, "claim": "董其昌", "correct": "李霖燦", "ref": 1}'
    assert parse_verdict(ok, {1}) == {
        "mismatch": True,
        "claim": "董其昌",
        "correct": "李霖燦",
        "ref": 1,
    }
    assert parse_verdict('好的：{"mismatch": false}', {1}) == {"mismatch": False}
    # 作品資料寫 0
    assert parse_verdict(ok.replace('"ref": 1', '"ref": 0'), {1})["ref"] == 0
    # 不存在的段落、沒寫清楚哪裡錯、格式不對都當成看不懂
    assert parse_verdict(ok.replace('"ref": 1', '"ref": 5'), {1}) is None
    assert (
        parse_verdict('{"mismatch": true, "claim": "", "correct": "李霖燦", "ref": 1}', {1}) is None
    )
    assert parse_verdict('{"mismatch": "yes"}', {1}) is None
    assert parse_verdict("說錯了", {1}) is None


def test_note_points_to_ref_and_card():
    info = {"claim": "董其昌", "correct": "李霖燦", "ref": 1}
    assert note(info).startswith("（系統比對：問題裡「董其昌」和參考資料 [1] 不符")
    assert "（作品資料）" in note(info | {"ref": 0})
    assert "不要回答「知識庫中沒有這方面的資料」" in note(info)


def test_skips_without_sources_and_mock():
    assert asyncio.run(check("q", [])) is None
    assert asyncio.run(check("q", SOURCES, strategy="mock")) is None


def test_mismatch_found(monkeypatch):
    async def judge(question, sources, card):
        return '{"mismatch": true, "claim": "董其昌", "correct": "李霖燦", "ref": 1}'

    monkeypatch.setattr(premise_check, "_judge", judge)
    info = asyncio.run(check("董其昌怎麼找到簽名？", SOURCES))
    assert info["mismatch"] is True and info["correct"] == "李霖燦" and info["fallback"] is None


def test_unreadable_output_falls_back(monkeypatch):
    async def judge(question, sources, card):
        return "問題沒問題"

    monkeypatch.setattr(premise_check, "_judge", judge)
    info = asyncio.run(check("誰發現簽名？", SOURCES))
    assert info["mismatch"] is False and info["fallback"].startswith("模型輸出看不懂")


def test_timeout_falls_back(monkeypatch):
    async def judge(question, sources, card):
        await asyncio.sleep(1)

    monkeypatch.setattr(premise_check, "_judge", judge)
    monkeypatch.setattr(premise_check.get_models_config().premise_check, "timeout_s", 0.01)
    info = asyncio.run(check("誰發現簽名？", SOURCES))
    assert info["mismatch"] is False and info["fallback"].startswith("檢查逾時")


def test_chat_adds_note_when_premise_wrong(monkeypatch, all_chunks):
    """請求開 premise_check：sources 事件帶檢查結果，送給模型的參考資料後面多一句更正提醒；
    參考資料已經互相矛盾時不做前提檢查，只留矛盾提醒。"""

    async def judge(question, sources, card):
        assert "谿山行旅圖" in card  # 作品資料一起送去比對
        return json.dumps(
            {"mismatch": True, "claim": "董其昌", "correct": "李霖燦", "ref": sources[0]["ref"]}
        )

    async def conflict(question, sources):
        return json.dumps({"conflict": True, "refs": [s["ref"] for s in sources[:2]]})

    monkeypatch.setattr(premise_check, "_judge", judge)
    monkeypatch.setattr(conflict_check, "_judge", conflict)
    seen = {}
    real = chat_service.build_messages

    def spy(*args, **kw):
        seen["note"] = kw.get("note")
        return real(*args, **kw)

    monkeypatch.setattr(chat_service, "build_messages", spy)

    # 問句和矛盾檢查的測試相同，注入的段落才留得下來（湊滿 2 段）；前提檢查的判斷由上面的 judge 固定
    async def run(flag, conflict_flag=False):
        events = [
            e
            async for e in chat_service.chat_stream(
                "1962 年是誰發現了作者的簽名？",
                f"req_pc_{flag}_{conflict_flag}",
                artwork_id="npm-000001",
                rearrange=False,
                inject=[
                    {"kind": "counterfactual", "text": "1962 年張大千發現款。", "topic": "落款"}
                ],
                conflict_check=conflict_flag,
                premise_check=flag,
            )
        ]
        sources = next(e for e in events if e.startswith("event: sources"))
        return json.loads(sources.split("data: ", 1)[1])

    sources = asyncio.run(run(True))
    assert sources["premise_check"]["mismatch"] is True
    assert seen["note"].startswith("（系統比對：問題裡「董其昌」")

    both = asyncio.run(run(True, conflict_flag=True))
    assert len(both["sources"]) >= 2, both["sources"]
    assert both["premise_check"] is None
    assert "說法不一致" in seen["note"] and "問題裡" not in seen["note"]

    sources = asyncio.run(run(False))
    assert sources["premise_check"] is None and seen["note"] is None
