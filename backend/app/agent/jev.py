"""TypeSafe Jev（System One 決策模型）：只做分類判斷、不生成文字。

POST {JEV_BASE_URL}/systemone，Authorization: Bearer <JEV_API_KEY>；一次請求問完所有題目：
- intent（choice）：使用者想做哪件事
- modify_op（choice）：如果要修改資料，是哪一種操作
- wants_numbers／refers_to_current／overrides_rules（noul，是非題）

送出去的只有本機代號化後的文字（[圖紙A]、[客戶1]）與題目說明；圖片、原始名稱、資料庫內容都不送。
回應：answers.<題目>.choice／probabilities／noul／confidence。金鑰沒填或呼叫失敗時由呼叫端改走本地路由。
"""

import json
import time

import httpx

from app.agent.types import System1Result, normalize
from app.core.config import get_agent_config, get_settings

# 測試用：注入 httpx.MockTransport，不連真的 API
TRANSPORT: httpx.AsyncBaseTransport | None = None

STATE_CONTEXT = (
    "這是一套地端的畫作導覽＋工廠機械加工圖助理：可以用文字找畫、問畫作，查工廠圖紙與製程文件，"
    "查庫存／訂單／工單數字，把圖紙轉成 3D，執行生產排程，或修改庫存、訂單、工單。"
    "使用者訊息裡的 [圖紙A]、[客戶1]、[倉庫1] 等是代號，代表真實名稱。"
)


class JevUnavailable(Exception):
    """沒金鑰、停用、逾時或 API 錯誤：改走本地路由，message 會顯示在路由卡上。"""


def status() -> tuple[bool, str]:
    s = get_settings()
    if not s.jev_enabled:
        return False, "已停用（JEV_ENABLED=false），一律走本地路由"
    if not s.jev_api_key:
        return False, "未設定金鑰（.env 的 JEV_API_KEY 留空），一律走本地路由"
    return True, f"{s.jev_model}（{s.jev_base_url}）"


def build_request(masked_text: str, photo_kind: str | None) -> dict:
    cfg = get_agent_config()
    s = get_settings()
    state: dict = {"context": STATE_CONTEXT, "user_message": masked_text}
    if photo_kind:
        # 照片本身不送：只告訴 Jev 本機辨識出照片是畫作還是圖紙
        state["attached_photo"] = {"art": "畫作照片", "drawing": "工廠圖紙照片"}.get(
            photo_kind, "無法辨識的照片"
        )
    return {
        "model": s.jev_model,
        "state": state,
        "questions": {
            "intent": {
                "type": "choice",
                "instructions": "使用者這句話想要系統做哪一件事？選最符合的一項。",
                "criteria": {k: v["jev"] for k, v in cfg["intents"].items()},
            },
            "modify_op": {
                "type": "choice",
                "instructions": "如果使用者要修改資料，是哪一種操作？不是修改資料就選 none。",
                "criteria": {
                    "none": "不是修改資料（只是查詢或其他）",
                    **{k: v["jev"] for k, v in cfg["modify_ops"].items()},
                },
            },
            **{
                name: {"type": "noul", "instructions": f["jev"]} for name, f in cfg["flags"].items()
            },
        },
    }


def parse_response(data: dict) -> System1Result:
    cfg = get_agent_config()
    answers = data.get("answers") or {}
    intent = answers.get("intent") or {}
    if not intent.get("probabilities") and not intent.get("choice"):
        raise JevUnavailable("Jev 回應缺少 intent")
    keys = list(cfg["intents"])
    raw = intent.get("probabilities") or {intent["choice"]: 1.0}
    op = answers.get("modify_op") or {}
    op_keys = ["none", *cfg["modify_ops"]]
    op_raw = op.get("probabilities") or ({op["choice"]: 1.0} if op.get("choice") else {})
    op_raw = {k: v for k, v in op_raw.items() if k in op_keys}
    flag_probs = {
        name: float((answers.get(name) or {}).get("noul") or 0.0) for name in cfg["flags"]
    }
    return System1Result(
        engine="jev",
        intent_probs=normalize(raw, keys),
        op_probs=normalize(op_raw, op_keys) if op_raw else {},
        flags={k: v >= 0.5 for k, v in flag_probs.items()},
        flag_probs=flag_probs,
        model=data.get("model") or get_settings().jev_model,
        jev_confidence=intent.get("confidence"),
        detail={"usage": data.get("usage") or {}},
    )


async def classify(masked_text: str, photo_kind: str | None = None) -> System1Result:
    ok, detail = status()
    if not ok:
        raise JevUnavailable(detail)
    s = get_settings()
    body = build_request(masked_text, photo_kind)
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    url = s.jev_base_url.rstrip("/") + "/systemone"
    headers = {"Authorization": f"Bearer {s.jev_api_key}", "Content-Type": "application/json"}
    t0 = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=s.jev_timeout_s, transport=TRANSPORT) as client:
            r = await client.post(url, content=payload, headers=headers)
    except httpx.TimeoutException as e:
        raise JevUnavailable(f"Jev 逾時（>{s.jev_timeout_s:g} 秒）") from e
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
        result = parse_response(r.json())
    except (ValueError, KeyError, TypeError) as e:
        raise JevUnavailable(f"Jev 回應無法解析：{e}") from e
    result.latency_ms = round((time.perf_counter() - t0) * 1000)
    result.egress_bytes = len(payload)
    result.detail["request"] = body
    return result
