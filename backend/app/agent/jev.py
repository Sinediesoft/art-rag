"""TypeSafe Jev（System One 決策模型）：只做分類判斷、不生成文字。

2026-10-03 起用在七段權限控管的四個地方（docs/adr/015）：
- 第 2 段 Jev Choice：使用者這句話是正常查詢、Prompt 注入還是無關閒聊
- 第 4 段 Jev Noul：每個公開段落 ① is_relevant ② security_leak_check
- 第 5 段 Jev Score：通過的段落依幫助程度評分（0～3），取代傳統的 Reranker
- 第 6 段 Jev Noul（生成閘門）：權限內的資料能不能回答、回答是否合規

POST {JEV_BASE_URL}/systemone，Authorization: Bearer <JEV_API_KEY>；一次請求問完所有題目：
choice（選擇題，回 probabilities／choice）、noul（是非題，回 noul＝「是」的機率）、
score（評分題，回 score＝各等級機率加權後的分數）。

送出去的只有遮蔽個資、代號化後的文字（[圖紙A]、[客戶1]、[電話1]）與題目說明；
圖片、原始名稱、內部與機密段落、資料庫內容都不送。金鑰沒填或呼叫失敗時由呼叫端改用地端規則。

laya測試（開發/laya測試-開發日誌.md）：叫不到 Jev（沒金鑰、停用、斷網、逾時、HTTP 錯誤、
回應無法解析）時改問本機 Unsloth Studio 的 Laya
（`POST {LAYA_BASE_URL}/systemone`，格式和 Jev 相同）。
cloud=False 只問 Laya、不送雲端：內部、機密段落與評估的 engine=local 用。
兩個都叫不到才丟 JevUnavailable。
Laya 不出本機，回覆的 bytes 記 0（外送量只算雲端）。
"""

import json
import math
import time
from dataclasses import dataclass, field

import httpx

from app.core.config import get_settings

# 測試用：注入 httpx.MockTransport，不連真的 API
TRANSPORT: httpx.AsyncBaseTransport | None = None


class JevUnavailable(Exception):
    """沒金鑰、停用、逾時或 API 錯誤：改用地端規則，message 會顯示在處理過程上。"""


@dataclass
class JevReply:
    model: str
    latency_ms: int
    # 送出本機的位元組數（請求本文；Laya 在本機，記 0）
    bytes: int
    request: dict
    answers: dict = field(default_factory=dict)
    # 誰回答的：jev（雲端）／laya（本機）
    engine: str = "jev"
    # 改問 Laya 時，為什麼沒用 Jev（給處理過程看）
    note: str | None = None

    @property
    def who(self) -> str:
        return "Laya" if self.engine == "laya" else "Jev"

    def _answer(self, name: str) -> dict:
        a = self.answers.get(name)
        if not isinstance(a, dict):
            raise JevUnavailable(f"Jev 回應缺少 {name}")
        return a

    def noul(self, name: str) -> float:
        """是非題：回答「是」的機率。缺欄、不是數字、NaN、無限大或不在 0～1 → JevUnavailable
        （呼叫端改用地端判斷；不可以把缺欄當成 0 而放行，docs/adr/030）。"""
        a = self._answer(name)
        if "noul" not in a:
            raise JevUnavailable(f"Jev 回應缺少 {name}.noul")
        return number(a["noul"], f"{name}.noul", 1.0)

    def score(self, name: str, top: float = 3.0) -> float:
        """評分題：各等級機率加權後的分數（0＝最低一級，最高 top）。檢查同 noul。"""
        a = self._answer(name)
        if a.get("score") is None:
            raise JevUnavailable(f"Jev 回應缺少 {name}.score")
        return number(a["score"], f"{name}.score", top)

    def choice(self, name: str, keys: list[str]) -> tuple[str, float, dict[str, float]]:
        """選擇題：(機率最高的選項, 它的機率, 各選項機率)。機率檢查同 noul；
        只有 choice 時要是 keys 之一。"""
        a = self._answer(name)
        raw = a.get("probabilities")
        if raw is None:
            pick = a.get("choice")
            if pick not in keys:
                raise JevUnavailable(f"Jev 回應缺少 {name}")
            raw = {pick: 1.0}
        if not isinstance(raw, dict):
            raise JevUnavailable(f"Jev 回應的 {name}.probabilities 格式不對")
        probs = {k: number(raw.get(k, 0.0), f"{name}.{k}", 1.0) for k in keys}
        if not any(probs.values()):
            raise JevUnavailable(f"Jev 回應缺少 {name}")
        best = max(probs.items(), key=lambda kv: kv[1])
        return best[0], best[1], probs


def number(value, name: str, top: float) -> float:
    """Jev 回來的數字只收 0～top 的有限數；布林、字串、NaN、無限大、超出範圍都當服務異常。"""
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise JevUnavailable(f"Jev 回應的 {name} 不是數字")
    x = float(value)
    if not math.isfinite(x) or not 0.0 <= x <= top:
        raise JevUnavailable(f"Jev 回應的 {name} 超出範圍（{value!r}）")
    return x


def jev_status() -> tuple[bool, str]:
    """雲端 Jev 能不能叫（只看設定，不連線）。"""
    s = get_settings()
    if not s.jev_enabled:
        return False, "Jev 已停用（JEV_ENABLED=false）"
    if not s.jev_api_key:
        return False, "Jev 未設定金鑰（.env 的 JEV_API_KEY 留空）"
    return True, f"{s.jev_model}（{s.jev_base_url}）"


def laya_status() -> tuple[bool, str]:
    """本機 Laya 能不能叫（只看設定，不連線；Unsloth Studio 沒開時要等到請求才知道）。"""
    s = get_settings()
    if not s.laya_enabled:
        return False, "Laya 已停用（LAYA_ENABLED=false）"
    if not s.laya_api_key:
        return False, "Laya 未設定金鑰（.env 的 LAYA_API_KEY 留空）"
    return True, f"Laya {s.laya_model}（{s.laya_base_url}）"


def status(cloud: bool = True) -> tuple[bool, str]:
    """Jev 或 Laya 有一個能叫就算可以。cloud=False 只看 Laya。
    兩個都不行時第 2、4～6 段一律擋下或降級（地端規則已註解，laya測試）。"""
    j_ok, j_detail = jev_status() if cloud else (False, "只用本機")
    if j_ok:
        return True, j_detail
    l_ok, l_detail = laya_status()
    if l_ok:
        return True, f"{l_detail}（{j_detail}）" if cloud else l_detail
    return False, f"{j_detail}；{l_detail}；地端規則已註解，第 2、4～6 段擋下或降級"


async def _post(
    who: str, base_url: str, model: str, key: str, timeout: float, state: dict, questions: dict
) -> JevReply:
    body = {"model": model, "state": state, "questions": questions}
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    url = base_url.rstrip("/") + "/systemone"
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    t0 = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=timeout, transport=TRANSPORT) as client:
            r = await client.post(url, content=payload, headers=headers)
    except httpx.TimeoutException as e:
        raise JevUnavailable(f"{who} 逾時（>{timeout:g} 秒）") from e
    except httpx.HTTPError as e:
        raise JevUnavailable(f"連不上 {who}：{type(e).__name__}") from e
    if r.status_code != 200:
        reason = {
            401: "金鑰無效（401）",
            422: "請求格式不符（422）",
            429: "超過速率限制（429）",
            529: "服務忙碌（529）",
        }
        raise JevUnavailable(
            f"{who} 回應錯誤：{reason.get(r.status_code, f'HTTP {r.status_code}')}"
        )
    try:
        data = r.json()
        if not isinstance(data, dict):
            raise ValueError("回應不是物件")
        answers = data.get("answers")
        if not isinstance(answers, dict) or not answers:
            raise ValueError("缺少 answers")
    except (ValueError, KeyError, TypeError) as e:
        raise JevUnavailable(f"{who} 回應無法解析：{e}") from e
    laya = who == "Laya"
    return JevReply(
        model=str(data.get("model") or model),
        latency_ms=round((time.perf_counter() - t0) * 1000),
        bytes=0 if laya else len(payload),
        request=body,
        answers=answers,
        engine="laya" if laya else "jev",
    )


async def ask(
    state: dict, questions: dict, timeout_s: float | None = None, cloud: bool = True
) -> JevReply:
    """一次請求問完 questions；state 是給 Jev 看的情境（送雲端時只能放代號化文字）。
    timeout_s 留空＝.env 的 JEV_TIMEOUT_S（第 4 段一次問多段，呼叫端會給長一點）。
    cloud=True 先問 Jev，叫不到再問 Laya；cloud=False 只問 Laya。兩個都叫不到 → JevUnavailable。"""
    s = get_settings()
    why: list[str] = []
    if cloud:
        ok, detail = jev_status()
        if ok:
            try:
                return await _post(
                    "Jev",
                    s.jev_base_url,
                    s.jev_model,
                    s.jev_api_key,
                    timeout_s or s.jev_timeout_s,
                    state,
                    questions,
                )
            except JevUnavailable as e:
                why.append(str(e))
        else:
            why.append(detail)
    ok, detail = laya_status()
    if not ok:
        raise JevUnavailable("；".join([*why, detail]))
    try:
        reply = await _post(
            "Laya",
            s.laya_base_url,
            s.laya_model,
            s.laya_api_key,
            max(timeout_s or 0.0, s.laya_timeout_s),
            state,
            questions,
        )
    except JevUnavailable as e:
        raise JevUnavailable("；".join([*why, str(e)])) from e
    reply.note = ("；".join(why) + " → 改問本機 Laya") if why else "只送本機 Laya（不出廠的內容）"
    return reply
