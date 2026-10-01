"""信心閘門：依「判斷錯的代價」分流，不是所有動作共用一個門檻。

- clarify：信心不足或前兩名機率差太小 → 顯示澄清按鈕讓使用者點選
- modify：寫入類 → 修改資料流程（權限判定、試算、確認卡、超額送主管核准）
- confirm：耗時動作（3D 重建、排程）→ 先出確認卡
- direct：唯讀且信心足夠 → 直接交給 System 2 本地執行
- out_of_scope：超出範圍 → 說明系統能做什麼
"""

from dataclasses import dataclass, field

from app.agent.types import System1Result
from app.core.config import get_agent_config


@dataclass
class GateDecision:
    gate: str
    threshold: float
    reason: str
    # 澄清按鈕的選項：[(意圖, 機率)]
    options: list[tuple[str, float]] = field(default_factory=list)


def decide(result: System1Result) -> GateDecision:
    cfg = get_agent_config()
    intents = cfg["intents"]
    intent, conf, margin = result.intent, result.confidence, result.margin
    risk = intents[intent]["risk"]
    threshold = float(cfg["thresholds"][risk])
    notes = []
    if result.flags.get("overrides_rules"):
        threshold = min(0.99, threshold + float(cfg["override_penalty"]))
        notes.append("偵測到想略過規則的說法，門檻提高")
    if result.engine != "user" and (conf < threshold or margin < float(cfg["clarify_margin"])):
        options = [(k, p) for k, p in result.ranked[:3] if p >= 0.05] or result.ranked[:2]
        why = (
            f"信心 {conf:.2f} 低於門檻 {threshold:.2f}"
            if conf < threshold
            else f"前兩名差距 {margin:.2f} 小於 {cfg['clarify_margin']}"
        )
        return GateDecision("clarify", threshold, "；".join([why, *notes]), options)
    label = intents[intent]["label"]
    if intent == "out_of_scope":
        return GateDecision("out_of_scope", threshold, "與本系統的功能無關")
    if risk == "write":
        reason = f"{label}：寫入類，門檻 {threshold:.2f}，進入修改資料流程"
        return GateDecision("modify", threshold, "；".join([reason, *notes]))
    if risk == "heavy":
        reason = f"{label}：耗時動作，門檻 {threshold:.2f}，先出確認卡"
        return GateDecision("confirm", threshold, "；".join([reason, *notes]))
    reason = f"{label}：唯讀，信心 {conf:.2f} ≥ {threshold:.2f}，直接執行"
    return GateDecision("direct", threshold, "；".join([reason, *notes]))
