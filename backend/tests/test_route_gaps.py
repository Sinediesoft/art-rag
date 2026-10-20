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
    assert r["intent_label"] == "切換模組"  # 不是借用的「超出範圍」
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


def _jev_says_chitchat(monkeypatch):
    from app.agent import guard

    async def chitchat(*a, **kw):
        return guard.GuardResult(passed=True, engine="jev", verdict="chitchat")

    monkeypatch.setattr(guard, "guard_input", chitchat)


def test_confident_task_is_not_short_circuited_as_chitchat(client, no_jev, monkeypatch):
    """首頁範例「水邊撐陽傘的人群」：本地分流確定是以文搜畫（1.00），Jev 把沒有問號的短句判成閒聊，
    原本短路回「不在我的範圍」。本地分流直接執行的功能照功能走"""
    as_account(client, "guest")
    _fixed_router(monkeypatch, "art_search", 0.99)
    _jev_says_chitchat(monkeypatch)
    r = route(client, "水邊撐陽傘的人群")
    assert r["outcome"] == "pass" and r["intent"] == "art_search" and r["short_circuit"] is None
    assert r["guard"]["verdict"] == "query" and "照功能執行" in r["guard"]["reason"]


def test_real_chitchat_is_still_short_circuited(client, no_jev, monkeypatch):
    """本地分流也不確定、或判成超出範圍時，Jev 的閒聊照舊短路"""
    as_account(client, "guest")
    _fixed_router(monkeypatch, "out_of_scope", 0.9)
    _jev_says_chitchat(monkeypatch)
    assert route(client, "今天天氣如何？")["short_circuit"]["kind"] == "chitchat"
    _fixed_router(monkeypatch, "art_search", 0.4)  # 信心不夠：要使用者選，不是 direct
    _jev_says_chitchat(monkeypatch)
    assert route(client, "嗯嗯")["short_circuit"]["kind"] == "chitchat"


@pytest.mark.parametrize("question", ["目前的生產排程", "看一下這週排了哪些工單"])
def test_viewing_the_schedule_is_a_query(client, no_jev, monkeypatch, question):
    """工作台建議「目前的生產排程」：分流判成執行排程，倉管（不能執行排程）被擋；看排程改成查詢"""
    as_account(client, "warehouse")
    _fixed_router(monkeypatch, "schedule", 0.8)
    r = route(client, question)
    assert r["intent"] == "data_query" and r["gate"] == "direct" and r["outcome"] == "pass"


@pytest.mark.parametrize("question", ["重新排程", "幫我排一下下週的生產"])
def test_running_the_schedule_still_needs_confirmation(client, no_jev, monkeypatch, question):
    as_account(client, "manager")
    _fixed_router(monkeypatch, "schedule", 0.95)
    r = route(client, question)
    assert r["intent"] == "schedule" and r["gate"] == "confirm"


@pytest.mark.parametrize(
    "question, path",
    [("打開待核准清單", "/approvals"), ("打開庫存頁", "/inventory"), ("排程頁", "/schedule")],
)
def test_open_a_page(client, no_jev, question, path):
    """「打開待核准清單」原本被分成排程（信心 0.26）、跑出不相干的選項：給打開那一頁的按鈕"""
    as_account(client, "manager")
    r = route(client, question)
    sc = r["short_circuit"]
    assert r["outcome"] == "short_circuit" and sc["kind"] == "navigate"
    assert sc["page"]["path"] == path and sc["module"] is None and r["intent_label"] == "切換模組"
