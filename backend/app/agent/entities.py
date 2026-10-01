"""本機前處理：從一句話找出零件、倉庫、客戶、畫作、單號，並在送 Jev 前換成代號。

對照表（〈連接法蘭〉↔ [圖紙A]）只留在本機；名稱清單由知識庫、工廠資料庫與
shared/agent.yaml 的別名自動產生，make demo-add 新增圖紙或畫作後不必改程式。
"""

import re
import threading
from dataclasses import dataclass, field
from string import ascii_uppercase

from app.core.config import get_agent_config
from app.repositories.index_store import get_store
from app.repositories.inventory_repo import get_inventory_repo

DOC_NO = re.compile(r"\b(SO|WO)-\d{4}-\d{2,3}\b", re.I)
SITE = re.compile(r"一廠|二廠")
STATUS = re.compile(r"可用|保留|待檢|不良")

# 代號前綴：零件用字母（圖紙A），其他用數字
CODE_PREFIX = {
    "part": "圖紙",
    "warehouse": "倉庫",
    "customer": "客戶",
    "artwork": "畫作",
    "artist": "畫家",
    "so": "訂單",
    "wo": "工單",
}


@dataclass
class Entity:
    kind: str  # part／warehouse／customer／artwork／artist／so／wo／site
    id: str  # mfg-002／WH-A／晨峰自動化／aic-27992／SO-2609-008／WO-2610-04／一廠
    label: str  # 顯示名稱
    text: str  # 原文中出現的字串
    start: int
    end: int


@dataclass
class Masked:
    text: str
    # 代號 → 實體（只留在本機，畫面上可以展開看）
    mapping: dict[str, dict] = field(default_factory=dict)


def _term_regex(term: str) -> str:
    """名稱中的空格可有可無（「L 型固定支架」也認得「L型固定支架」）。"""
    return r"\s*".join(re.escape(part) for part in term.split())


class EntityIndex:
    def __init__(self) -> None:
        cfg = get_agent_config()["aliases"]
        store = get_store()
        hints = get_inventory_repo().value_hints()
        terms: dict[str, tuple[str, str, str]] = {}

        def add(term: str, kind: str, id_: str, label: str) -> None:
            term = term.strip()
            if len(term) >= 2 and term not in terms:
                terms[term] = (kind, id_, label)

        for p in store.parts:
            label = p["name"]["zh"]
            for t in [
                label,
                p["part_no"],
                p["drawing_no"],
                p["id"],
                *cfg["parts"].get(p["id"], []),
            ]:
                add(t, "part", p["id"], label)
        for w in hints["warehouses"]:
            for t in [w["name"], w["warehouse_id"], *cfg["warehouses"].get(w["warehouse_id"], [])]:
                add(t, "warehouse", w["warehouse_id"], w["name"])
        customers = hints["customers"]
        for c in customers:
            add(c, "customer", c, c)
            short = c[:2]  # 「晨峰」：前兩字不跟其他客戶重複才加
            if sum(x.startswith(short) for x in customers) == 1:
                add(short, "customer", c, c)
        for a in store.artworks:
            title = a["title"]["zh"]
            add(title, "artwork", a["id"], title)
            if a["title"].get("en"):
                add(a["title"]["en"].split(" — ")[0], "artwork", a["id"], title)
            artist = a["artist"]["zh"]
            add(artist, "artist", a["id"], artist)
            add(artist.split("．")[-1], "artist", a["id"], artist)  # 「梵谷」
        self.terms = terms
        ordered = sorted(terms, key=len, reverse=True)  # 同一位置優先比對最長的名稱
        self.pattern = (
            re.compile("|".join(_term_regex(t) for t in ordered), re.I) if ordered else None
        )
        self._lookup = {re.sub(r"\s+", "", t).lower(): v for t, v in terms.items()}

    def find(self, text: str) -> list[Entity]:
        found: list[Entity] = []
        taken: list[tuple[int, int]] = []
        for m in DOC_NO.finditer(text):
            no = m.group(0).upper()
            found.append(Entity(no[:2].lower(), no, no, m.group(0), m.start(), m.end()))
            taken.append(m.span())
        if self.pattern:
            for m in self.pattern.finditer(text):
                if any(s < m.end() and m.start() < e for s, e in taken):
                    continue
                kind, id_, label = self._lookup[re.sub(r"\s+", "", m.group(0)).lower()]
                found.append(Entity(kind, id_, label, m.group(0), m.start(), m.end()))
                taken.append(m.span())
        for m in SITE.finditer(text):
            if not any(s <= m.start() < e for s, e in taken):
                found.append(Entity("site", m.group(0), m.group(0), m.group(0), m.start(), m.end()))
        return sorted(found, key=lambda e: e.start)

    @staticmethod
    def pseudonymize(text: str, entities: list[Entity]) -> Masked:
        """名稱換成代號：〈連接法蘭〉→ [圖紙A]、晨峰自動化 → [客戶1]。廠區、數字、日期保留。"""
        codes: dict[tuple[str, str], str] = {}
        counters: dict[str, int] = {}
        out, pos = [], 0
        masked = Masked(text="")
        for e in entities:
            if e.kind not in CODE_PREFIX:
                continue
            key = (e.kind, e.id)
            if key not in codes:
                n = counters.get(e.kind, 0)
                counters[e.kind] = n + 1
                suffix = ascii_uppercase[n % 26] if e.kind == "part" else str(n + 1)
                codes[key] = f"[{CODE_PREFIX[e.kind]}{suffix}]"
                masked.mapping[codes[key]] = {"kind": e.kind, "id": e.id, "label": e.label}
            out.append(text[pos : e.start])
            out.append(codes[key])
            pos = e.end
        out.append(text[pos:])
        masked.text = "".join(out)
        return masked


_index: EntityIndex | None = None
_index_key: tuple = ()
_lock = threading.Lock()


def get_index() -> EntityIndex:
    """知識庫重建索引或工廠資料庫重建後自動換上新的名稱清單。"""
    global _index, _index_key
    repo = get_inventory_repo()
    repo.ensure_built()
    key = (get_store().manifest.get("kb_hash"), repo.manifest.get("seed_hash"))
    with _lock:
        if _index is None or key != _index_key:
            _index, _index_key = EntityIndex(), key
        return _index


def first(entities: list[Entity], kind: str) -> Entity | None:
    return next((e for e in entities if e.kind == kind), None)
