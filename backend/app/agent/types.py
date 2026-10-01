"""System 1 的判斷結果：Jev 與本地路由輸出同一種格式，信心閘門只看這個。"""

from dataclasses import dataclass, field


@dataclass
class System1Result:
    engine: str  # jev／local／user（使用者點澄清按鈕）
    intent_probs: dict[str, float]
    op_probs: dict[str, float] = field(default_factory=dict)
    flags: dict[str, bool] = field(default_factory=dict)
    flag_probs: dict[str, float] = field(default_factory=dict)
    model: str = ""
    latency_ms: int = 0
    # 送出本機的位元組數（Jev 請求本文）；本地路由為 0
    egress_bytes: int = 0
    jev_confidence: float | None = None
    detail: dict = field(default_factory=dict)

    @property
    def ranked(self) -> list[tuple[str, float]]:
        return sorted(self.intent_probs.items(), key=lambda kv: kv[1], reverse=True)

    @property
    def intent(self) -> str:
        return self.ranked[0][0]

    @property
    def confidence(self) -> float:
        return self.ranked[0][1]

    @property
    def margin(self) -> float:
        r = self.ranked
        return r[0][1] - (r[1][1] if len(r) > 1 else 0.0)

    @property
    def modify_op(self) -> str | None:
        """機率最高的修改操作；Jev 判斷「不是修改」（none）時為 None。"""
        if not self.op_probs:
            return None
        top = max(self.op_probs.items(), key=lambda kv: kv[1])[0]
        return None if top == "none" else top


def normalize(probs: dict[str, float], keys: list[str]) -> dict[str, float]:
    """補齊沒出現的選項（機率 0），總和調成 1。"""
    full = {k: max(0.0, float(probs.get(k, 0.0))) for k in keys}
    total = sum(full.values())
    if total <= 0:
        return {k: 1 / len(keys) for k in keys}
    return {k: v / total for k, v in full.items()}
