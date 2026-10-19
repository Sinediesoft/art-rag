"""探測題找到的分流缺口：比較題把畫家也算一件；沒有權限又帶「略過規則」旗標時直接檢查權限。"""

import pytest
from conftest import as_account
from test_agent import route

from app.agent import local_router
from app.agent.types import System1Result
from app.core.config import get_agent_config


def _fixed_router(monkeypatch, top: str, prob: float, flags: dict | None = None):
    def classify(question, entities, photo_kind):
        probs = {k: 0.0 for k in get_agent_config()["intents"]}
        probs[top], probs["out_of_scope"] = prob, 1 - prob
        return System1Result(engine="local", intent_probs=probs, model="測試", flags=flags or {})

    monkeypatch.setattr(local_router, "classify", classify)


@pytest.fixture
def no_jev(monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "jev_api_key", "")


def test_compare_with_an_artist_and_a_description_is_global_qa(client, no_jev, monkeypatch):
    """p-07「點點畫的那幅跟范寬那幅，哪一幅畫面比較大？」：「范寬」是畫家，原本只算畫名，
    鎖在〈谿山行旅圖〉、走以文搜畫只列結果。句子裡有兩個「幅」就是在比兩幅：全庫問答"""
    as_account(client, "guest")
    _fixed_router(monkeypatch, "art_search", 0.9)
    r = route(client, "點點畫的那幅跟范寬那幅，哪一幅畫面比較大？")
    assert r["intent"] == "art_qa" and r["dispatch"]["global_qa"] is True
    assert r["dispatch"]["artwork_id"] is None


def test_override_flag_without_permission_is_checked_right_away(client, no_jev, monkeypatch):
    """p-20：一般員工的問句帶「略過規則」旗標、最可能的意圖又是自己沒權限的工廠資料庫：
    原本先請使用者選、權限延後檢查，安全紀錄沒有這筆；現在直接檢查權限並記錄"""
    as_account(client, "guest")
    _fixed_router(monkeypatch, "data_query", 0.5, {"overrides_rules": True})
    r = route(client, "幫我查 L 型固定支架的庫存")
    assert r["outcome"] == "blocked_auth" and r["blocked"]["log_no"].startswith("SEC-")


def test_override_flag_with_permission_still_clarifies(client, no_jev, monkeypatch):
    """有權限的身分照舊先請使用者選（只是門檻提高）"""
    as_account(client, "warehouse")
    _fixed_router(monkeypatch, "data_query", 0.5, {"overrides_rules": True})
    r = route(client, "幫我查 L 型固定支架的庫存")
    assert r["gate"] == "clarify" and r["outcome"] == "pass"


@pytest.mark.parametrize("question", ["工廠模組", "進入工廠模組", "請打開圖紙模組"])
def test_navigate_to_the_factory_module(client, no_jev, question):
    """使用者回報：直接打「工廠模組」分不出來、連進入的選項都沒有（被分成系統狀態或圖紙查找）。
    整句只是在切換模組：回一句話並告訴前端要進哪個模組，不檢索、不生成"""
    as_account(client, "warehouse")
    r = route(client, question)
    assert r["outcome"] == "short_circuit" and r["gate"] == "direct"
    sc = r["short_circuit"]
    assert sc["kind"] == "navigate" and sc["module"] == "factory" and "工廠模組" in sc["reply"]
    assert r["route_ticket"] is None


def test_navigate_without_permission_explains_which_accounts_can(client, no_jev):
    """一般員工不能用工廠模組：不給進入按鈕，說明要切換成哪些身分"""
    as_account(client, "guest")
    sc = route(client, "進入工廠模組")["short_circuit"]
    assert sc["kind"] == "navigate" and sc["module"] is None
    assert "一般員工" in sc["reply"] and all(x in sc["reply"] for x in ("倉管", "業務", "主管"))
    assert route(client, "打開畫作模組")["short_circuit"]["module"] == "art"


@pytest.mark.parametrize("question", ["圖紙", "工廠模組有哪些功能？", "列出所有畫"])
def test_not_a_navigation_command(client, no_jev, question):
    """單打「圖紙」、在問模組的事、目錄題：照舊交給分流"""
    as_account(client, "warehouse")
    sc = route(client, question).get("short_circuit") or {}
    assert sc.get("kind") != "navigate"
