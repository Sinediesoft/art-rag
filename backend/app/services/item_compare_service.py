"""兩件並排比較（docs/adr/017）：兩幅畫或兩張圖紙，逐欄並排，不同的格子標出來，每格附出處。

- 表格：直接讀知識庫 JSON（kb/ 經建索引後的資料），不呼叫模型，毫秒級；
  畫作比基本資料、典藏、風格與色彩分析（ADR 010），
  圖紙比料號、版次、材料與標準模型算出的外形、體積、重量。
- 差異摘要（選用）：本地生成端依「表格的事實＋兩邊各幾段知識段落」寫一段，每句附 [編號]；
  段落先過地端的洩密規則（和七段權限控管第 4 段同一組 local_rules），圖紙只用本地模型。
影像上的差異（同一件作品的兩張照片哪裡不同）是另一個工具：compare_service（docs/adr/012）。
"""

import re
import time
from collections.abc import AsyncIterator

from app.agent import guard
from app.core.config import get_models_config
from app.core.errors import AppError
from app.rag.prompt import load_template
from app.rag.providers import ProviderUnavailable, get_provider
from app.repositories.index_store import get_store
from app.services import memory_guard
from app.services.chat_service import NO_EGRESS, sse
from app.services.identity import Account, require_domain, require_part
from app.services.search_service import artwork_summary, part_summary

REF = re.compile(r"^(artwork|part):([a-z0-9][a-z0-9-]*)$")
REF_PATTERN = REF.pattern
KB_JSON = {"artwork": "kb/artworks/{id}.json", "part": "kb/parts/{id}.json"}


def _parse(ref: str) -> tuple[str, str]:
    m = REF.match(ref)
    if not m:
        raise AppError("VALIDATION_ERROR", f"比較對象要寫成 artwork:<id> 或 part:<id>：{ref}", 422)
    return m.group(1), m.group(2)


def _load(kind: str, item_id: str, account: Account) -> dict:
    store = get_store()
    if kind == "artwork":
        a = store.get_artwork(item_id)
        if not a:
            raise AppError("ARTWORK_NOT_FOUND", f"找不到畫作 {item_id}", 404)
        return a
    require_domain(account, "mfg")
    p = store.get_part(item_id)
    if not p:
        raise AppError("PART_NOT_FOUND", f"找不到圖紙 {item_id}", 404)
    require_part(account, p)
    return p


def resolve(a: str, b: str, account: Account) -> tuple[str, dict, dict]:
    ka, ia = _parse(a)
    kb, ib = _parse(b)
    if ka != kb:
        raise AppError(
            "COMPARE_KIND_MISMATCH",
            "只能比較同一類：兩幅畫，或兩張圖紙（畫作和圖紙沒有可以對照的欄位）",
            422,
        )
    if ia == ib:
        raise AppError("VALIDATION_ERROR", "請選兩件不同的作品或圖紙", 422)
    return ka, _load(ka, ia, account), _load(kb, ib, account)


# ---------------------------------------------------------------- 表格
def _fmt(v) -> str | None:
    if v is None or v == "" or v == []:
        return None
    if isinstance(v, list):
        return "、".join(str(x) for x in v)
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)


def _palette(a: dict) -> str | None:
    pal = (a.get("colors") or {}).get("palette") or []
    return "、".join(f"{c['name']} {c['share'] * 100:.0f}%" for c in pal[:4]) or None


def _temperature(t: dict) -> str | None:
    if not t:
        return None
    return "・".join(
        f"{k} {t[key] * 100:.0f}%"
        for k, key in (("暖", "warm"), ("冷", "cool"), ("中性", "neutral"))
    )


def _art_rows(a: dict) -> list[tuple[str, str, str, object, str]]:
    """(key, 分區, 欄位, 值, 出處種類)。出處種類：kb＝知識庫 JSON、color＝建索引時算的色彩分析。"""
    colors = a.get("colors") or {}
    light = colors.get("lightness") or {}
    temp = colors.get("temperature") or {}
    return [
        ("title", "基本資料", "畫名", a["title"]["zh"], "kb"),
        ("title_en", "基本資料", "英文畫名", a["title"].get("en"), "kb"),
        ("artist", "基本資料", "作者", a["artist"]["zh"], "kb"),
        ("date_text", "基本資料", "年代", a["date_text"], "kb"),
        ("medium", "基本資料", "材質", a.get("medium"), "kb"),
        ("dimensions", "基本資料", "尺寸", a.get("dimensions"), "kb"),
        ("style_tags", "基本資料", "風格標籤", a.get("style_tags"), "kb"),
        ("collection", "典藏與授權", "典藏單位", a["collection"], "kb"),
        ("source_id", "典藏與授權", "館藏編號", a.get("source_id"), "kb"),
        ("image_license", "典藏與授權", "圖片授權", a["image"]["license"], "kb"),
        ("topics", "典藏與授權", "知識段落", [d["topic"] for d in a["descriptions"]], "kb"),
        ("color_summary", "色彩分析", "整體", colors.get("summary"), "color"),
        ("palette", "色彩分析", "主要色彩", _palette(a), "color"),
        (
            "lightness",
            "色彩分析",
            "平均明度 L*",
            light.get("mean"),
            "color",
        ),
        ("temperature", "色彩分析", "冷暖比例", _temperature(temp), "color"),
    ]


def _part_rows(p: dict) -> list[tuple[str, str, str, object, str]]:
    """出處種類：kb＝知識庫 JSON、cad＝執行標準模型算出、drawing＝照片建檔時確認的圖上標註。"""
    g = p.get("geometry") or {}
    geo = "cad" if "cad" in p else "drawing"
    return [
        ("name", "基本資料", "品名", p["name"]["zh"], "kb"),
        ("part_no", "基本資料", "料號", p["part_no"], "kb"),
        ("drawing_no", "基本資料", "圖號", p["drawing_no"], "kb"),
        ("revision", "基本資料", "版次", p["revision"], "kb"),
        ("category", "基本資料", "類別", p["category"], "kb"),
        ("company", "基本資料", "公司", p.get("company"), "kb"),
        ("owner", "基本資料", "負責單位", p.get("owner"), "kb"),
        ("confidentiality", "基本資料", "機密等級", p["confidentiality"], "kb"),
        ("material", "材料與表面", "材料", p["material"], "kb"),
        ("density", "材料與表面", "密度（g/cm³）", p["density_g_cm3"], "kb"),
        ("surface", "材料與表面", "表面處理", p.get("surface"), "kb"),
        ("width", "外形", "寬（mm）", g.get("width"), geo),
        ("depth", "外形", "深（mm）", g.get("depth"), geo),
        ("height", "外形", "高（mm）", g.get("height"), geo),
        ("volume", "外形", "體積（mm³）", g.get("volume_mm3"), "cad"),
        ("weight", "外形", "重量（kg）", g.get("weight_kg"), "cad"),
        ("model", "外形", "標準 3D 模型", "有" if "cad" in p else "無（照片建檔）", "kb"),
        ("topics", "文件", "知識段落", [d["topic"] for d in p["descriptions"]], "kb"),
    ]


def _source(kind: str, item: dict, how: str) -> dict:
    path = KB_JSON[kind].format(id=item["id"])
    if how == "color":
        return {"label": "色彩分析（建索引時由原圖計算，ADR 010）", "url": None}
    if how == "cad":
        if "cad" not in item:
            return {"label": "照片建檔的零件沒有標準模型，未計算", "url": None}
        return {"label": f"執行標準模型 {item['cad']} 計算", "url": None}
    if how == "drawing":
        return {"label": "照片建檔時確認的圖上標註", "url": None}
    if kind == "artwork":
        return {"label": f"{path}（資料出處：典藏頁）", "url": item["source_url"]}
    return {"label": path, "url": None}


def _same(va, vb) -> bool:
    if isinstance(va, list) and isinstance(vb, list):
        return sorted(map(str, va)) == sorted(map(str, vb))
    return _fmt(va) == _fmt(vb)


def table(a_ref: str, b_ref: str, account: Account) -> dict:
    t0 = time.perf_counter()
    kind, a, b = resolve(a_ref, b_ref, account)
    build = _art_rows if kind == "artwork" else _part_rows
    rows = []
    for (key, group, label, va, how), (_, _, _, vb, how_b) in zip(build(a), build(b), strict=True):
        rows.append(
            {
                "key": key,
                "group": group,
                "label": label,
                "a": _fmt(va),
                "b": _fmt(vb),
                "same": _same(va, vb),
                "source_a": _source(kind, a, how),
                "source_b": _source(kind, b, how_b),
            }
        )
    summary = artwork_summary if kind == "artwork" else part_summary
    return {
        "kind": kind,
        "a": {**summary(a), "ref": a_ref},
        "b": {**summary(b), "ref": b_ref},
        "level": _max_level(kind, a, b),
        "rows": rows,
        "differences": sum(not r["same"] for r in rows if r["a"] or r["b"]),
        "latency_ms": round((time.perf_counter() - t0) * 1000),
        "egress": NO_EGRESS,
    }


def _max_level(kind: str, a: dict, b: dict) -> str:
    if kind == "artwork":
        return "公開"
    order = ["公開", "內部", "機密"]
    return max(a["confidentiality"], b["confidentiality"], key=order.index)


# ---------------------------------------------------------------- 差異摘要（本地生成）
def _chunks(kind: str, item: dict, limit: int) -> tuple[list[dict], int]:
    """這件作品的知識段落（最多 limit 段）與被剔除的段數：先過地端洩密規則
    （觀眾留言夾帶的指令、要求附內部資料的段落不放）；色彩段落不放，表格裡已經有色彩分析。"""
    store = get_store()
    pool = (
        [c for c in store.chunks if c.get("artwork_id") == item["id"]]
        if kind == "artwork"
        else [c for c in store.mfg.chunks if c.get("part_id") == item["id"]]
    )
    pool = [c for c in pool if not c["chunk_id"].endswith("#color")]
    kept = [c for c in pool if not guard.local_leak(c["text"])]
    return kept[:limit], len(pool) - len(kept)


def _title(kind: str, item: dict) -> str:
    return item["title"]["zh"] if kind == "artwork" else item["name"]["zh"]


async def summary_stream(
    a_ref: str, b_ref: str, account: Account, request_id: str
) -> AsyncIterator[str]:
    """先驗權限、組好上下文，再交給本地生成端；記憶體管理標記 Qwen3-VL 使用中。"""
    kind, a, b = resolve(a_ref, b_ref, account)  # 權限不符直接丟 AppError（403／404）
    events = _summary(kind, a, b, request_id)
    async for e in memory_guard.stream("compare_summary", {"qwen"}, events):
        yield e


async def _summary(kind: str, a: dict, b: dict, request_id: str) -> AsyncIterator[str]:
    t0 = time.perf_counter()
    cfg = get_models_config().item_compare
    rows = _art_rows if kind == "artwork" else _part_rows
    facts = []
    for (_, _, label, va, _), (_, _, _, vb, _) in zip(rows(a), rows(b), strict=True):
        if _fmt(va) or _fmt(vb):
            mark = "（相同）" if _same(va, vb) else ""
            facts.append(f"- {label}：甲 {_fmt(va) or '—'}；乙 {_fmt(vb) or '—'}{mark}")
    sources, ctx, dropped = [], [], 0
    for side, item in (("甲", a), ("乙", b)):
        chunks, n = _chunks(kind, item, cfg.chunks_per_item)
        dropped += n
        for c in chunks:
            ref = len(sources) + 1
            title = _title(kind, item)
            sources.append(
                {
                    "ref": ref,
                    "chunk_id": c["chunk_id"],
                    "side": side,
                    "title": title,
                    "topic": c["topic"],
                    "text": c["text"],
                    "source_url": c.get("source_url"),
                    "source_label": c.get("source"),
                    "license": c.get("license"),
                }
            )
            ctx.append(f"[{ref}]（{side}〈{title}〉{c['topic']}）{c['text']}")
    yield sse(
        "sources",
        {
            "request_id": request_id,
            "kind": kind,
            "sources": sources,
            "dropped": dropped,
        },
    )
    tpl = load_template(cfg.prompt_version)
    noun = "畫作" if kind == "artwork" else "零件圖紙"
    user = (
        tpl["user"]
        .replace("{{noun}}", noun)
        .replace("{{a}}", _title(kind, a))
        .replace("{{b}}", _title(kind, b))
        .replace("{{facts}}", "\n".join(facts))
        .replace("{{passages}}", "\n".join(ctx) or "（沒有知識段落）")
    )
    messages = [
        {"role": "system", "content": tpl["system"]},
        {"role": "user", "content": [{"type": "text", "text": user}]},
    ]
    errors, used, model, first_ms, usage = [], None, None, None, None
    for strategy in ("hybrid", "hybrid_fallback"):  # 只走本地：圖紙屬機密，畫作也不必外送
        try:
            provider = get_provider(strategy)
            provider.max_tokens, provider.temperature = cfg.max_tokens, 0.2
            async for piece in provider.stream(messages):
                if first_ms is None:
                    first_ms = round((time.perf_counter() - t0) * 1000)
                yield sse("token", {"text": piece})
            used, model, usage = strategy, provider.model, provider.usage
            break
        except ProviderUnavailable as e:
            errors.append(f"{strategy}：{e}")
    if used is None:
        yield sse(
            "error",
            {
                "code": "STRATEGY_UNAVAILABLE",
                "message": "本地模型無法使用（" + "；".join(errors) + "），表格仍然可以看",
                "request_id": request_id,
            },
        )
        return
    yield sse(
        "done",
        {
            "request_id": request_id,
            "model": model,
            "strategy_used": used,
            "fallback": used != "hybrid",
            "prompt_version": cfg.prompt_version,
            "latency_ms": {
                "first_token": first_ms,
                "total": round((time.perf_counter() - t0) * 1000),
            },
            "tokens": {"input": usage.input_tokens, "output": usage.output_tokens},
            "egress": NO_EGRESS,
        },
    )
