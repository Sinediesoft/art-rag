"""五段防護（docs/adr/012）：使用者輸入從收到到生成的五道關卡，這裡是第 1、2、4 段與「拒絕並記錄」。

個資遮蔽：一收到就把手機、Email、身分證換成 [電話1]…，原值不保留；
之後各段、紀錄、送 Jev 都只看遮蔽後的文字
1. 接收輸入・RBAC：身分只看伺服器端工作階段（打字說「我是主管」不算）；本地分流判斷要做什麼；
   資料範圍（領域＋機密等級，shared/access.yaml 的 clearance）與動作權限都在後端硬性檢查，
   同時產生第 3 段的 Metadata Filter。沒過就拒絕並記錄，不送 Jev、不碰資料
2. 前置防禦・Jev 第一層護欄：prompt_attack／overrides_rules／risk_class 三題，只收代號化文字；
   Jev 不能用（沒金鑰、逾時、選了地端規則）時改用地端規則；用 Jev 時地端規則也照跑當保底
3. 檢索・Metadata Filter 向量檢索：chat_service.retrieve（看不到的圖紙在檢索時就被濾掉）
4. 後置過濾・Jev 第二層過濾：逐段檢查間接注入＋關聯性重排，最多留 max_keep 段；
   公開段落（畫作）代號化後送 Jev，內部、機密段落（工廠圖紙）不出廠，在地端過濾
5. 最終生成・地端 LLM：只依第 4 段留下的段落回答（chat_service）

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
from app.services.identity import Account, accounts, domain_label

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
RISK_LABEL = {
    "read": "查詢",
    "heavy": "耗時工作",
    "write": "修改資料",
    "out_of_scope": "與系統無關",
}
RISK_RANK = {"out_of_scope": 0, "read": 1, "heavy": 2, "write": 3}
RISK_VERB = {"read": "查詢", "heavy": "執行耗時工作", "write": "修改資料"}
STAGE_LABEL = {1: "RBAC", 2: "Jev 護欄", 4: "Jev 過濾"}
# 第 4 段一次問多段，給 Jev 長一點的時間（第 2 段用 .env 的 JEV_TIMEOUT_S）
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


# ================================================================ 第 1 段：RBAC
@dataclass
class MetaFilter:
    """第 3 段向量檢索的 Metadata Filter（由第 1 段的資料範圍產生，前端改不了）。"""

    domain: str
    levels: list[str]
    doc_id: str | None = None
    doc_label: str | None = None
    doc_level: str | None = None

    @property
    def text(self) -> str:
        levels = ", ".join(f'"{x}"' for x in self.levels)
        doc = f' AND doc_id = "{self.doc_id}"' if self.doc_id else ""
        return f'domain = "{domain_label(self.domain)}" AND level IN ({levels}){doc}'

    def public(self) -> dict:
        return {**asdict(self), "domain_label": domain_label(self.domain), "text": self.text}


@dataclass
class RbacResult:
    passed: bool
    pending: bool
    checks: list[Check]
    tag: str | None = None
    reason: str | None = None
    # 換成誰就可以（給「切換成〇〇再試一次」）
    retry: dict | None = None
    filter: MetaFilter | None = None

    def public(self) -> dict:
        return {
            "passed": self.passed,
            "pending": self.pending,
            "checks": _checks(self.checks),
            "tag": self.tag,
            "reason": self.reason,
            "retry": self.retry,
            "filter": self.filter.public() if self.filter else None,
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


def rbac_check(
    account: Account,
    intent: str,
    gate: str,
    op: str | None,
    op_label: str | None,
    doc: dict | None,
    entities: list[Entity],
) -> RbacResult:
    """doc：已指定的對象 {id, label, level}（某幅畫、某張圖紙），沒有就是 None。"""
    checks = [
        Check(
            "identity",
            "身分",
            "地端",
            True,
            f"伺服器端工作階段：〈{account.label}〉；打字自稱的身分不算",
        )
    ]
    if gate == "clarify":
        checks += [
            Check("scope", "資料範圍", "地端", None, "要做什麼還不確定，等你選擇後再檢查"),
            Check("action", "動作權限", "地端", None, "同上"),
        ]
        return RbacResult(True, True, checks)
    domain = INTENT_DOMAIN.get(intent)
    if domain is None:
        checks += [
            Check("scope", "資料範圍", "地端", True, "不涉及地端資料"),
            Check("action", "動作權限", "地端", True, "直接回覆"),
        ]
        return RbacResult(True, False, checks)

    # 資料範圍：領域＋已指定對象的機密等級
    tag = None
    dlabel = domain_label(domain)
    if not account.can_read(domain):
        checks.append(
            Check(
                "scope",
                "資料範圍",
                "地端",
                False,
                f"〈{account.label}〉不能使用「{dlabel}」（{account.scope_note}）",
            )
        )
        tag = "資料範圍不符"
    elif doc and not account.can_see(doc["level"]):
        checks.append(
            Check(
                "scope",
                "資料範圍",
                "地端",
                False,
                f"〈{doc['label']}〉是{doc['level']}文件；{account.scope_note}",
            )
        )
        tag = "資料範圍不符"
    else:
        detail = {
            "factory": "可查工廠資料庫（唯讀連線）",
            "art": "可查公開的畫作知識庫",
        }.get(domain, f"可查「{dlabel}」的{'／'.join(account.levels)}文件")
        checks.append(Check("scope", "資料範圍", "地端", True, detail))

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
    if failed and op != "other":
        who = _who_can(domain, doc["level"] if doc else None, need, entities)
        if who:
            retry = {"account_id": who[0].id, "label": who[0].label}
    filt = None
    if domain in ("art", "mfg"):
        levels = ["公開"] if domain == "art" else [x for x in account.levels if x != "公開"]
        filt = MetaFilter(
            domain,
            levels,
            doc["id"] if doc else None,
            doc["label"] if doc else None,
            doc["level"] if doc else None,
        )
    return RbacResult(
        not failed,
        False,
        checks,
        tag,
        "；".join(c.detail for c in failed) or None,
        retry,
        filt,
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


# ================================================================ 第 2 段：Jev 第一層護欄
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
    # 給路由紀錄：有沒有冒充身分或要求略過管控的說法
    overrides: bool = False

    @property
    def egress_bytes(self) -> int:
        return self.call["bytes"] if self.call else 0

    def public(self) -> dict:
        return {
            "passed": self.passed,
            "engine": self.engine,
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
        **{k: {"type": "noul", "instructions": v} for k, v in g["input_questions"].items()},
        "risk_class": {
            "type": "choice",
            "instructions": g["risk_question"],
            "criteria": g["risk_classes"],
        },
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
) -> GuardResult:
    """text：遮蔽個資後的原句（地端規則看這個）；pseudo：再代號化後的句子（只有這個送 Jev）。
    risk：第 1 段判斷的動作類別（read／heavy／write／out_of_scope）；
    還不確定（等使用者選）時為 None，這時用 risk_hint（候選意圖裡最危險的那個）判斷冒充身分。"""
    if not text:
        return GuardResult(True, "skip", skipped="只有照片，沒有文字要掃描")
    g = _cfg()
    th = float(g["jev_threshold"])
    direct, suspects = _local_hits(text)
    checks: list[Check] = []
    tag = None
    fallback = None
    call = None
    reply = None
    if use_jev:
        state, questions = _input_request(pseudo, photo_kind)
        try:
            reply = await jev.ask(state, questions)
        except jev.JevUnavailable as e:
            fallback = str(e)

    if reply is not None:
        attack = reply.noul("prompt_attack")
        overrides = reply.noul("overrides_rules")
        jr, jp, _ = reply.choice("risk_class", list(g["risk_classes"]))
        # 冒充身分要看「要做的事」危不危險：第 1 段（或候選意圖）與 Jev 取比較危險的那個
        base = risk or risk_hint or "read"
        eff = jr if RISK_RANK[jr] > RISK_RANK[base] else base
        if attack >= th:
            checks.append(
                Check(
                    "prompt_attack",
                    "直接注入",
                    "Jev",
                    False,
                    f"試圖改變助理設定、要它不受限制或套取系統設定（{attack:.2f}）",
                )
            )
            tag = "提示詞注入"
        else:
            checks.append(
                Check("prompt_attack", "直接注入", "Jev", True, f"沒有夾帶指令（{attack:.2f}）")
            )
        if overrides >= th and eff in ("heavy", "write"):
            checks.append(
                Check(
                    "overrides_rules",
                    "冒充身分",
                    "Jev",
                    False,
                    f"冒充身分或要求略過管控（{overrides:.2f}），又要{RISK_VERB[eff]} → 越權嘗試",
                )
            )
            tag = tag or "越權嘗試"
        elif overrides >= th:
            checks.append(
                Check(
                    "overrides_rules",
                    "冒充身分",
                    "Jev",
                    True,
                    f"有冒充身分的說法（{overrides:.2f}），但只是查詢；身分一律以第 1 段為準",
                    warn=True,
                )
            )
        else:
            checks.append(
                Check(
                    "overrides_rules",
                    "冒充身分",
                    "Jev",
                    True,
                    f"沒有冒充身分或要求略過管控（{overrides:.2f}）",
                )
            )
        if risk is None:
            checks.append(
                Check(
                    "risk_class",
                    "動作類別",
                    "Jev",
                    True,
                    f"判為「{RISK_LABEL[jr]}」（{jp:.2f}）；第 1 段還沒確定要做什麼，只記錄",
                )
            )
        elif (
            risk == "read"
            and RISK_RANK[jr] > RISK_RANK[risk]
            and jp >= float(g["risk_mismatch_min_prob"])
        ):
            # 查詢會直接執行、不出確認卡：Jev 認為其實要改資料或跑耗時工作，就不放行
            checks.append(
                Check(
                    "risk_class",
                    "動作類別",
                    "Jev",
                    False,
                    f"第 1 段判為「查詢」（會直接執行），Jev 判為「{RISK_LABEL[jr]}」（{jp:.2f}）"
                    " → 權限是照查詢檢查的，不放行",
                )
            )
            tag = tag or "判斷不一致"
        elif RISK_RANK[jr] > RISK_RANK[risk]:
            # 耗時工作與修改資料都要再確認、各自檢查權限：不一致只提醒
            why = (
                f"機率不高（{jp:.2f}）"
                if jp < float(g["risk_mismatch_min_prob"])
                else f"（{jp:.2f}）；兩者都要再確認、各自檢查權限"
            )
            checks.append(
                Check(
                    "risk_class",
                    "動作類別",
                    "Jev",
                    True,
                    f"Jev 判為「{RISK_LABEL[jr]}」{why}，以第 1 段的「{RISK_LABEL[risk]}」為準",
                    warn=True,
                )
            )
        else:
            checks.append(
                Check(
                    "risk_class",
                    "動作類別",
                    "Jev",
                    True,
                    f"與第 1 段一致（Jev：{RISK_LABEL[jr]} {jp:.2f}）",
                )
            )
        # 地端規則保底：已知樣式不論 Jev 怎麼判都擋
        if direct:
            checks.append(
                Check(
                    "local_direct",
                    "地端保底",
                    "地端",
                    False,
                    f"命中已知樣式「{direct[0]}」（「{direct[1]}」）",
                )
            )
            tag = tag or direct[0]
        if suspects and eff in ("heavy", "write") and not (overrides >= th):
            said = "、".join(f"「{m}」" for _, m in suspects)
            checks.append(
                Check(
                    "local_suspect",
                    "地端保底",
                    "地端",
                    False,
                    f"{'＋'.join(r for r, _ in suspects)}（{said}），"
                    f"又要{RISK_VERB[eff]} → 越權嘗試",
                )
            )
            tag = tag or "越權嘗試"
        call = _call_info(
            2,
            reply,
            [pseudo],
            mapping,
            [
                {
                    "label": "prompt_attack・注入指令／要它不受限制",
                    "value": f"{attack:.2f}",
                    "alert": attack >= th,
                },
                {
                    "label": "overrides_rules・冒充身分／略過管控",
                    "value": f"{overrides:.2f}",
                    "alert": overrides >= th,
                },
                {"label": f"risk_class・{RISK_LABEL[jr]}", "value": f"{jp:.2f}", "alert": False},
            ],
        )
        engine = "jev"
        overrides_flag = overrides >= th or bool(suspects)
    else:
        # 地端規則：只認得已知樣式
        if direct:
            checks.append(
                Check(
                    "prompt_attack",
                    "直接注入",
                    "地端",
                    False,
                    f"命中「{direct[0]}」（「{direct[1]}」）",
                )
            )
            tag = direct[0]
        else:
            checks.append(
                Check(
                    "prompt_attack",
                    "直接注入",
                    "地端",
                    True,
                    "沒有命中已知的注入樣式（換句話說的攻擊認不出來）",
                )
            )
        eff = risk or risk_hint or "read"
        said = "、".join(f"「{m}」" for _, m in suspects)
        if suspects and eff in ("heavy", "write"):
            checks.append(
                Check(
                    "overrides_rules",
                    "冒充身分",
                    "地端",
                    False,
                    f"{'＋'.join(r for r, _ in suspects)}（{said}），"
                    f"又要{RISK_VERB[eff]} → 越權嘗試",
                )
            )
            tag = tag or "越權嘗試"
        elif suspects:
            checks.append(
                Check(
                    "overrides_rules",
                    "冒充身分",
                    "地端",
                    True,
                    f"有{said}的說法，但只是查詢；身分以第 1 段為準",
                    warn=True,
                )
            )
        else:
            checks.append(
                Check("overrides_rules", "冒充身分", "地端", True, "沒有冒充身分或要求略過管控")
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
        overrides=overrides_flag,
    )


# ================================================================ 第 4 段：Jev 第二層過濾
@dataclass
class PostResult:
    mode: str  # jev／local（智慧助理的完整第 4 段）、scan（其他頁：只掃描間接注入）
    engine: str  # 實際用的：jev／local
    kept: list[dict]
    injected: list[dict] = field(default_factory=list)
    dropped: list[dict] = field(default_factory=list)
    checks: list[Check] = field(default_factory=list)
    cloud: int = 0  # 送 Jev 的段落數
    local: int = 0  # 留在地端過濾的段落數
    candidates: int = 0
    call: dict | None = None
    fallback_reason: str | None = None
    rearrange: dict | None = None
    ms: int = 0
    # 夾帶指令的段落是誰抓到的：chunk_id → Jev／地端
    injected_by: dict[str, str] = field(default_factory=dict)

    @property
    def egress_bytes(self) -> int:
        return self.call["bytes"] if self.call else 0

    def caught_by(self, chunk_id: str) -> str:
        """這段夾帶指令是誰抓到的：Jev 或地端規則。"""
        return self.injected_by.get(chunk_id, "地端")

    def public(self) -> dict:
        brief = [
            {
                "chunk_id": s["chunk_id"],
                "title": s["title"],
                "topic": s["topic"],
                "by": self.caught_by(s["chunk_id"]),
            }
            for s in self.injected
        ]
        return {
            "mode": self.mode,
            "engine": self.engine,
            "candidates": self.candidates,
            "kept": len(self.kept),
            "injected": brief,
            "dropped": [
                {"chunk_id": s["chunk_id"], "title": s["title"], "topic": s["topic"]}
                for s in self.dropped
            ],
            "checks": _checks(self.checks),
            "cloud": self.cloud,
            "local": self.local,
            "call": self.call,
            "fallback_reason": self.fallback_reason,
            "rearrange": self.rearrange,
            "ms": self.ms,
        }


def _passage_request(question: str, passages: list[tuple[str, str]]) -> tuple[dict, dict]:
    g = _cfg()
    state = {
        "context": g["passage_context"],
        "question": question,
        "passages": [{"id": pid, "text": t} for pid, t in passages],
    }
    questions: dict = {}
    for pid, _ in passages:
        questions[f"{pid}_injection"] = {
            "type": "noul",
            "instructions": g["passage_injection"].format(id=pid),
        }
        questions[f"{pid}_relevant"] = {
            "type": "noul",
            "instructions": g["passage_relevant"].format(id=pid),
        }
    return state, questions


async def filter_passages(
    question: str, sources: list[dict], mode: str, strategy: str = "hybrid"
) -> PostResult:
    """sources：第 3 段檢索出的候選段落（每段有 level）。回傳留下的段落（ref 重新編號 1..n）。

    mode＝scan：其他頁面的問答，只用地端規則移除夾帶指令的段落，段落數照原本規則；
    mode＝local／jev：智慧助理的完整第 4 段，注入檢查＋關聯性重排，最多留 max_keep 段。"""
    t0 = time.perf_counter()
    g = _cfg()
    th = float(g["jev_threshold"])
    max_keep = int(g["max_keep"])
    rx = _rules()["indirect"]
    n = len(sources)
    local_inj = {s["chunk_id"]: bool(rx.search(s["text"])) for s in sources}
    public = [s for s in sources if s.get("level") == "公開"] if mode == "jev" else []
    jev_inj: dict[str, float] = {}
    jev_rel: dict[str, float] = {}
    call = None
    fallback = None
    if public:
        idx = get_index()
        texts = [mask_pii(question)[0], *(mask_pii(s["text"])[0] for s in public)]
        pseudo, mapping = idx.pseudonymize_many(texts)
        pids = [f"p{sources.index(s) + 1}" for s in public]
        state, questions = _passage_request(pseudo[0], list(zip(pids, pseudo[1:], strict=True)))
        try:
            reply = await jev.ask(state, questions, timeout_s=POST_TIMEOUT_S)
            for pid, s in zip(pids, public, strict=True):
                jev_inj[s["chunk_id"]] = reply.noul(f"{pid}_injection")
                jev_rel[s["chunk_id"]] = reply.noul(f"{pid}_relevant")
            call = _call_info(
                4,
                reply,
                [pseudo[0], *(f"{pid}：{t}" for pid, t in zip(pids, pseudo[1:], strict=True))],
                mapping,
                [
                    {
                        "label": f"{pid}・〈{s['title']}〉{s['topic']}：注入／相關",
                        "value": f"{jev_inj[s['chunk_id']]:.2f}／{jev_rel[s['chunk_id']]:.2f}",
                        "alert": jev_inj[s["chunk_id"]] >= th,
                    }
                    for pid, s in zip(pids, public, strict=True)
                ],
            )
        except jev.JevUnavailable as e:
            fallback = str(e)
            jev_inj, jev_rel = {}, {}
    cloud_ids = set(jev_rel)

    def injected(s: dict) -> bool:
        return local_inj[s["chunk_id"]] or jev_inj.get(s["chunk_id"], 0.0) >= th

    clean = [s for s in sources if not injected(s)]
    rearrange_info = None
    if mode == "scan":
        kept = clean
    else:
        # 關聯性：Jev 判過的段落照 Jev 的機率；其他段落在地端判斷
        # （有開段落篩選就請本地 Qwen3-VL 挑，否則照向量相似度），兩組都只留前 max_keep 段
        by_jev = sorted(
            (s for s in clean if s["chunk_id"] in cloud_ids),
            key=lambda s: -jev_rel[s["chunk_id"]],
        )
        rel_jev = [s for s in by_jev if jev_rel[s["chunk_id"]] >= float(g["relevance_threshold"])]
        local = [s for s in clean if s["chunk_id"] not in cloud_ids]
        if len(local) > 1 and rearrange_mod.enabled(None):
            picked, rearrange_info = await rearrange_mod.rearrange(question, local, strategy)
            ids = {p["chunk_id"] for p in picked}
            local = [s for s in local if s["chunk_id"] in ids]
        kept = (rel_jev + local)[:max_keep]
        if not kept and clean:  # 至少留一段，拒答交給生成端的規則
            kept = (by_jev or clean)[:1]
    kept_ids = {s["chunk_id"] for s in kept}
    order = {s["chunk_id"]: i for i, s in enumerate(kept)}

    checks = []
    for i, s in enumerate(sources):
        cid = s["chunk_id"]
        by = "Jev" if cid in cloud_ids else "地端"
        head = f"〈{s['title']}〉{s['topic']}："
        if injected(s):
            score = jev_inj.get(cid)
            if score is not None and score >= th:
                by, why = "Jev", f"夾帶指令（{score:.2f}）"
            else:
                by, why = "地端", "命中地端規則：夾帶指令"
            checks.append(Check(f"p{i + 1}", f"段落 {i + 1}", by, False, f"{head}{why} → 移除"))
        elif cid in kept_ids:
            rel = f"相關 {jev_rel[cid]:.2f}" if cid in jev_rel else f"相似度 {s['score']:.2f}"
            checks.append(
                Check(
                    f"p{i + 1}",
                    f"段落 {i + 1}",
                    by,
                    True,
                    f"{head}{rel} → 第 {order[cid] + 1} 名，放進上下文",
                )
            )
        else:
            rel = f"相關 {jev_rel[cid]:.2f}" if cid in jev_rel else f"相似度 {s['score']:.2f}"
            checks.append(
                Check(f"p{i + 1}", f"段落 {i + 1}", by, None, f"{head}{rel} → 不放進上下文")
            )

    return PostResult(
        mode=mode,
        engine="jev" if call else "local",
        kept=[{**s, "ref": i + 1} for i, s in enumerate(kept)],
        injected=[s for s in sources if injected(s)],
        injected_by={
            s["chunk_id"]: "Jev" if jev_inj.get(s["chunk_id"], 0.0) >= th else "地端"
            for s in sources
            if injected(s)
        },
        dropped=[s for s in clean if s["chunk_id"] not in kept_ids],
        checks=checks,
        cloud=len(cloud_ids),
        local=n - len(cloud_ids),
        candidates=n,
        call=call,
        fallback_reason=fallback,
        rearrange=rearrange_info,
        ms=round((time.perf_counter() - t0) * 1000),
    )


# ================================================================ 拒絕並記錄（Log & Block）
def log_block(
    stage: int, rule: str, account: Account | None, text: str, request_id: str, judge: str
) -> str:
    """擋下或移除都記一筆；text 只存遮蔽個資後的文字。回傳紀錄編號（SEC-0001）。"""
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
