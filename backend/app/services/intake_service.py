"""照片建檔（docs/adr/013）：拍照 → 擋模糊 → 確認知識庫還沒有 → 填欄位 → 規則驗證 → 主管收錄進 kb/。

兩個領域共用同一條流程，只換欄位設定（shared/models.yaml 的 intake.<領域>.fields）：
- 圖紙（mfg）：找紙張四角拉正 → 對齊標題欄外框 → 800×970、白底黑線（知識庫圖紙的版面：辨識、縮圖、
  3D 重建都假設它）→ 本地 Qwen3-VL 讀標題欄與外形尺寸（response_format 用 JSON schema 限制輸出）
  → 規則：編號格式、列舉值、和知識庫重複、材料依牌號對照既有零件校正並補密度
- 畫作（art）：拍畫作本身，不讀展牌（館方解說有著作權、也沒有統一版面）；資料由人在跳出的表單填
  → 規則：來源代碼依典藏單位對照既有畫作、編號取館藏編號、授權與標示文字、和知識庫重複

模糊的照片一律先擋、不送模型：模型看不清楚時不會填 null，而是編（實測模糊照 30/49 格是編的）。
草稿存在 data/intake/<draft_id>/（照片、要存進知識庫的圖、欄位與每欄的來源），
和上傳照片同一個保存期限。
收錄（kb_intake 權限，只有主管）：再驗一次 schema → 寫 kb/ → 遞增 kb/VERSION →
背景執行緒重建索引（同一個行程，沿用已載入的模型）；新資料沒進索引就把寫進去的檔案和版本還原。
全程只連本機推論伺服器，外送資料量恆為 0。
"""

import asyncio
import base64
import importlib.util
import json
import re
import shutil
import threading
import time
import unicodedata
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from functools import lru_cache
from pathlib import Path

from app.analysis import page
from app.core.config import REPO_ROOT, IntakeDomainSpec, get_models_config, get_settings
from app.core.errors import AppError
from app.core.logging import log
from app.rag.kb import artwork_problems, part_problems
from app.rag.preprocess import load_image, to_jpeg_bytes
from app.rag.prompt import load_template
from app.rag.providers import ProviderUnavailable, get_provider
from app.repositories.index_store import get_store
from app.repositories.production_repo import get_production_repo
from app.services import memory_guard
from app.services.chat_service import NO_EGRESS, sse
from app.services.identity import Account, require
from app.services.search_service import (
    artwork_summary,
    identify,
    identify_any,
    identify_drawing,
    load_upload,
    part_summary,
)

KB_DIR = REPO_ROOT / "kb"  # 收錄寫到這裡；測試改成暫存資料夾
DRAFT_ID = re.compile(r"^intake_[0-9a-f]{16}$")
EDITABLE = {"draft", "failed"}  # 收錄失敗（已還原）可以改了再收錄
MODEL, RULE, HUMAN = "Qwen3-VL", "規則", "人"
URL = re.compile(r"^https?://\S+$")
STAGES = {
    "sharpness": "檢查清晰度",
    "identify": "確認知識庫還沒有",
    "page": "拉正、對齊知識庫版面",
    "read": "本地 Qwen3-VL 讀標題欄",
    "validate": "驗證欄位",
}


@dataclass(frozen=True)
class Domain:
    key: str
    noun: str  # 這張圖紙／這幅畫
    schema: str
    folder: str  # kb/ 底下的資料夾
    image_dir: str  # kb/ 底下放圖的資料夾
    image_name: str  # 草稿裡要存進知識庫的圖
    route: str  # 收錄後的前端頁面


DOMAINS = {
    "mfg": Domain(
        "mfg", "這張圖紙", "part.schema.json", "parts", "drawings", "drawing.png", "/drawings"
    ),
    "art": Domain(
        "art", "這幅畫", "artwork.schema.json", "artworks", "images", "image.jpg", "/artworks"
    ),
}
FILES = {"photo.jpg", *(d.image_name for d in DOMAINS.values())}

_lock = threading.Lock()  # 收錄（寫檔、遞增版本）一次只做一筆
_busy: dict[str, str | None] = {"draft_id": None}  # 正在重建索引的草稿


# ---------------------------------------------------------------- 設定與小工具
def _spec(domain: str) -> IntakeDomainSpec:
    spec = getattr(get_models_config().intake, domain, None) if domain in DOMAINS else None
    if spec is None:
        raise AppError(
            "INTAKE_DOMAIN_UNSUPPORTED", f"shared/models.yaml 沒有設定 intake.{domain}", 422
        )
    return spec


@lru_cache
def _schema(name: str) -> dict:
    return json.loads((REPO_ROOT / "shared" / "schemas" / name).read_text(encoding="utf-8"))


def _enum_options(domain: str) -> dict[str, list[str]]:
    """列舉值從 schema 讀，不在設定裡重抄一份：圖紙的機密等級、畫作的授權白名單。"""
    if domain == "art":
        licenses = _schema("artwork.schema.json")["$defs"]["license"]["enum"]
        return {"image_license": licenses, "description_license": licenses}
    props = _schema("part.schema.json")["properties"]
    return {k: v["enum"] for k, v in props.items() if "enum" in v}


@lru_cache
def _pipeline(name: str):
    """載入 pipelines/<name>.py（建索引、遞增版本和 make 指令用同一份程式）。"""
    spec = importlib.util.spec_from_file_location(
        f"artrag_pipeline_{name}", REPO_ROOT / "pipelines" / f"{name}.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _code(v: str) -> str:
    """料號、圖號、版次：全形轉半形、去空白、大寫。"""
    return "".join(unicodedata.normalize("NFKC", v).split()).upper()


def _grade(material: str | None) -> str | None:
    """材料的牌號（「SCM440 鉻鉬鋼」→ SCM440）。"""
    if not material:
        return None
    m = re.match(r"[A-Za-z0-9][A-Za-z0-9\-]*", unicodedata.normalize("NFKC", material).strip())
    return m.group(0).upper() if m else None


_CJK = "㐀-鿿豈-﫿"


def _spacing(v: str) -> str:
    """英數字和中文之間留一個空格（知識庫的寫法：「L 型固定支架」「S45C 中碳鋼」）。

    模型常把「L 型」抄成「L型」：字是對的，但和知識庫寫法不同，搜尋與比對重複時會對不上
    （2026-10-03 make eval-intake：8 格「通過驗證但錯」有 6 格是這個）。
    """
    v = re.sub(rf"([A-Za-z0-9])\s*([{_CJK}])", r"\1 \2", v)
    return re.sub(rf"([{_CJK}])\s*([A-Za-z0-9])", r"\1 \2", v)


def _normalize(key: str, raw, kind: str):
    if raw is None:
        return None
    if kind == "number":
        m = re.search(r"\d+(?:\.\d+)?", unicodedata.normalize("NFKC", str(raw)))
        return float(m.group(0)) if m else None
    v = str(raw).strip()
    if v.lower() in ("", "null", "none", "n/a"):
        return None
    if key in ("part_no", "drawing_no", "revision"):
        return _code(v)
    if key in ("id_code", "id_no"):
        return "".join(unicodedata.normalize("NFKC", v).split()).lower()
    if kind in ("enum", "url", "longtext"):
        return v
    return _spacing(v)


def _kb_items(domain: str) -> list[dict]:
    store = get_store()
    return list(store.parts if domain == "mfg" else store.artworks)


def _kb_parts() -> list[dict]:
    return _kb_items("mfg")


def _materials() -> dict[str, tuple[str, float]]:
    """知識庫既有零件的材料：牌號 → (材料寫法, 密度)。"""
    out = {}
    for p in _kb_parts():
        g = _grade(p.get("material"))
        if g and g not in out:
            out[g] = (p["material"], float(p["density_g_cm3"]))
    return out


def _suggestions(domain: str) -> dict[str, list[str]]:
    items = _kb_items(domain)
    if domain == "art":
        out = {k: sorted({a[k] for a in items if a.get(k)}) for k in ("collection", "medium")}
        out["artist"] = sorted({a["artist"]["zh"] for a in items})
        out["style_tags"] = sorted({t for a in items for t in a.get("style_tags", [])})
        return out
    keys = ("company", "category", "owner", "surface", "material")
    return {k: sorted({p[k] for p in items if p.get(k)}) for k in keys}


def _used_ids(domain: str) -> set[str]:
    """知識庫與 kb_staging（make demo-add 會加進來）用過的 ID。"""
    folder = DOMAINS[domain].folder
    dirs = (KB_DIR / folder, REPO_ROOT / "kb_staging" / folder)
    return {p.stem for d in dirs for p in d.glob("*.json")}


def _next_id(prefix: str, domain: str, width: int = 3) -> str:
    used = 0
    pat = re.compile(rf"^{re.escape(prefix)}-(\d+)$")
    for i in _used_ids(domain):
        if m := pat.match(i):
            used = max(used, int(m.group(1)))
    return f"{prefix}-{used + 1:0{width}d}"


def _item_id(d: dict) -> str:
    """圖紙收錄時依序編號（mfg-008）；畫作由「來源代碼－編號」組成（npm-000002、photo-001）。"""
    if d["domain"] == "art":
        v = d["values"]
        return f"{v.get('id_code') or ''}-{v.get('id_no') or ''}"
    return d.get("item_id") or d["proposed_id"]


# ---------------------------------------------------------------- 草稿存取
def _dir(draft_id: str) -> Path:
    if not DRAFT_ID.match(draft_id):
        raise AppError("INTAKE_DRAFT_NOT_FOUND", "找不到這份建檔草稿", 404)
    return get_settings().intake_dir / draft_id


def _load(draft_id: str) -> dict:
    path = _dir(draft_id) / "draft.json"
    if not path.is_file():
        raise AppError("INTAKE_DRAFT_NOT_FOUND", "找不到這份建檔草稿，可能已超過保存期限", 404)
    return json.loads(path.read_text(encoding="utf-8"))


def _save(d: dict) -> None:
    d["updated_at"] = _now()
    path = _dir(d["draft_id"]) / "draft.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def draft_file(draft_id: str, name: str) -> Path:
    if name not in FILES:
        raise AppError("VALIDATION_ERROR", "只提供 " + "、".join(sorted(FILES)), 422)
    path = _dir(draft_id) / name
    if not path.is_file():
        raise AppError("INTAKE_DRAFT_NOT_FOUND", "找不到這份建檔草稿，可能已超過保存期限", 404)
    return path


def purge_drafts(ttl_days: int) -> int:
    """刪掉超過保存期限的草稿（與上傳照片同一個期限）；正在收錄的不刪。"""
    root = get_settings().intake_dir
    if not root.is_dir():
        return 0
    cutoff = (datetime.now(UTC) - timedelta(days=ttl_days)).timestamp()
    n = 0
    for d in root.iterdir():
        if d.is_dir() and d.stat().st_mtime < cutoff and d.name != _busy["draft_id"]:
            shutil.rmtree(d, ignore_errors=True)
            n += 1
    return n


# ---------------------------------------------------------------- 規則
def _set_rule(d: dict, key: str, value, note: str) -> None:
    """規則補的值：人填過的欄位不動。"""
    if d["sources"].get(key) == HUMAN:
        return
    d["values"][key], d["sources"][key] = value, RULE if value is not None else None
    if value is None:
        d["notes"].pop(key, None)
    else:
        d["notes"][key] = note


def _apply_rules(d: dict) -> None:
    (_rules_art if d["domain"] == "art" else _rules_mfg)(d)


def _rules_mfg(d: dict) -> None:
    """材料依牌號對照知識庫既有零件：校正寫法（模型讀的才改）並補密度（沒人填過才補）。"""
    values, sources, notes = d["values"], d["sources"], d["notes"]
    grade = _grade(values.get("material"))
    known = _materials().get(grade) if grade else None
    if not known:
        if sources.get("density_g_cm3") == RULE:  # 材料改了、對不到 → 規則補的密度作廢
            _set_rule(d, "density_g_cm3", None, "")
        return
    name, density = known
    if values["material"] != name and sources.get("material") in (MODEL, RULE):
        notes["material"] = f"依知識庫既有材料校正（照片讀到「{values['material']}」）"
        values["material"], sources["material"] = name, RULE
    _set_rule(d, "density_g_cm3", density, f"知識庫既有零件的 {grade} 密度")


def _rules_art(d: dict) -> None:
    """知識庫 ID：來源代碼依典藏單位對照既有畫作（國立故宮博物院 → npm），編號取館藏編號。"""
    v, spec = d["values"], _spec("art")
    same = next((a for a in _kb_items("art") if a["collection"] == v.get("collection")), None)
    if same:
        code = same["id"].split("-")[0]
        _set_rule(d, "id_code", code, f"知識庫裡「{same['collection']}」的畫作用 {code}")
    else:
        _set_rule(
            d, "id_code", spec.id_prefix, f"典藏單位不在知識庫裡，用預設代碼 {spec.id_prefix}"
        )
    code = v.get("id_code") or spec.id_prefix
    from_source = re.sub(
        r"[^a-z0-9]", "", unicodedata.normalize("NFKC", v.get("source_id") or "").lower()
    )
    if from_source:
        _set_rule(d, "id_no", from_source, "取自館藏編號")
    else:
        no = _next_id(code, "art").split("-", 1)[1]
        _set_rule(d, "id_no", no, f"沒有館藏編號，用 {code} 的下一個編號")


# ---------------------------------------------------------------- 驗證、畫面
def _check(domain: str, key: str, values: dict, spec, others: list[dict]) -> tuple[str, str | None]:
    """回傳 (status, message)：ok／invalid／missing／empty（選填沒填）。"""
    value = values.get(key)
    required = spec.required or _required_if(domain, key, values)
    if value is None:
        if not required:
            return "empty", None
        if domain == "art" and not spec.required:
            return "missing", _required_if(domain, key, values)
        return "missing", "照片上讀不到，請對照照片填寫" if spec.read else "請填寫"
    if spec.kind == "number":
        lo = spec.min if spec.min is not None else float("-inf")
        hi = spec.max if spec.max is not None else float("inf")
        if not lo <= value <= hi:
            return "invalid", f"超出合理範圍（{lo:g}–{hi:g}）"
        return "ok", None
    options = _enum_options(domain).get(key, [])
    if spec.kind == "enum" and value not in options:
        return "invalid", "只能是 " + "、".join(options)
    if spec.kind == "url" and not URL.match(value):
        return "invalid", "要是 http:// 或 https:// 開頭的網址"
    if spec.pattern and not re.fullmatch(spec.pattern, value):
        return "invalid", f"{spec.label}的格式不符（{spec.pattern}）"
    return _duplicate(domain, key, values, others)


def _required_if(domain: str, key: str, values: dict) -> str | None:
    """畫作的條件必填：CC BY 4.0 要標示文字；填了介紹就要出處與授權。回傳原因。"""
    if domain != "art":
        return None
    if key == "image_attribution" and values.get("image_license") == "CC BY 4.0":
        return "照片授權是 CC BY 4.0，要填標示文字"
    if key == "description_attribution" and values.get("description_license") == "CC BY 4.0":
        return "介紹授權是 CC BY 4.0，要填標示文字"
    if key in ("description_source_url", "description_license") and values.get("description"):
        return "填了介紹，就要寫出處與授權"
    return None


def _duplicate(domain: str, key: str, values: dict, others: list[dict]) -> tuple[str, None | str]:
    value = values[key]
    if domain == "mfg" and key in ("part_no", "drawing_no"):
        dup = next((p for p in others if p.get(key) == value), None)
        if dup:
            return "invalid", f"知識庫已有這個料號／圖號：〈{dup['name']['zh']}〉（{dup['id']}）"
    if domain == "art":
        if key == "description" and len(value) < 20:
            return "invalid", f"介紹至少要 20 字（現在 {len(value)} 字）"
        if key == "id_no" and values.get("id_code"):
            aid = f"{values['id_code']}-{value}"
            if aid in _used_ids("art"):
                return "invalid", f"知識庫 ID {aid} 已經有人用了，請換一個編號"
        if key == "title" and values.get("artist"):
            dup = next(
                (
                    a
                    for a in others
                    if a["title"]["zh"] == value and a["artist"]["zh"] == values["artist"]
                ),
                None,
            )
            if dup:
                return "invalid", f"知識庫已有〈{value}〉（{dup['id']}），不用再建檔"
    return "ok", None


def _view(d: dict) -> dict:
    domain = d["domain"]
    spec, sugg, others = _spec(domain), _suggestions(domain), _kb_items(domain)
    fields, blockers = [], []
    for key, fs in spec.fields.items():
        status, message = _check(domain, key, d["values"], fs, others)
        if status in ("missing", "invalid"):
            blockers.append(fs.label)
        fields.append(
            {
                "key": key,
                "label": fs.label,
                "value": d["values"].get(key),
                "source": d["sources"].get(key),
                "note": d["notes"].get(key),
                "hint": None if fs.read else fs.hint,
                "group": fs.group,
                "read": fs.read,
                "required": fs.required or bool(_required_if(domain, key, d["values"])),
                "kind": fs.kind,
                "options": _enum_options(domain).get(key, []),
                "suggestions": sugg.get(key, []),
                "status": status,
                "message": message,
            }
        )
    did, status = d["draft_id"], d["status"]
    checks = list(d["checks"])
    checks.append(
        {
            "label": "欄位驗證",
            "ok": not blockers,
            "detail": "全部通過" if not blockers else "還要處理：" + "、".join(blockers),
        }
    )
    item_id = _item_id(d)
    return {
        "draft_id": did,
        "domain": domain,
        "status": status,
        "created_at": d["created_at"],
        "updated_at": d.get("updated_at"),
        "image_id": d["image_id"],
        "photo_url": f"/api/v1/intake/{did}/photo.jpg",
        "kb_image_url": f"/api/v1/intake/{did}/{DOMAINS[domain].image_name}",
        "item_id": item_id,
        "fields": fields,
        "checks": checks,
        "extraction": d["extraction"],
        "can_commit": status in EDITABLE and not blockers,
        "blockers": blockers,
        "commit": d.get("commit"),
        "item_url": f"{DOMAINS[domain].route}/{item_id}" if status == "done" else None,
        "egress": NO_EGRESS,
    }


# ---------------------------------------------------------------- 讀標題欄（圖紙）
def _prompt(spec: IntakeDomainSpec) -> tuple[list[dict], dict]:
    tpl = load_template(get_models_config().intake.prompt_version)
    lines, props = [], {}
    for key, fs in spec.fields.items():
        if not fs.read:
            continue
        line = f"- {key}（{fs.label}）：{fs.hint or fs.label}"
        if fs.kind == "number":
            line += "，只填數字"
            props[key] = {"type": ["number", "null"]}
        elif fs.kind == "enum":
            opts = _enum_options("mfg").get(key, [])
            line += "，只能是 " + "、".join(opts)
            props[key] = {"enum": [*opts, None]}
        else:
            props[key] = {"type": ["string", "null"]}
        lines.append(line)
    schema = {
        "type": "object",
        "properties": props,
        "required": list(props),
        "additionalProperties": False,
    }
    user = tpl["user"].replace("{{fields}}", "\n".join(lines))
    return [{"role": "system", "content": tpl["system"]}, {"role": "user", "content": user}], schema


def _parse(text: str) -> dict:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, flags=re.S)
        try:
            data = json.loads(m.group(0)) if m else {}
        except json.JSONDecodeError:
            data = {}
    return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------- 建立草稿（SSE）
async def intake_stream(
    image_id: str, request_id: str, domain: str | None = None
) -> AsyncIterator[str]:
    """照片建檔用到 Chinese-CLIP（辨識）與 Qwen3-VL（讀圖紙標題欄）；記憶體吃緊時先釋放其他模型。

    domain＝從哪一邊的頁面進來（圖紙頁 mfg、尋畫 art）；沒給就交給領域路由判斷。
    """
    models = {"clip"} if domain == "art" else {"clip", "qwen"}
    events = _intake_stream(image_id, request_id, domain)
    async for e in memory_guard.stream("intake", models, events):
        yield e


async def _intake_stream(image_id: str, request_id: str, hint: str | None) -> AsyncIterator[str]:
    t0 = time.perf_counter()
    s, cfg = get_settings(), get_models_config().intake

    def err(code: str, message: str, **extra) -> str:
        return sse("error", {"code": code, "message": message, "request_id": request_id, **extra})

    def stage(key: str, **extra) -> str:
        return sse("stage", {"stage": key, "label": STAGES[key], **extra})

    try:
        if hint is not None:
            _spec(hint)
        photo = load_image(load_upload(image_id))
    except AppError as e:
        yield err(e.code, e.message)
        return
    checks = []

    # 1. 模糊就擋（兩個領域同一個門檻）
    yield stage("sharpness")
    blur = round(page.blur_score(photo), 3)
    checks.append(
        {
            "label": "清晰度",
            "ok": blur <= cfg.max_blur,
            "detail": f"模糊程度 {blur:.2f}（{cfg.max_blur:.2f} 以下才收）",
        }
    )
    if blur > cfg.max_blur:
        yield err(
            "INTAKE_TOO_BLURRY",
            f"照片太模糊（模糊程度 {blur:.2f}，{cfg.max_blur:.2f} 以下才收）。"
            "看不清楚的照片建進知識庫，之後就認不出來，資料也容易抄錯；請拿穩、對焦後重拍。",
            blur=blur,
        )
        return

    # 2. 領域路由＋辨識：已收錄就不建檔
    yield stage("identify")
    found = await asyncio.to_thread(identify_any, image_id)
    route = found["route"]
    domain = hint or route["domain"]
    if hint and route["domain"] != hint and not route["uncertain"]:
        where = "工廠圖紙" if route["domain"] == "mfg" else "畫作"
        page_name = "「工廠圖紙」" if route["domain"] == "mfg" else "「尋畫」"
        yield err(
            "INTAKE_WRONG_DOMAIN",
            f"這張照片看起來是{where}，請到{page_name}的拍照建檔。",
            route=route,
        )
        return
    try:
        spec = _spec(domain)
    except AppError as e:
        yield err(e.code, e.message, route=route)
        return
    if domain == "art":
        result = found["artwork_result"] or await asyncio.to_thread(identify, image_id)
        if result["matched"]:
            a = get_store().get_artwork(result["best_artwork_id"])
            yield err(
                "INTAKE_ALREADY_IN_KB",
                f"知識庫已經有這幅畫：〈{a['title']['zh']}〉{a['artist']['zh']}（{a['id']}），不用再建檔。",
                artwork=artwork_summary(a),
            )
            return
        best = result["results"][0] if result["results"] else None
        detail = (
            f"最相近的是 {best['artwork']['id']}〈{best['artwork']['title_zh']}〉，"
            f"相似度 {best['score']:.2f}，沒有通過幾何驗證"
            if best
            else "知識庫沒有畫作"
        )
    else:
        result = found["drawing_result"] or await asyncio.to_thread(identify_drawing, image_id)
        if result["matched"]:
            part = get_store().get_part(result["best_part_id"])
            yield err(
                "INTAKE_ALREADY_IN_KB",
                f"知識庫已經有這張圖紙：〈{part['name']['zh']}〉{part['part_no']}"
                f"（{part['id']}），不用再建檔。",
                part=part_summary(part),
            )
            return
        best = result["results"][0] if result["results"] else None
        detail = (
            f"最相近的是 {best['part']['id']}〈{best['part']['name_zh']}〉，"
            f"相似度 {best['score']:.2f}，沒有通過幾何驗證"
            if best
            else "知識庫沒有圖紙"
        )
    checks.append({"label": f"知識庫還沒有{DOMAINS[domain].noun}", "ok": True, "detail": detail})

    draft_id = "intake_" + uuid.uuid4().hex[:16]
    values = dict.fromkeys(spec.fields)
    sources: dict[str, str | None] = dict.fromkeys(spec.fields)
    extraction = {"model": None, "strategy": None, "ms": None, "raw": None, "error": None}
    kb_image = photo  # 畫作：照片本身就是知識庫的圖（長邊 1024，和 kb/images 相同）

    if domain == "mfg":
        # 3. 拉正、對齊知識庫版面
        yield stage("page")
        kb_image = _fit_drawing(photo, cfg)
        if kb_image is None:
            yield err(
                "INTAKE_PAGE_NOT_FOUND",
                "找不到整張圖紙的四個角或完整的標題欄外框：請把整張圖紙（含下方的標題欄）拍進畫面、"
                "背景和紙張顏色要有對比、盡量拍正。照片建檔目前只收和知識庫圖紙同一種版面"
                "（三視圖＋下方標題欄）的圖紙。",
            )
            return
        checks.append(
            {"label": "對齊知識庫圖紙版面", "ok": True, "detail": "已拉正，標題欄外框對上版面"}
        )
        # 4. 讀標題欄（送原照片：實驗用的就是原照片，整理過的圖小字會變細）
        yield stage("read")
        async for piece in _read_title_block(photo, spec, values, sources, extraction):
            yield sse("token", {"text": piece})
        checks.append(_read_check(extraction, values))

    # 5. 規則、存草稿（畫作的欄位由人在跳出的表單填）
    yield stage("validate")
    d = {
        "draft_id": draft_id,
        "domain": domain,
        "status": "draft",
        "created_at": _now(),
        "image_id": image_id,
        "proposed_id": _next_id(spec.id_prefix, "mfg") if domain == "mfg" else None,
        "item_id": None,
        "blur": blur,
        "route": route,
        "checks": checks,
        "extraction": extraction,
        "values": values,
        "sources": sources,
        "notes": {},
        "commit": None,
    }
    _apply_rules(d)
    folder = s.intake_dir / draft_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "photo.jpg").write_bytes(to_jpeg_bytes(photo))
    if domain == "mfg":
        kb_image.convert("L").save(folder / "drawing.png", optimize=True)
    else:
        (folder / "image.jpg").write_bytes(to_jpeg_bytes(kb_image, 90))
    _save(d)
    log.info("intake_draft", extra={"fields": {"draft_id": draft_id, "domain": domain}})
    yield sse("draft", _view(d))
    yield sse(
        "done",
        {
            "request_id": request_id,
            "draft_id": draft_id,
            "latency_ms": {
                "read": extraction["ms"],
                "total": round((time.perf_counter() - t0) * 1000),
            },
            "egress": NO_EGRESS,
        },
    )


def _fit_drawing(photo, cfg):
    """拉正 → 去陰影 → 依標題欄外框對齊知識庫版面；對不上回 None。"""
    size = tuple(cfg.page_size)
    quad = page.find_page(photo)
    target = size[1] / size[0]
    if quad is None or abs(page.aspect(quad) - target) / target > cfg.page_aspect_tol:
        return None
    rect = page.clean_drawing(page.rectify(photo, quad, size))
    box = page.title_block_box(rect)
    return page.fit_layout(rect, box) if box else None


async def _read_title_block(photo, spec, values: dict, sources: dict, extraction: dict):
    """本地 Qwen3-VL 讀標題欄（hybrid → 本地備援），逐字回傳模型輸出。

    結果寫進 values、sources、extraction。
    """
    if get_settings().llm_mode == "mock":
        extraction["error"] = "LLM_MODE=mock：沒有讀照片，欄位請自己填"
        return
    cfg = get_models_config().intake
    messages, schema = _prompt(spec)
    url = "data:image/jpeg;base64," + base64.b64encode(to_jpeg_bytes(photo, 90)).decode()
    messages[-1]["content"] = [
        {"type": "image_url", "image_url": {"url": url}},
        {"type": "text", "text": messages[-1]["content"]},
    ]
    t_read, errors = time.perf_counter(), []
    for strategy in ("hybrid", "hybrid_fallback"):
        try:
            provider = get_provider(strategy)
            provider.max_tokens, provider.temperature = cfg.max_tokens, 0.0
            provider.response_format = {
                "type": "json_schema",
                "json_schema": {"name": "intake", "schema": schema, "strict": True},
            }
            answer = ""
            async for piece in provider.stream(messages):
                answer += piece
                yield piece
            extraction.update(
                model=provider.model,
                strategy=strategy,
                raw=answer[:1000],
                tokens={
                    "input": provider.usage.input_tokens,
                    "output": provider.usage.output_tokens,
                },
            )
            data = _parse(answer)
            for key, fs in spec.fields.items():
                if fs.read and (v := _normalize(key, data.get(key), fs.kind)) is not None:
                    values[key], sources[key] = v, MODEL
            break
        except ProviderUnavailable as e:
            errors.append(f"{strategy}：{e}")
            log.info(f"照片建檔：{strategy} 無法使用（{e}）")
    extraction["ms"] = round((time.perf_counter() - t_read) * 1000)
    if extraction["model"] is None:
        extraction["error"] = "本地模型無法使用（" + "；".join(errors) + "），欄位請對照照片自己填"


def _read_check(extraction: dict, values: dict) -> dict:
    fallback = extraction["strategy"] == "hybrid_fallback"
    return {
        "label": "讀標題欄",
        "ok": extraction["error"] is None,
        "detail": (
            extraction["error"]
            or f"{extraction['model']}，{extraction['ms'] / 1000:.0f} 秒，"
            f"讀到 {sum(v is not None for v in values.values())} 個欄位"
            + ("（主推論伺服器沒回應，改用本地備援模型）" if fallback else "")
        ),
    }


# ---------------------------------------------------------------- 人修改、捨棄
def get_draft(draft_id: str) -> dict:
    return _view(_load(draft_id))


def update_draft(draft_id: str, values: dict) -> dict:
    """人改過的欄位來源標成「人」，再重新套規則（人填過的欄位規則不動）。"""
    with _lock:
        d = _load(draft_id)
        spec = _spec(d["domain"])
        if d["status"] not in EDITABLE:
            raise AppError("INTAKE_CLOSED", "這份草稿已經收錄或正在收錄，不能再修改", 409)
        for key, raw in values.items():
            fs = spec.fields.get(key)
            if fs is None:
                raise AppError("VALIDATION_ERROR", f"沒有這個欄位：{key}", 422)
            v = _normalize(key, raw, fs.kind)
            if v != d["values"].get(key):
                d["values"][key], d["sources"][key] = v, HUMAN if v is not None else None
                d["notes"].pop(key, None)
        _apply_rules(d)
        _save(d)
    return _view(d)


def discard_draft(draft_id: str) -> None:
    with _lock:
        d = _load(draft_id)
        if d["status"] == "indexing":
            raise AppError("INTAKE_CLOSED", "這份草稿正在收錄、重建索引，不能捨棄", 409)
        shutil.rmtree(_dir(draft_id), ignore_errors=True)


# ---------------------------------------------------------------- 收錄
def _build_part(d: dict, part_id: str, account: Account) -> dict:
    v = d["values"]
    model = d["extraction"].get("model")
    today = date.today().isoformat()
    read_by = f"由本地 {model} 讀取標題欄與外形尺寸" if model else "由人依照片填寫"
    return {
        "id": part_id,
        "part_no": v["part_no"],
        "drawing_no": v["drawing_no"],
        "revision": v["revision"],
        "name": {"zh": v["name"], **({"en": v["name_en"]} if v.get("name_en") else {})},
        "category": v["category"],
        "material": v["material"],
        "density_g_cm3": v["density_g_cm3"],
        **({"surface": v["surface"]} if v.get("surface") else {}),
        "company": v["company"],
        "owner": v["owner"],
        "confidentiality": v["confidentiality"],
        "drawing": f"kb/drawings/{part_id}.png",
        "dimensions_mm": {k: v[k] for k in ("width", "depth", "height")},
        "descriptions": [
            {
                "topic": "建檔紀錄",
                "text": (
                    f"這張圖紙於 {today} 以照片建檔：{read_by}，{account.label}確認後收錄。"
                    "沒有標準 3D 模型，外形尺寸取自圖上標註，未估算重量。"
                ),
                "source": f"照片建檔 {d['draft_id']}",
            }
        ],
        "intake": {
            "method": "photo",
            "date": today,
            "draft_id": d["draft_id"],
            **({"model": model} if model else {}),
            "fields_from_model": [k for k, src in d["sources"].items() if src == MODEL],
            "confirmed_by": account.id,
        },
    }


def _tags(raw: str | None) -> list[str]:
    return [t.strip() for t in re.split(r"[、,，;；]", raw or "") if t.strip()]


def _build_artwork(d: dict, artwork_id: str, account: Account) -> dict:
    """畫作 JSON：沒填介紹就由系統依欄位寫一段「基本資料」（只寫事實，授權 CC0，出處是典藏頁）。"""
    v = d["values"]
    today = date.today().isoformat()
    image = {"path": f"kb/images/{artwork_id}.jpg", "license": v["image_license"]}
    if v.get("image_attribution"):
        image["attribution"] = v["image_attribution"]
    if v.get("description"):
        desc = {
            "lang": "zh",
            "topic": "介紹",
            "text": v["description"],
            "source_url": v["description_source_url"],
            "license": v["description_license"],
        }
        if v.get("description_attribution"):
            desc["attribution"] = v["description_attribution"]
    else:
        facts = "，".join(
            x for x in (v["artist"], v["date_text"], v.get("medium"), v.get("dimensions")) if x
        )
        desc = {
            "lang": "zh",
            "topic": "基本資料",
            "text": f"〈{v['title']}〉，{facts}，{v['collection']}典藏。"
            f"這筆資料於 {today} 以照片建檔，{account.label}確認後收錄。",
            "source_url": v["source_url"],
            "license": "CC0",
        }
    return {
        "id": artwork_id,
        **({"source_id": v["source_id"]} if v.get("source_id") else {}),
        "title": {"zh": v["title"], **({"en": v["title_en"]} if v.get("title_en") else {})},
        "artist": {"zh": v["artist"], **({"en": v["artist_en"]} if v.get("artist_en") else {})},
        "date_text": v["date_text"],
        **({"medium": v["medium"]} if v.get("medium") else {}),
        **({"dimensions": v["dimensions"]} if v.get("dimensions") else {}),
        "collection": v["collection"],
        "image": image,
        "source_url": v["source_url"],
        "descriptions": [desc],
        **({"style_tags": _tags(v.get("style_tags"))} if _tags(v.get("style_tags")) else {}),
        "intake": {
            "method": "photo",
            "date": today,
            "draft_id": d["draft_id"],
            "generated_description": not v.get("description"),
            "confirmed_by": account.id,
        },
    }


def commit_draft(draft_id: str, account: Account, request_id: str) -> dict:
    require(account, "kb_intake", "收錄照片建檔的資料")
    with _lock:
        d = _load(draft_id)
        domain = d["domain"]
        dom = DOMAINS[domain]
        if d["status"] not in EDITABLE:
            raise AppError("INTAKE_CLOSED", "這份草稿已經收錄或正在收錄", 409)
        if _busy["draft_id"]:
            raise AppError("INTAKE_BUSY", "另一筆資料正在收錄、重建索引，請稍候再按", 409)
        view = _view(d)
        if not view["can_commit"]:
            raise AppError(
                "INTAKE_INVALID", "還有欄位沒通過驗證：" + "、".join(view["blockers"]), 422
            )
        if domain == "mfg":
            item_id = _next_id(_spec("mfg").id_prefix, "mfg")
            item = _build_part(d, item_id, account)
            problems = part_problems(item)
            name = f"〈{item['name']['zh']}〉{item['part_no']}"
        else:
            item_id = _item_id(d)
            item = _build_artwork(d, item_id, account)
            problems = artwork_problems(item)
            name = f"〈{item['title']['zh']}〉{item['artist']['zh']}"
        if problems:
            raise AppError("INTAKE_INVALID", "；".join(problems[:3]), 422)
        json_path = KB_DIR / dom.folder / f"{item_id}.json"
        image_path = KB_DIR / dom.image_dir / f"{item_id}{Path(dom.image_name).suffix}"
        version_path = KB_DIR / "VERSION"
        old_version = version_path.read_text(encoding="utf-8")
        shutil.copyfile(_dir(draft_id) / dom.image_name, image_path)
        json_path.write_text(
            json.dumps(item, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        new_version = _pipeline("bump_version").next_version(old_version)
        version_path.write_text(new_version + "\n", encoding="utf-8")
        d.update(
            status="indexing",
            item_id=item_id,
            commit={
                "by": account.id,
                "by_label": account.label,
                "at": _now(),
                "kb_version": new_version,
                "index_ms": None,
                "error": None,
            },
        )
        _save(d)
        _busy["draft_id"] = draft_id
    get_production_repo().add_audit(
        {"actor_id": account.id, "actor_label": account.label, "action": "寫入",
         "op": "kb_intake", "ref_no": item_id, "summary": f"照片建檔收錄 {item_id}{name}",
         "detail": {"draft_id": draft_id, "domain": domain, "kb_version": new_version},
         "request_id": request_id}
    )  # fmt: skip
    _start(_finish, draft_id, domain, item_id, [json_path, image_path], version_path, old_version)
    return _view(d)


def _start(fn, *args) -> None:
    """背景執行（測試改成直接呼叫）。"""
    threading.Thread(target=fn, args=args, daemon=True, name="intake-index").start()


def _rebuild_index() -> None:
    """同一個行程裡重建索引（沿用已載入的 Chinese-CLIP、bge-m3），再換上新索引。"""
    with memory_guard.use("intake_index", {"clip", "bge"}):
        rc = _pipeline("build_index").main([])
    get_store().maybe_reload()
    if rc != 0:
        log.info(f"照片建檔：重建索引回傳 {rc}（知識庫有其他問題，見 /health）")


def _in_index(domain: str, item_id: str) -> bool:
    store = get_store()
    return (store.get_part if domain == "mfg" else store.get_artwork)(item_id) is not None


def _finish(
    draft_id: str, domain: str, item_id: str, written: list[Path], version_path: Path, old: str
) -> None:
    t0, error = time.perf_counter(), None
    try:
        _rebuild_index()
        if not _in_index(domain, item_id):
            error = "新的資料沒有進索引（知識庫驗證沒過，見 /health 的問題清單）"
    except (Exception, SystemExit) as e:  # noqa: BLE001 — 建索引失敗要還原，不能讓執行緒默默結束
        error = f"重建索引失敗：{e}"
    if error:
        # 還原：刪掉寫進去的檔案、版本改回去，再建一次索引讓知識庫與索引一致
        for p in written:
            p.unlink(missing_ok=True)
        version_path.write_text(old, encoding="utf-8")
        try:
            _rebuild_index()
        except (Exception, SystemExit) as e:  # noqa: BLE001
            error += f"；還原後重建也失敗：{e}"
        log.error(f"照片建檔 {draft_id} 收錄失敗：{error}")
    with _lock:
        d = _load(draft_id)
        d["status"] = "failed" if error else "done"
        d["commit"].update(index_ms=round((time.perf_counter() - t0) * 1000), error=error)
        if error:
            d["item_id"] = None
        _save(d)
        _busy["draft_id"] = None
