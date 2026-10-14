"""知識庫目錄題（docs/adr/039）：清單題直接列出、比較題附知識庫目錄。"""

import pytest
from test_agent import route

from app.agent import catalog
from app.repositories.index_store import get_store
from app.services import chat_service


@pytest.mark.parametrize(
    "question", ["目前知識庫有哪些畫", "你們有哪些館藏？", "列出所有畫作", "知識庫總共有幾幅畫？"]
)
def test_list_question_is_answered_from_the_knowledge_base(client, question):
    """本地分流常把這類問題分錯（落到批次辨識、先請使用者選）；
    第 2 段放行後直接列出，不呼叫 LLM。"""
    r = route(client, question)
    assert r["outcome"] == "short_circuit" and r["short_circuit"]["kind"] == "catalog"
    assert r["intent"] == "art_qa" and r["gate"] == "direct" and r["options"] == []
    reply = r["short_circuit"]["reply"]
    titles = [a["title"]["zh"] for a in get_store().artworks]
    assert f"共有 {len(titles)} 幅畫" in reply and all(t in reply for t in titles)
    assert r["route_ticket"] is None  # 不交給問答


@pytest.mark.parametrize(
    "question", ["有哪些畫是在戶外寫生完成的？", "梵谷有哪些作品", "哪幅畫最早完成？"]
)
def test_questions_with_conditions_go_to_qa(client, question):
    assert not catalog.is_list_question(question)
    assert route(client, question)["outcome"] != "short_circuit"


def test_chitchat_short_circuit_is_marked(client):
    r = route(client, "明天會下雨嗎？")
    if r["outcome"] == "short_circuit":  # 地端規則判為閒聊時
        assert r["short_circuit"]["kind"] == "chitchat"


def test_compare_question_gets_the_catalog_as_first_source(client):
    """沒指定畫作的比較題：參考資料最前面是系統產生的知識庫目錄，其他段落編號往後移。"""
    sources = chat_service.retrieve("哪幅畫最早完成？", None)
    assert sources[0]["chunk_id"] == catalog.CHUNK_ID and sources[0]["ref"] == 1
    assert [s["ref"] for s in sources] == list(range(1, len(sources) + 1))
    assert all(a["title"]["zh"] in sources[0]["text"] for a in get_store().artworks)
    # 已指定畫作、或一般問題：不附
    assert all(
        s["chunk_id"] != catalog.CHUNK_ID
        for s in chat_service.retrieve("哪幅畫最早完成？", "aic-27992")
    )
    assert all(
        s["chunk_id"] != catalog.CHUNK_ID for s in chat_service.retrieve("這幅畫是誰畫的", None)
    )
