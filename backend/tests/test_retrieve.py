"""檢索段落規則：比較題替被點名的畫作留名額、段落篩選開著時多抓候選（docs/adr/008 2026-10-05）。"""

import pytest
from test_api import parse_sse

from app.core.config import get_models_config
from app.services import chat_service

QUESTION = "這幅畫和〈有絲柏的麥田〉的畫法有什麼不同？"


@pytest.fixture
def no_floor(monkeypatch):
    """mock 向量是雜湊亂數，相似度都在門檻以下；拿掉門檻才看得到名額怎麼分。
    門檻＝max(min_chunk_score, 第一名 × relative_chunk_ratio)：
    第一名是正的時，比例設成很大的負數就不擋。"""
    r = get_models_config().retrieval
    monkeypatch.setitem(r, "min_chunk_score", -1.0)
    monkeypatch.setitem(r, "relative_chunk_ratio", -1e6)


def test_split_slots_reserves_for_fill_and_gives_back_unused():
    own, fill = list("abcdefg"), list("xyz")
    assert chat_service.split_slots(own, fill, 5, 0) == (list("abcde"), [])  # 原本的規則
    assert chat_service.split_slots(own, fill, 5, 2) == (list("abc"), list("xy"))
    assert chat_service.split_slots(own, fill, 10, 5) == (list("abcdefg"), list("xyz"))
    assert chat_service.split_slots(list("ab"), fill, 5, 2) == (list("ab"), list("xyz"))
    assert chat_service.split_slots(own, [], 5, 2) == (list("abcde"), [])
    assert chat_service.split_slots([], fill, 5, 0) == ([], list("xyz"))  # 未指定畫作


def test_named_artworks_finds_other_titles_only():
    assert chat_service.named_artworks(QUESTION, "npm-000001") == {"met-436535"}
    assert chat_service.named_artworks("有絲柏的麥田收藏在哪裡？", "met-436535") == set()
    assert chat_service.named_artworks("畫的背景是什麼？", "npm-000001") == set()


def test_comparison_fills_from_named_artwork(no_floor):
    got = chat_service.retrieve(QUESTION, "npm-000001")
    k = int(get_models_config().retrieval["top_k_chunks"])
    by = [s["artwork_id"] for s in got]
    # 原本自己那幅一定先佔滿 5 段；現在替被點名的畫留一半名額，只從它補
    assert len(got) == k and by.count("met-436535") == k // 2
    assert set(by) == {"npm-000001", "met-436535"} and [s["ref"] for s in got] == [1, 2, 3, 4, 5]


def test_named_artwork_fills_without_comparison_words(no_floor):
    got = chat_service.retrieve("〈有絲柏的麥田〉是誰畫的？", "npm-000001")
    assert "met-436535" in {s["artwork_id"] for s in got}


def test_keyword_only_fill_keeps_own_first(no_floor):
    # 只有「背景」這類字、沒點名其他畫：照舊先取自己那幅（7 段佔滿 5 個名額）
    got = chat_service.retrieve("這幅畫的背景是什麼？", "npm-000001")
    assert {s["artwork_id"] for s in got} == {"npm-000001"}


def test_retrieve_takes_k_candidates(no_floor):
    assert len(chat_service.retrieve("用了什麼技法？", "met-436535", k=10)) == 8  # 麥田全部 8 段
    assert len(chat_service.retrieve("用了什麼技法？", "met-436535")) == 5


def test_rearrange_fallback_trims_extra_candidates(client, no_floor):
    """篩選失敗退回原本的段落時，多抓的 10 段照不篩選時的規則截回 top_k_chunks 段。"""
    body = {"question": QUESTION, "artwork_id": "npm-000001", "rearrange": True}
    src = parse_sse(client.post("/api/v1/chat", json={**body, "strategy": "hybrid"}).text)[0][1]
    info = src["rearrange"]
    k = int(get_models_config().retrieval["top_k_chunks"])
    # mock 生成端的輸出不是「1,3」格式 → 退回；候選是多抓的 10 段（自己那幅 5 段＋麥田 5 段）
    assert info["fallback"] and info["candidates"] == get_models_config().rearrange.max_candidates
    assert info["kept"] == len(src["sources"]) == k
    by = [s["artwork_id"] for s in src["sources"]]
    assert by.count("met-436535") == k // 2  # 和不篩選時一樣，替被點名的畫留一半名額
    assert [s["ref"] for s in src["sources"]] == list(range(1, k + 1))
