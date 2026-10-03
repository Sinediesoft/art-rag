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
"""

import json
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
    # 送出本機的位元組數（請求本文）
    bytes: int
    request: dict
    answers: dict = field(default_factory=dict)

    def noul(self, name: str) -> float:
        """是非題：回答「是」的機率。"""
        return float((self.answers.get(name) or {}).get("noul") or 0.0)

    def score(self, name: str) -> float:
        """評分題：各等級機率加權後的分數（0＝最低一級）。"""
        a = self.answers.get(name) or {}
        if a.get("score") is None:
            raise JevUnavailable(f"Jev 回應缺少 {name}")
        return float(a["score"])

    def choice(self, name: str, keys: list[str]) -> tuple[str, float, dict[str, float]]:
        """選擇題：(機率最高的選項, 它的機率, 各選項機率)。"""
        a = self.answers.get(name) or {}
        probs = a.get("probabilities") or ({a["choice"]: 1.0} if a.get("choice") else {})
        probs = {k: float(probs.get(k, 0.0)) for k in keys}
        if not any(probs.values()):
            raise JevUnavailable(f"Jev 回應缺少 {name}")
        best = max(probs.items(), key=lambda kv: kv[1])
        return best[0], best[1], probs


def status() -> tuple[bool, str]:
    s = get_settings()
    if not s.jev_enabled:
        return False, "已停用（JEV_ENABLED=false），第 2、4～6 段改用地端規則"
    if not s.jev_api_key:
        return False, "未設定金鑰（.env 的 JEV_API_KEY 留空），第 2、4～6 段改用地端規則"
    return True, f"{s.jev_model}（{s.jev_base_url}）"


async def ask(state: dict, questions: dict, timeout_s: float | None = None) -> JevReply:
    """一次請求問完 questions；state 是給 Jev 看的情境（只能放代號化文字）。
    timeout_s 留空＝.env 的 JEV_TIMEOUT_S（第 4 段一次問多段，呼叫端會給長一點）。"""
    ok, detail = status()
    if not ok:
        raise JevUnavailable(detail)
    s = get_settings()
    body = {"model": s.jev_model, "state": state, "questions": questions}
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    url = s.jev_base_url.rstrip("/") + "/systemone"
    headers = {"Authorization": f"Bearer {s.jev_api_key}", "Content-Type": "application/json"}
    timeout = timeout_s or s.jev_timeout_s
    t0 = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=timeout, transport=TRANSPORT) as client:
            r = await client.post(url, content=payload, headers=headers)
    except httpx.TimeoutException as e:
        raise JevUnavailable(f"Jev 逾時（>{timeout:g} 秒）") from e
    except httpx.HTTPError as e:
        raise JevUnavailable(f"連不上 Jev：{type(e).__name__}") from e
    if r.status_code != 200:
        reason = {
            401: "金鑰無效（401）",
            422: "請求格式不符（422）",
            429: "超過速率限制（429）",
            529: "服務忙碌（529）",
        }
        raise JevUnavailable(f"Jev 回應錯誤：{reason.get(r.status_code, f'HTTP {r.status_code}')}")
    try:
        data = r.json()
        answers = data.get("answers")
        if not isinstance(answers, dict) or not answers:
            raise ValueError("缺少 answers")
    except (ValueError, KeyError, TypeError) as e:
        raise JevUnavailable(f"Jev 回應無法解析：{e}") from e
    return JevReply(
        model=data.get("model") or s.jev_model,
        latency_ms=round((time.perf_counter() - t0) * 1000),
        bytes=len(payload),
        request=body,
        answers=answers,
    )
