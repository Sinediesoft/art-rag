"""日期核對：回答寫了參考資料、作品資料、問題都沒有的年份或年月，請生成端重答一次；
重答還有就拿掉那幾句（探測題 p-10「1853 年出生」、p-15「〈星夜〉1889 年 6 月」）。"""

import pytest
from tests.test_drawing_retrieval import (
    _Reply,
    client_as,
    parse_sse,
    realistic_retrieval,  # noqa: F401 -- fixture
)

from app.core import config
from app.rag.textproc import NO_DATA, drop_sentences_with, unsupported_dates
from app.services import chat_service

SUPPORT = (
    "〈有絲柏的麥田〉創作年代為1889 年。"
    "1889 年 5 月，梵谷自願住進聖保羅療養院。這幅畫繪於 1889 年 7 月初。"
)


def test_unsupported_dates_reports_only_what_the_sources_lack():
    # p-10：生年是模型自己補的
    assert unsupported_dates("他於1853年出生，1889年時是37歲 [1]。", SUPPORT) == ["1853"]
    # p-15：年份 1889 資料有，「1889 年 6 月」沒有
    ans = "〈有絲柏的麥田〉是 1889 年畫的 [1]，而〈星夜〉是 1889 年 6 月畫的 [2]。"
    assert unsupported_dates(ans, SUPPORT) == ["1889年6月"]
    # 資料寫了的年月、空白寫法不同也算有
    assert unsupported_dates("這幅畫繪於1889年7月初 [2]。", SUPPORT) == []
    # 年齡、尺寸、件數不是年份
    assert unsupported_dates("當時 36 歲，畫面 1200 mm，共 2026 件 [1]。", SUPPORT) == []


def test_question_counts_as_support():
    """錯誤前提題要先引用使用者說的年份再更正，不能當成編造"""
    ans = "這幅畫沒有在 1884 年展出，是 1886 年第八屆印象派畫展首次亮相 [1]。"
    assert unsupported_dates(ans, "問題：1884 年展出時評價如何？\n1886 年第八屆印象派畫展") == []


def test_drop_sentences_with():
    ans = "這幅畫繪於 1889 年 7 月初 [2]。他於 1853 年出生 [1]。當時住在療養院 [2]。"
    assert (
        drop_sentences_with(ans, ["1853"]) == "這幅畫繪於 1889 年 7 月初 [2]。當時住在療養院 [2]。"
    )
    assert drop_sentences_with("他於 1853 年出生 [1]。", ["1853"]) == ""


class _Replies(_Reply):
    """第一次回 replies[0]，重答回 replies[1]"""

    def __init__(self, replies: list[str], calls: list):
        super().__init__(replies[0])
        self.replies, self.calls = replies, calls

    async def stream(self, messages):
        self.calls.append(messages)
        yield self.replies[min(len(self.calls), len(self.replies)) - 1]


GOOD = "此圖紙為立式軸承座 [1]，材料為 FC250 灰口鑄鐵 [1]。"
INVENTED = GOOD + "這張圖紙在 1987 年首次發行 [1]。"


@pytest.mark.parametrize(
    "replies, expected, detail",
    [
        ([GOOD], GOOD, None),  # 沒有資料外的日期：不重答
        ([INVENTED, GOOD], GOOD, "重答一次"),  # 重答後乾淨：用重答
        ([INVENTED, INVENTED], GOOD, "拿掉資料沒寫的"),  # 重答還有：拿掉那句
        (["這張圖紙在 1987 年首次發行 [1]。"] * 2, NO_DATA, "拿掉資料沒寫的"),  # 全拿掉：沒有資料
    ],
)
@pytest.mark.usefixtures("realistic_retrieval")
def test_chat_retries_or_drops_invented_dates(client, monkeypatch, replies, expected, detail):
    calls: list = []
    provider = _Replies(replies, calls)
    monkeypatch.setattr(chat_service, "get_provider", lambda strategy: provider)
    s = config.get_settings()
    monkeypatch.setattr(s, "llm_mode", "real")
    monkeypatch.setattr(s, "premise_check", "false")
    monkeypatch.setattr(s, "conflict_check", "false")
    res = client_as("manager").post(
        "/api/v1/chat", json={"question": "材料是什麼？", "part_id": "mfg-006"}
    )
    events = parse_sse(res.text)
    text = "".join(x["text"] for e, x in events if e == "token")
    done = next(x for e, x in events if e == "done")
    stage7 = next(st for st in done["pipeline"] if st["stage"] == 7)
    assert text == expected and stage7["status"] == "pass"
    assert len(calls) == (1 if detail is None else 2)
    if detail:
        assert detail in stage7["detail"] and "1987" in stage7["detail"]
        # 重答時提醒接在參考資料後面
        assert "參考資料沒有的 「1987」" in calls[1][-1]["content"][-1]["text"]
