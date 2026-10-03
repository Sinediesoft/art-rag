"""七段權限控管（docs/adr/015）：使用者提問從收到到生成的七道關卡。

這裡是第 1、2、4、5、6 段與「拒絕並記錄」。

個資遮蔽：一收到就把手機、Email、身分證換成 [電話1]…，原值不保留；
之後各段、紀錄、送 Jev 都只看遮蔽後的文字
1. 認證與授權：API 閘道驗 JWT 的簽章與效期（identity.gateway，不符就 401、碰不到任何模型）；
   這裡再用憑證裡的角色檢查「要做的事」能不能做（auth_check），同時產生第 3 段的 Metadata Filter。
   文件層級的權限不在這裡透露，交給第 3 段過濾、第 6 段降級
2. Jev 意圖路由／防護欄（Jev Choice）：正常查詢／Prompt 注入／無關閒聊三選一，只收代號化文字；
   注入就拒絕並記錄、閒聊就快速短路回覆。叫不到 Jev（斷網、逾時、回錯誤、沒金鑰）才改用地端規則
3. 權限感知檢索：chat_service.retrieve，Metadata Filter 只照 JWT 產生（clearance＋dept）
4. Jev Noul 雙重驗證：每段 ① is_relevant ② security_leak_check，任一不通過就剔除
5. Jev Score 評分重排：通過的段落依幫助程度打分（0～3），取前 max_keep 段
6. 生成閘門：Jev Noul 檢查可答性與合規；權限內沒有可答內容就降級回「查無資料」，
   不透露「有文件但你沒有權限」
7. 本地 LLM 生成：只依第 5 段留下的段落回答（chat_service）

第 4～6 段只把公開段落（畫作）代號化後送 Jev；內部、機密段落（工廠圖紙）不出廠，
改在地端判斷（規則掃描＋本地 Qwen3-VL 挑選＋相似度）。
規則與 Jev 的題目都在 shared/agent.yaml 的 guard；資料範圍在 shared/access.yaml。
"""

import re
import time
from dataclasses import asdict, dataclass, field
from functools import lru_cache

from app.agent import jev
from app.agent.entities import Entity, get_index
from app.core.config import get_agent_config
from app.rag import rearrange as rearrange_mod
from app.repositories.logs_repo import get_logs_repo
from app.services.identity import Account, accounts, domain_label, level_rank

# 每個意圖會讀哪一個資料領域（None＝不碰地端資料）
INTENT_DOMAIN = {
    "art_search": "art",
    "art_qa": "art",
    "drawing_search": "mfg",
    "drawing_qa": "mfg",
    "reconstruct": "mfg",
    "data_query": "factory",
    "schedule": "factory",
    "modify": "factory",
}
# 問答：文件看不看得到交給第 3 段 Metadata Filter 與第 6 段生成閘門（不在第 1 段透露）
QA_INTENTS = {"art_qa", "drawing_qa"}
RISK_RANK = {"out_of_scope": 0, "read": 1, "heavy": 2, "write": 3}
RISK_VERB = {"read": "查詢", "heavy": "執行耗時工作", "write": "修改資料"}
VERDICT_LABEL = {"query": "正常查詢", "attack": "Prompt 注入或越權", "chitchat": "無關閒聊"}
# 第 4～6 段一次問多段，給 Jev 長一點的時間（第 2 段用 .env 的 JEV_TIMEOUT_S）
POST_TIMEOUT_S = 4.0


@dataclass
class Check:
    key: str
    label: str
    by: str  # 地端／Jev
    ok: bool | None  # None＝沒判定（等使用者選、或不放進上下文）
    detail: str
    warn: bool = False


def _checks(items: list[Check]) -> list[dict]:
    return [asdict(c) for c in items]


@lru_cache
def _rules() -> dict:
    g = get_agent_config()["guard"]
    lr = g["local_rules"]
    return {
        "pii": [(r["kind"], r["code"], re.compile(r["pattern"])) for r in g["pii"]],
        "direct": [(r["rule"], re.compile(r["pattern"], re.I)) for r in lr["direct"]],
        "suspect": [(r["rule"], re.compile(r["pattern"], re.I)) for r in lr["suspect"]],
        "indirect": re.compile(lr["indirect"], re.I),
        "leak": re.compile(lr["leak"], re.I),
    }


def _cfg() -> dict:
    return get_agent_config()["guard"]


# ================================================================ 個資遮蔽
def mask_pii(text: str) -> tuple[str, list[dict]]:
    """手機、市話、Email、身分證換成 [電話1]、[Email1]…；只回傳種類與代號，不留原值。"""
    found: list[dict] = []
    counters: dict[str, int] = {}
    for kind, code, rx in _rules()["pii"]:

        def sub(m: re.Match, kind: str = kind, code: str = code) -> str:
            counters[code] = counters.get(code, 0) + 1
            c = f"[{code}{counters[code]}]"
            found.append({"kind": kind, "code": c})
            return c

        text = rx.sub(sub, text)
    return text, found


# ================================================================ 第 1 段：認證與授權
@dataclass
class MetaFilter:
    """第 3 段向量檢索的 Metadata Filter：只照 JWT 的 clearance 與 depts 產生，
    不採用使用者輸入或模型產出的條件（前端改不了）。"""

    domain: str
    clearance: int
    depts: list[str]
    doc_id: str | None = None
    doc_label: str | None = None
    doc_level: str | None = None

    @classmethod
    def of(cls, account: Account, domain: str, doc: dict | None = None) -> "MetaFilter":
        f = cls(domain, account.clearance, ["公開", *account.depts])
        if doc:
            f.doc_id, f.doc_label = doc["id"], doc["label"]
            # 看不到的文件不回傳它的機密等級：不透露「有文件但你沒有權限」
            if f.allows(doc["level"], doc.get("dept", "公開")):
                f.doc_level = doc["level"]
        return f

    def allows(self, level: str, dept: str) -> bool:
        return level_rank(level) <= self.clearance and dept in self.depts

    @property
    def levels(self) -> list[str]:
        order = ["公開", "內部", "機密"]
        return [x for x in order if level_rank(x) <= self.clearance]

    @property
    def text(self) -> str:
        depts = ", ".join(f'"{x}"' for x in self.depts)
        doc = f' AND doc_id = "{self.doc_id}"' if self.doc_id else ""
        return (
            f'domain = "{domain_label(self.domain)}" AND clearance <= {self.clearance}'
            f" AND dept IN ({depts}){doc}"
        )

    def public(self) -> dict:
        return {
            **asdict(self),
            "levels": self.levels,
            "domain_label": domain_label(self.domain),
            "text": self.text,
        }


def visible(account: Account, doc: dict) -> bool:
    """這份文件在不在憑證的權限內（機密等級＋部門）。"""
    return MetaFilter.of(account, "mfg").allows(doc["level"], doc.get("dept", "公開"))


@dataclass
class AuthResult:
    passed: bool
    pending: bool
    checks: list[Check]
    tag: str | None = None
    reason: str | None = None
    # 換成誰就可以（給「切換成〇〇再試一次」）；文件層級的降級不給
    retry: dict | None = None
    filter: MetaFilter | None = None
    # True＝不是權限不足，而是「查無資料」的降級回應（不透露有文件但你沒有權限）
    degraded: bool = False

    def public(self) -> dict:
        return {
            "passed": self.passed,
            "pending": self.pending,
            "checks": _checks(self.checks),
            "tag": self.tag,
            "reason": self.reason,
            "retry": self.retry,
            "filter": self.filter.public() if self.filter else None,
            "degraded": self.degraded,
        }


def needed_op(intent: str, op: str | None) -> str | None:
    """這個意圖要哪一項操作權限：排程要 schedule_run，修改要該項操作，其他（查詢、3D）人人可用。"""
    if intent == "schedule":
        return "schedule_run"
    if intent == "modify":
        return op
    return None


def _who_can(
    domain: str, level: str | None, op: str | None, entities: list[Entity]
) -> list[Account]:
    """誰能做：領域、等級、操作都符合；句子提到的倉庫或客戶在誰的範圍，誰排前面。"""
    wh = {e.id for e in entities if e.kind == "warehouse"}
    cust = {e.id for e in entities if e.kind == "customer"}
    ok = [
        a
        for a in accounts().values()
        if a.can_read(domain) and (level is None or a.can_see(level)) and (op is None or a.can(op))
    ]
    return sorted(ok, key=lambda a: not (wh & set(a.warehouses) or cust & set(a.customers)))


def auth_check(
    token_checks: list[dict],
    account: Account,
    intent: str,
    gate: str,
    op: str | None,
    op_label: str | None,
    doc: dict | None,
    entities: list[Entity],
) -> AuthResult:
    """token_checks：閘道驗過的簽章、效期、角色（identity.verify）。
    doc：已指定的對象 {id, label, level, dept}（某幅畫、某張圖紙），沒有就是 None。"""
    checks = [Check(c["key"], c["label"], "地端", c["ok"], c["detail"]) for c in token_checks]
    if gate == "clarify":
        checks.append(
            Check("function", "功能授權", "地端", None, "要做什麼還不確定，等你選擇後再檢查")
        )
        return AuthResult(True, True, checks)
    domain = INTENT_DOMAIN.get(intent)
    if domain is None:
        checks.append(Check("function", "功能授權", "地端", True, "不涉及地端資料，直接回覆"))
        return AuthResult(True, False, checks)

    tag = None
    degraded = False
    dlabel = domain_label(domain)
    filt = MetaFilter.of(account, domain, doc) if domain in ("art", "mfg") else None
    # 功能授權：角色能不能用這個資料領域（訪客不能用工廠圖紙、工廠資料庫）
    if not account.can_read(domain):
        checks.append(
            Check(
                "function",
                "功能授權",
                "地端",
                False,
                f"roles={[account.role_label]} 不能使用「{dlabel}」（{account.scope_note}）",
            )
        )
        tag = "權限不足"
    elif domain == "mfg" and doc and intent not in QA_INTENTS and not visible(account, doc):
        # 3D、圖紙查找指定了看不到的圖紙：降級成「查無資料」，不透露這張圖紙存在
        # （庫存、訂單、工單是工廠資料庫的權限，和圖紙的機密等級無關）
        checks.append(Check("function", "功能授權", "地端", False, _cfg()["degrade_message"]))
        tag, degraded = "資料範圍不符", True
    else:
        detail = {
            "factory": "可查工廠資料庫（唯讀連線）",
            "art": "可查公開的畫作知識庫",
        }.get(domain, f"可使用「{dlabel}」；看得到哪些文件由第 3 段 Metadata Filter 決定")
        checks.append(Check("function", "功能授權", "地端", True, detail))

    # 動作權限：查詢、3D 人人可用；排程、修改看角色
    need = needed_op(intent, op)
    any_write = any(o.startswith(("stock_", "so_", "wo_")) for o in account.ops)
    if intent == "modify" and (op is None or op == "other"):
        if op == "other":
            checks.append(
                Check(
                    "action",
                    "動作權限",
                    "地端",
                    False,
                    "零件主檔、機台資料等不開放用對話修改（任何身分都不行）",
                )
            )
            tag = tag or "權限不足"
        elif not any_write:
            checks.append(
                Check(
                    "action",
                    "動作權限",
                    "地端",
                    False,
                    f"〈{account.label}〉沒有修改資料的權限"
                    + ("（訪客只能查詢）" if account.role == "guest" else ""),
                )
            )
            tag = tag or "權限不足"
        else:
            checks.append(
                Check(
                    "action",
                    "動作權限",
                    "地端",
                    True,
                    "可以修改部分資料；是哪一種操作要等參數抽取後再判定",
                    warn=True,
                )
            )
    elif need is None:
        detail = "3D 重建所有身分都可以" if intent == "reconstruct" else "查詢"
        checks.append(Check("action", "動作權限", "地端", True, detail))
    elif account.can(need):
        what = "執行排程" if intent == "schedule" else f"做「{op_label or need}」"
        checks.append(Check("action", "動作權限", "地端", True, f"{account.role_label}可以{what}"))
    else:
        what = "執行排程" if intent == "schedule" else f"「{op_label or need}」"
        roles = "、".join(dict.fromkeys(a.role_label for a in _who_can(domain, None, need, [])))
        checks.append(
            Check(
                "action",
                "動作權限",
                "地端",
                False,
                f"訪客只能查詢，不能{'執行排程' if intent == 'schedule' else '修改資料'}"
                if account.role == "guest"
                else f"〈{account.label}〉沒有{what}的權限（{roles or '沒有任何身分'}才可以）",
            )
        )
        tag = tag or "權限不足"

    failed = [c for c in checks if c.ok is False]
    retry = None
    if failed and op != "other" and not degraded:
        who = _who_can(domain, None, need, entities)
        if who:
            retry = {"account_id": who[0].id, "label": who[0].label}
    return AuthResult(
        not failed,
        False,
        checks,
        tag,
        "；".join(dict.fromkeys(c.detail for c in failed)) or None,
        retry,
        filt,
        degraded,
    )


# ================================================================ Jev 呼叫的紀錄（給畫面看）
def _call_info(
    stage: int, reply: jev.JevReply, sent: list[str], mapping: dict, answers: list[dict]
) -> dict:
    return {
        "stage": stage,
        "model": reply.model,
        "latency_ms": reply.latency_ms,
        "bytes": reply.bytes,
        "request": reply.request,
        "sent": sent,
        "mapping": mapping,
        "answers": answers,
    }


# ================================================================ 第 2 段：Jev 意圖路由／防護欄
@dataclass
class GuardResult:
    passed: bool
    engine: str  # jev／local／skip
    checks: list[Check] = field(default_factory=list)
    tag: str | None = None
    reason: str | None = None
    skipped: str | None = None
    fallback_reason: str | None = None
    call: dict | None = None
    # Jev Choice 的結論：query（正常查詢）／attack（注入）／chitchat（閒聊 → 快速短路回覆）
    verdict: str = "query"
    # 給路由紀錄：有沒有冒充身分或要求略過管控的說法
    overrides: bool = False

    @property
    def egress_bytes(self) -> int:
        return self.call["bytes"] if self.call else 0

    def public(self) -> dict:
        return {
            "passed": self.passed,
            "engine": self.engine,
            "verdict": self.verdict,
            "checks": _checks(self.checks),
            "tag": self.tag,
            "reason": self.reason,
            "skipped": self.skipped,
            "fallback_reason": self.fallback_reason,
            "call": self.call,
        }


def _local_hits(text: str) -> tuple[tuple[str, str] | None, list[tuple[str, str]]]:
    r = _rules()
    direct = next(((rule, m.group(0)) for rule, rx in r["direct"] if (m := rx.search(text))), None)
    suspects = [(rule, m.group(0)) for rule, rx in r["suspect"] if (m := rx.search(text))]
    return direct, suspects


def _input_request(pseudo: str, photo_kind: str | None) -> tuple[dict, dict]:
    g = _cfg()
    state: dict = {"context": g["input_context"], "user_message": pseudo}
    if photo_kind:
        # 照片本身不送：只告訴 Jev 本機辨識出照片是畫作還是圖紙
        state["attached_photo"] = {"art": "畫作照片", "drawing": "工廠圖紙照片"}.get(
            photo_kind, "無法辨識的照片"
        )
    questions = {
        "intent_guard": {
            "type": "choice",
            "instructions": g["intent_question"],
            "criteria": g["intent_classes"],
        }
    }
    return state, questions


async def guard_input(
    text: str,
    pseudo: str,
    mapping: dict,
    risk: str | None,
    use_jev: bool,
    photo_kind: str | None = None,
    risk_hint: str | None = None,
    local_intent: str | None = None,
) -> GuardResult:
    """text：遮蔽個資後的原句（地端規則看這個）；pseudo：再代號化後的句子（只有這個送 Jev）。
    risk：本地分流判斷的動作類別（read／heavy／write）；還不確定時為 None，地端備援改用 risk_hint
    （候選意圖裡最危險的那個）判斷冒充身分。local_intent：本地分流的意圖（地端備援判斷閒聊用）。"""
    if not text:
        return GuardResult(True, "skip", skipped="只有照片，沒有文字要判斷")
    th = float(_cfg()["jev_threshold"])
    checks: list[Check] = []
    tag = None
    fallback = None
    reply = None
    if use_jev:
        state, questions = _input_request(pseudo, photo_kind)
        try:
            reply = await jev.ask(state, questions)
        except jev.JevUnavailable as e:
            fallback = str(e)

    if reply is not None:
        keys = list(VERDICT_LABEL)
        best, _, probs = reply.choice("intent_guard", keys)
        # Choice 回的是校準過的機率：注入、閒聊要 ≥ 門檻才採信；都不到就當正常查詢（只提醒）
        verdict = (
            "attack"
            if probs["attack"] >= th
            else "chitchat"
            if probs["chitchat"] >= th
            else "query"
        )
        p = probs[verdict]
        if verdict == "attack":
            checks.append(
                Check(
                    "intent_guard",
                    "意圖防護",
                    "Jev",
                    False,
                    f"Jev Choice 判為「Prompt 注入或越權」（{p:.2f}）→ 攔截，不進檢索",
                )
            )
            tag = "提示詞注入"
        elif verdict == "chitchat":
            checks.append(
                Check(
                    "intent_guard",
                    "意圖防護",
                    "Jev",
                    True,
                    f"Jev Choice 判為「無關閒聊」（{p:.2f}）→ 快速短路回覆，不檢索、不生成",
                    warn=True,
                )
            )
        else:
            checks.append(
                Check(
                    "intent_guard",
                    "意圖防護",
                    "Jev",
                    True,
                    f"Jev Choice 判為「正常查詢」（{p:.2f}）"
                    + ("" if best == "query" else f"；最高的是{VERDICT_LABEL[best]}但不到門檻"),
                    warn=best != "query",
                )
            )
        call = _call_info(
            2,
            reply,
            [pseudo],
            mapping,
            [
                {
                    "label": f"intent_guard・{VERDICT_LABEL[k]}",
                    "value": f"{probs[k]:.2f}",
                    "alert": k == verdict and k != "query",
                }
                for k in keys
            ],
        )
        engine = "jev"
        overrides_flag = verdict == "attack"
    else:
        # 叫不到 Jev 時的備援：地端規則只認得已知樣式
        call = None
        direct, suspects = _local_hits(text)
        eff = risk or risk_hint or "read"
        said = "、".join(f"「{m}」" for _, m in suspects)
        verdict = "query"
        if direct:
            checks.append(
                Check(
                    "intent_guard",
                    "意圖防護",
                    "地端",
                    False,
                    f"命中「{direct[0]}」（「{direct[1]}」）→ 攔截",
                )
            )
            tag, verdict = direct[0], "attack"
        elif suspects and eff in ("heavy", "write"):
            checks.append(
                Check(
                    "intent_guard",
                    "意圖防護",
                    "地端",
                    False,
                    f"{'＋'.join(r for r, _ in suspects)}（{said}），"
                    f"又要{RISK_VERB[eff]} → 越權嘗試",
                )
            )
            tag, verdict = "越權嘗試", "attack"
        elif local_intent == "out_of_scope":
            checks.append(
                Check(
                    "intent_guard",
                    "意圖防護",
                    "地端",
                    True,
                    "本地分流判為超出範圍（閒聊）→ 快速短路回覆",
                    warn=True,
                )
            )
            verdict = "chitchat"
        else:
            note = f"；有{said}的說法，但只是查詢，身分以第 1 段的憑證為準" if suspects else ""
            checks.append(
                Check(
                    "intent_guard",
                    "意圖防護",
                    "地端",
                    True,
                    "沒有命中已知的注入樣式（換句話說的攻擊認不出來）" + note,
                    warn=bool(suspects),
                )
            )
        engine = "local"
        overrides_flag = bool(suspects)

    failed = [c for c in checks if c.ok is False]
    return GuardResult(
        passed=not failed,
        engine=engine,
        checks=checks,
        tag=tag,
        reason="；".join(c.detail for c in failed) or None,
        fallback_reason=fallback,
        call=call,
        verdict=verdict,
        overrides=overrides_flag,
    )


# ================================================================ 第 4～6 段：驗證、重排、生成閘門
@dataclass
class StageInfo:
    engine: str  # jev／local
    checks: list[Check] = field(default_factory=list)
    call: dict | None = None
    fallback_reason: str | None = None
    ms: int = 0

    def public(self) -> dict:
        return {
            "engine": self.engine,
            "checks": _checks(self.checks),
            "call": self.call,
            "fallback_reason": self.fallback_reason,
            "ms": self.ms,
        }


@dataclass
class GateInfo(StageInfo):
    passed: bool = True
    message: str | None = None

    def public(self) -> dict:
        return {**super().public(), "passed": self.passed, "message": self.message}


@dataclass
class PostResult:
    mode: str  # jev／local（智慧助理的第 4～6 段）、scan（其他頁：只掃描洩密風險）
    kept: list[dict]
    candidates: int
    verify: StageInfo
    rerank: StageInfo | None = None
    gate: GateInfo | None = None
    # 第 4 段 security_leak_check 剔除的段落（chunk_id → Jev／地端）
    flagged: list[dict] = field(default_factory=list)
    flagged_by: dict[str, str] = field(default_factory=dict)
    # 與提問無關（第 4 段）、分數太低或超過上限（第 5 段）沒放進上下文的段落
    dropped: list[dict] = field(default_factory=list)
    cloud: int = 0  # 送 Jev 的段落數
    local: int = 0  # 留在地端判斷的段落數
    rearrange: dict | None = None
    ms: int = 0

    @property
    def calls(self) -> list[dict]:
        stages = [self.verify, self.rerank, self.gate]
        return [s.call for s in stages if s and s.call]

    @property
    def egress_bytes(self) -> int:
        return sum(c["bytes"] for c in self.calls)

    @property
    def degraded(self) -> bool:
        return self.gate is not None and not self.gate.passed

    def caught_by(self, chunk_id: str) -> str:
        """這段的洩密風險是誰抓到的：Jev 或地端規則。"""
        return self.flagged_by.get(chunk_id, "地端")

    def public(self) -> dict:
        def brief(s: dict) -> dict:
            return {"chunk_id": s["chunk_id"], "title": s["title"], "topic": s["topic"]}

        return {
            "mode": self.mode,
            "engine": "jev" if self.calls else "local",
            "candidates": self.candidates,
            "kept": len(self.kept),
            "flagged": [{**brief(s), "by": self.caught_by(s["chunk_id"])} for s in self.flagged],
            "dropped": [brief(s) for s in self.dropped],
            "cloud": self.cloud,
            "local": self.local,
            "verify": self.verify.public(),
            "rerank": self.rerank.public() if self.rerank else None,
            "gate": self.gate.public() if self.gate else None,
            "rearrange": self.rearrange,
            "egress_bytes": self.egress_bytes,
            "ms": self.ms,
        }


def _head(s: dict) -> str:
    return f"〈{s['title']}〉{s['topic']}："


def _local_leak(text: str) -> str | None:
    r = _rules()
    if r["indirect"].search(text):
        return "夾帶要 AI 執行的指令"
    if r["leak"].search(text):
        return "要求附上內部資料或含帳密"
    return None


async def _ask(state: dict, questions: dict) -> tuple[jev.JevReply | None, str | None]:
    try:
        return await jev.ask(state, questions, timeout_s=POST_TIMEOUT_S), None
    except jev.JevUnavailable as e:
        return None, str(e)


async def process_passages(
    question: str,
    sources: list[dict],
    mode: str,
    strategy: str = "hybrid",
    card: dict | None = None,
) -> PostResult:
    """sources：第 3 段檢索出的候選段落（每段有 level）。回傳留下的段落（ref 重新編號 1..n）。

    mode＝scan：其他頁面的問答，只用地端規則剔除有洩密風險的段落，段落數照原本規則；
    mode＝jev／local：智慧助理的第 4～6 段（雙重驗證 → 評分重排 → 生成閘門）。
    card：已指定且看得到的作品／圖紙資料 {level, text}（生成時也會放進 prompt，閘門一起看）。"""
    t0 = time.perf_counter()
    g = _cfg()
    th = float(g["jev_threshold"])
    n = len(sources)
    pid = {s["chunk_id"]: f"p{i + 1}" for i, s in enumerate(sources)}
    local_leak = {s["chunk_id"]: _local_leak(s["text"]) for s in sources}
    use_jev = mode == "jev" and jev.status()[0]
    public = [s for s in sources if s.get("level") == "公開"] if mode == "jev" else []

    # 代號化一次，第 4～6 段共用同一套代號（只代號化會送 Jev 的公開內容）
    pseudo: dict[str, str] = {}
    mapping: dict = {}
    pq = mask_pii(question)[0]
    card_public = bool(card) and card.get("level") == "公開"
    if public or (mode == "jev" and card_public):
        idx = get_index()
        texts = [pq, *(mask_pii(s["text"])[0] for s in public)]
        if card_public:
            texts.append(mask_pii(card["text"])[0])
        out, mapping = idx.pseudonymize_many(texts)
        pq = out[0]
        pseudo = {s["chunk_id"]: t for s, t in zip(public, out[1 : 1 + len(public)], strict=True)}
        if card_public:
            pseudo["__card__"] = out[-1]

    # ---------------------------------------------------------------- 第 4 段：Jev Noul 雙重驗證
    t4 = time.perf_counter()
    verify = StageInfo("local")
    jev_leak: dict[str, float] = {}
    jev_rel: dict[str, float] = {}
    if public and mode == "jev":
        state = {
            "context": g["passage_context"],
            "question": pq,
            "passages": [{"id": pid[s["chunk_id"]], "text": pseudo[s["chunk_id"]]} for s in public],
        }
        questions: dict = {}
        for s in public:
            i = pid[s["chunk_id"]]
            questions[f"{i}_relevant"] = {
                "type": "noul",
                "instructions": g["passage_relevant"].format(id=i),
            }
            questions[f"{i}_leak"] = {
                "type": "noul",
                "instructions": g["passage_leak"].format(id=i),
            }
        reply, verify.fallback_reason = await _ask(state, questions)
        if reply:
            for s in public:
                i = pid[s["chunk_id"]]
                jev_rel[s["chunk_id"]] = reply.noul(f"{i}_relevant")
                jev_leak[s["chunk_id"]] = reply.noul(f"{i}_leak")
            verify.engine = "jev"
            verify.call = _call_info(
                4,
                reply,
                [pq, *(f"{pid[s['chunk_id']]}：{pseudo[s['chunk_id']]}" for s in public)],
                mapping,
                [
                    {
                        "label": f"{pid[s['chunk_id']]}・〈{s['title']}〉{s['topic']}："
                        "is_relevant／security_leak_check",
                        "value": f"{jev_rel[s['chunk_id']]:.2f}／{jev_leak[s['chunk_id']]:.2f}",
                        "alert": jev_leak[s["chunk_id"]] >= th,
                    }
                    for s in public
                ],
            )
    cloud_ids = set(jev_rel)

    def leaked(s: dict) -> bool:
        return bool(local_leak[s["chunk_id"]]) or jev_leak.get(s["chunk_id"], 0.0) >= th

    clean = [s for s in sources if not leaked(s)]
    flagged = [s for s in sources if leaked(s)]
    flagged_by = {
        s["chunk_id"]: "Jev" if jev_leak.get(s["chunk_id"], 0.0) >= th else "地端" for s in flagged
    }
    rearrange_info = None
    judged: set[str] = set()  # 本地 Qwen3-VL 判斷過的段落
    picked: set[str] = set()
    relevant: set[str] = set()
    if mode == "scan":
        relevant = {s["chunk_id"] for s in clean}
    else:
        relevant |= {
            cid for cid in cloud_ids if jev_rel[cid] >= th and cid in {s["chunk_id"] for s in clean}
        }
        local = [s for s in clean if s["chunk_id"] not in cloud_ids]
        # 不送 Jev 的段落在地端判斷：段落篩選開著由本地 Qwen3-VL 挑；關掉、失敗或只有 1 段時看相似度
        if len(local) > 1 and rearrange_mod.enabled(None):
            got, rearrange_info = await rearrange_mod.rearrange(question, local, strategy)
            if not rearrange_info["fallback"]:
                judged = {s["chunk_id"] for s in local[: rearrange_info["candidates"]]}
                picked = set() if rearrange_info.get("none") else {p["chunk_id"] for p in got}
        floor = float(g["local_relevance_min"])
        for s in local:
            cid = s["chunk_id"]
            if (cid in picked) if cid in judged else s["score"] >= floor:
                relevant.add(cid)

    for s in sources:
        cid = s["chunk_id"]
        i = pid[cid]
        by = "Jev" if cid in cloud_ids else "地端"
        if leaked(s):
            score = jev_leak.get(cid)
            if score is not None and score >= th:
                why = f"security_leak_check 有洩密風險（{score:.2f}）"
            else:
                by, why = "地端", f"地端規則：{local_leak[cid]}"
            verify.checks.append(Check(i, f"段落 {i[1:]}", by, False, f"{_head(s)}{why} → 剔除"))
            continue
        if mode == "scan":
            verify.checks.append(
                Check(i, f"段落 {i[1:]}", "地端", True, f"{_head(s)}沒有命中洩密規則")
            )
            continue
        if cid in cloud_ids:
            rel = f"is_relevant {jev_rel[cid]:.2f}・security_leak_check {jev_leak[cid]:.2f}"
        elif cid in judged:
            verdict = "判斷有幫助" if cid in picked else "判斷沒幫助"
            rel = f"Qwen3-VL {verdict}（相似度 {s['score']:.2f}）・地端規則無洩密"
        else:
            rel = f"相似度 {s['score']:.2f}・地端規則無洩密"
        ok = cid in relevant
        verify.checks.append(
            Check(
                i,
                f"段落 {i[1:]}",
                by,
                True if ok else None,
                f"{_head(s)}{rel} → {'通過' if ok else '與提問無關，剔除'}",
            )
        )
    verify.ms = round((time.perf_counter() - t4) * 1000)
    verified = [s for s in clean if s["chunk_id"] in relevant]

    if mode == "scan":
        return PostResult(
            mode=mode,
            kept=[{**s, "ref": i + 1} for i, s in enumerate(verified)],
            candidates=n,
            verify=verify,
            flagged=flagged,
            flagged_by=flagged_by,
            local=n,
            ms=round((time.perf_counter() - t0) * 1000),
        )

    # ---------------------------------------------------------------- 第 5 段：Jev Score 評分重排
    t5 = time.perf_counter()
    rerank = StageInfo("local")
    max_keep = int(g["max_keep"])
    min_score = float(g["min_score"])
    levels = g["score_levels"]
    to_score = [s for s in verified if s["chunk_id"] in cloud_ids]
    jev_score: dict[str, float] = {}
    if to_score:
        state = {
            "context": g["score_context"],
            "question": pq,
            "passages": [
                {"id": pid[s["chunk_id"]], "text": pseudo[s["chunk_id"]]} for s in to_score
            ],
        }
        questions = {
            f"{pid[s['chunk_id']]}_score": {
                "type": "score",
                "instructions": g["score_question"].format(id=pid[s["chunk_id"]]),
                "criteria": levels,
            }
            for s in to_score
        }
        reply, rerank.fallback_reason = await _ask(state, questions)
        if reply:
            jev_score = {
                s["chunk_id"]: reply.score(f"{pid[s['chunk_id']]}_score") for s in to_score
            }
            rerank.engine = "jev"
            rerank.call = _call_info(
                5,
                reply,
                [f"{pid[s['chunk_id']]}：{pseudo[s['chunk_id']]}" for s in to_score],
                mapping,
                [
                    {
                        "label": f"{pid[s['chunk_id']]}・〈{s['title']}〉{s['topic']}："
                        f"Score（0～{len(levels) - 1}）",
                        "value": f"{jev_score[s['chunk_id']]:.2f}",
                        "alert": jev_score[s["chunk_id"]] < min_score,
                    }
                    for s in to_score
                ],
            )

    def level_of(x: float) -> str:
        return levels[min(len(levels) - 1, max(0, round(x)))].split("：")[0]

    # Jev 評過的照分數；Jev 叫不到時照第 4 段的 is_relevant；
    # 地端段落照相似度（Qwen3-VL 判斷沒幫助的已在第 4 段剔除）
    scored = sorted(
        (s for s in verified if s["chunk_id"] in jev_score),
        key=lambda s: -jev_score[s["chunk_id"]],
    )
    unscored_cloud = sorted(
        (s for s in verified if s["chunk_id"] in cloud_ids and s["chunk_id"] not in jev_score),
        key=lambda s: -jev_rel[s["chunk_id"]],
    )
    local_sorted = sorted(
        (s for s in verified if s["chunk_id"] not in cloud_ids), key=lambda s: -s["score"]
    )
    low = [s for s in scored if jev_score[s["chunk_id"]] < min_score]
    ranked = (
        [s for s in scored if jev_score[s["chunk_id"]] >= min_score] + unscored_cloud + local_sorted
    )
    kept = ranked[:max_keep]
    order = {s["chunk_id"]: i for i, s in enumerate(kept)}
    for s in verified:
        cid = s["chunk_id"]
        i = pid[cid]
        if cid in jev_score:
            by, how = "Jev", f"Score {jev_score[cid]:.2f}（{level_of(jev_score[cid])}）"
        elif cid in cloud_ids:
            by, how = "地端", f"Jev 評分不能用，照 is_relevant {jev_rel[cid]:.2f} 排序"
        else:
            by, how = "地端", f"相似度 {s['score']:.2f}"
        if cid in order:
            rerank.checks.append(
                Check(
                    i,
                    f"段落 {i[1:]}",
                    by,
                    True,
                    f"{_head(s)}{how} → 第 {order[cid] + 1} 名，放進上下文",
                )
            )
        elif s in low:
            rerank.checks.append(
                Check(
                    i, f"段落 {i[1:]}", by, None, f"{_head(s)}{how} → 低於 {min_score:g} 分，不放"
                )
            )
        else:
            rerank.checks.append(
                Check(
                    i, f"段落 {i[1:]}", by, None, f"{_head(s)}{how} → 超過 {max_keep} 段上限，不放"
                )
            )
    rerank.ms = round((time.perf_counter() - t5) * 1000)

    # ---------------------------------------------------------------- 第 6 段：生成閘門
    t6 = time.perf_counter()
    gate = GateInfo("local")
    all_public = all(s["chunk_id"] in cloud_ids for s in kept) and (card is None or card_public)
    if not kept and card is None:
        gate.passed = False
        gate.checks.append(
            Check(
                "answerable",
                "可答性",
                "地端",
                False,
                "權限內沒有任何段落通過第 4、5 段 → 不讓 LLM 臆測",
            )
        )
    elif use_jev and all_public:
        state = {
            "context": g["gate_context"],
            "question": pq,
            "passages": [{"id": pid[s["chunk_id"]], "text": pseudo[s["chunk_id"]]} for s in kept],
        }
        if card_public:
            state["work"] = pseudo["__card__"]
        questions = {
            "answerable": {"type": "noul", "instructions": g["gate_answerable"]},
            "compliant": {"type": "noul", "instructions": g["gate_compliant"]},
        }
        reply, gate.fallback_reason = await _ask(state, questions)
        if reply:
            ans, comp = reply.noul("answerable"), reply.noul("compliant")
            gate.engine = "jev"
            gate.passed = ans >= th and comp >= th
            gate.checks += [
                Check(
                    "answerable",
                    "可答性",
                    "Jev",
                    ans >= th,
                    f"只依{'作品資料與' if card else ''} {len(kept)} 段參考資料"
                    f"{'能' if ans >= th else '不能'}回答（{ans:.2f}）",
                ),
                Check(
                    "compliant",
                    "合規",
                    "Jev",
                    comp >= th,
                    f"{'不會' if comp >= th else '可能會'}洩漏個資、帳密或內部資料（{comp:.2f}）",
                ),
            ]
            gate.call = _call_info(
                6,
                reply,
                [
                    pq,
                    *([state["work"]] if card_public else []),
                    *(f"{pid[s['chunk_id']]}：{pseudo[s['chunk_id']]}" for s in kept),
                ],
                mapping,
                [
                    {
                        "label": "answerable・權限內的資料能回答",
                        "value": f"{ans:.2f}",
                        "alert": ans < th,
                    },
                    {"label": "compliant・回答合規", "value": f"{comp:.2f}", "alert": comp < th},
                ],
            )
    if not gate.checks:
        # 地端閘門：機密段落不送 Jev、或叫不到 Jev。
        # 有段落通過第 4、5 段（或已指定看得到的圖紙）才讓 LLM 回答
        gate.passed = True
        gate.checks += [
            Check(
                "answerable",
                "可答性",
                "地端",
                True,
                f"{len(kept)} 段通過驗證與重排"
                if kept
                else "段落都沒通過，只能依已指定文件的基本資料與圖面回答",
                warn=not kept,
            ),
            Check("compliant", "合規", "地端", True, "留下的段落都沒有洩密風險（第 4 段已剔除）"),
        ]
    if not gate.passed:
        gate.message = g["degrade_message"]
    gate.ms = round((time.perf_counter() - t6) * 1000)

    return PostResult(
        mode=mode,
        kept=[{**s, "ref": i + 1} for i, s in enumerate(kept)] if gate.passed else [],
        candidates=n,
        verify=verify,
        rerank=rerank,
        gate=gate,
        flagged=flagged,
        flagged_by=flagged_by,
        dropped=[s for s in clean if s["chunk_id"] not in order],
        cloud=len(cloud_ids),
        local=n - len(cloud_ids),
        rearrange=rearrange_info,
        ms=round((time.perf_counter() - t0) * 1000),
    )


# ================================================================ 拒絕並記錄（Log & Block）
def log_block(
    stage: int, rule: str, account: Account | None, text: str, request_id: str, judge: str
) -> str:
    """擋下或剔除都記一筆；text 只存遮蔽個資後的文字。回傳紀錄編號（SEC-0001）。"""
    repo = get_logs_repo()
    return repo.add_security_log(
        {
            "created_at": repo.now(),
            "request_id": request_id,
            "stage": stage,
            "rule": rule,
            "judge": judge,
            "account_id": account.id if account else None,
            "account_label": account.label if account else None,
            "text": text[:300],
        }
    )
