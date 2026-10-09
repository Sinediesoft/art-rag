"""畫面區域的草稿與收錄（docs/adr/030 第 2 步）：
藝術家在畫上圈一塊、寫解說 → 草稿 → 主管收錄進 kb/。

- 送草稿要 kb_annotate（藝術家），而且只能標 access.yaml 分給自己的畫
  （accounts.<id>.artworks）。
- 解說之後會原文進 prompt：送出、收錄前都過輸入防護的地端規則（docs/adr/014），
  像在對 AI 下指令、要內部資料就擋。
- 收錄要 kb_intake（主管，和照片建檔同一個權限）：區域與段落寫進 kb/artworks/<id>.json、
  遞增 kb/VERSION、背景重建索引；新區域沒進索引就把 JSON 和版本還原。
  改 kb/、重建索引和照片建檔共用同一把鎖（intake_service._lock／_busy），一次只做一筆。
草稿存在 data/regions/<draft_id>.json；處理完的（已收錄、退回）和上傳照片同一個保存期限，
待收錄的不刪。
"""

import copy
import json
import re
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from PIL import Image

from app.agent import guard
from app.core.config import get_settings
from app.core.errors import AppError
from app.core.logging import log
from app.rag.kb import artwork_problems, region_problems
from app.repositories.index_store import get_store
from app.repositories.production_repo import get_production_repo
from app.services import intake_service as intake
from app.services.identity import Account, require

DRAFT_ID = re.compile(r"^region_[0-9a-f]{16}$")
COMMITTABLE = {"pending", "failed"}  # 收錄失敗（已還原）可以再收錄
DONE = {"done", "returned"}  # 處理完：保存期限到了就刪


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _root() -> Path:
    return get_settings().data_dir / "regions"


def _path(draft_id: str) -> Path:
    if not DRAFT_ID.match(draft_id):
        raise AppError("REGION_DRAFT_NOT_FOUND", "找不到這份區域解說草稿", 404)
    return _root() / f"{draft_id}.json"


def _load(draft_id: str) -> dict:
    path = _path(draft_id)
    if not path.is_file():
        raise AppError("REGION_DRAFT_NOT_FOUND", "找不到這份區域解說草稿，可能已超過保存期限", 404)
    return json.loads(path.read_text(encoding="utf-8"))


def _save(d: dict) -> None:
    d["updated_at"] = _now()
    path = _path(d["draft_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def _all() -> list[dict]:
    root = _root()
    if not root.is_dir():
        return []
    drafts = [json.loads(p.read_text(encoding="utf-8")) for p in root.glob("region_*.json")]
    return sorted(drafts, key=lambda d: d["created_at"], reverse=True)


def purge_drafts(ttl_days: int) -> int:
    """刪掉處理完（已收錄、退回）、超過保存期限的草稿；待收錄、收錄中、收錄失敗的留著等主管處理。"""
    cutoff = (datetime.now(UTC) - timedelta(days=ttl_days)).isoformat()
    n = 0
    for d in _all():
        if d["status"] in DONE and d["updated_at"] < cutoff:
            _path(d["draft_id"]).unlink(missing_ok=True)
            n += 1
    return n


# ---------------------------------------------------------------- 知識庫 JSON
def _kb_json(artwork_id: str) -> Path:
    # 每次呼叫才讀 intake.KB_DIR：測試改成暫存資料夾時，照片建檔和區域解說寫到同一個地方
    return intake.KB_DIR / "artworks" / f"{artwork_id}.json"


def _read_artwork(artwork_id: str) -> dict:
    path = _kb_json(artwork_id)
    if not path.is_file():
        raise AppError("ARTWORK_NOT_FOUND", f"找不到畫作 {artwork_id}", 404)
    return json.loads(path.read_text(encoding="utf-8"))


def _image_path(a: dict) -> Path:
    # image.path 是 kb/images/…；知識庫目錄換過（測試）時改從 KB_DIR 找
    return intake.KB_DIR / Path(a["image"]["path"]).relative_to("kb")


def _image_size(a: dict) -> list[int]:
    with Image.open(_image_path(a)) as im:
        return list(im.size)


def _next_region_id(a: dict) -> str:
    used = {r["id"] for r in a.get("regions", {}).get("items", [])}
    n = 1
    while f"r{n}" in used:
        n += 1
    return f"r{n}"


def _merge(a: dict, d: dict, region_id: str, reviewer: str = "主管") -> dict:
    """把草稿的區域與解說加進畫作 JSON（回傳新的一份，不改原本的）。"""
    a = copy.deepcopy(a)
    regions = a.setdefault("regions", {"image_size": d["image_size"], "items": []})
    regions["items"].append({"id": region_id, "label": d["label"], "points": d["points"]})
    desc = {
        "lang": "zh",
        # 藝術家常用第一人稱寫（「我第一次看到時…」）：主題標出是誰的解說，
        # 模型才不會把話算到畫家頭上
        "topic": f"{d['label']}（{d['by_label']}的解說）",
        "region": region_id,
        "text": d["text"],
        "speaker": d["by_label"],
        "source": f"{d['by_label']}親自標註（{d['created_at'][:10]}，{reviewer}收錄）",
        "license": d["license"],
    }
    if d.get("attribution"):
        desc["attribution"] = d["attribution"]
    a["descriptions"].append(desc)
    return a


def _scalar(x) -> bool:
    return not isinstance(x, (dict, list))


def dump_kb_json(obj, indent: int = 0) -> str:
    """縮排 2 格的 JSON，照手寫知識庫檔的排法：只有純量（或純量陣列）的陣列寫成一行
    （區域座標、style_tags），只有純量、一行寫得下（80 字內）的物件也寫成一行（title、artist）。
    沒改到的部分和原檔一字不差，收錄的 diff 只有新加的區域與段落。"""
    pad, inner = "  " * indent, "  " * (indent + 1)
    if isinstance(obj, dict):
        if not obj:
            return "{}"
        if all(map(_scalar, obj.values())):
            one = ", ".join(
                f"{json.dumps(k, ensure_ascii=False)}: {dump_kb_json(v)}" for k, v in obj.items()
            )
            if len(one) + 4 <= 80:
                return "{ " + one + " }"
        rows = [
            f"{inner}{json.dumps(k, ensure_ascii=False)}: {dump_kb_json(v, indent + 1)}"
            for k, v in obj.items()
        ]
        return "{\n" + ",\n".join(rows) + f"\n{pad}}}"
    if isinstance(obj, list):
        if all(_scalar(x) or (isinstance(x, list) and all(map(_scalar, x))) for x in obj):
            return json.dumps(obj, ensure_ascii=False, separators=(", ", ": "))
        rows = [f"{inner}{dump_kb_json(x, indent + 1)}" for x in obj]
        return "[\n" + ",\n".join(rows) + f"\n{pad}]"
    return json.dumps(obj, ensure_ascii=False)


# ---------------------------------------------------------------- 檢查
def _area(points: list[list[float]]) -> float:
    n = len(points)
    s = sum(
        points[i][0] * points[(i + 1) % n][1] - points[(i + 1) % n][0] * points[i][1]
        for i in range(n)
    )
    return abs(s) / 2


def _require_scope(account: Account, artwork_id: str) -> None:
    require(account, "kb_annotate", "在畫上圈區域、寫解說")
    if artwork_id not in account.artworks:
        mine = "、".join(account.artworks) or "沒有"
        raise AppError(
            "PERMISSION_DENIED",
            f"「{account.label}」只能標分給自己的畫（{mine}），不能標 {artwork_id}。",
            403,
        )


def _check(d: dict, a: dict) -> None:
    """送出與收錄都跑：注入掃描、授權標示、合進畫作 JSON 之後的 schema 與區域規則。"""
    risk = guard.passage_risk(f"{d['label']}\n{d['text']}")
    if risk:
        raise AppError("REGION_REJECTED", f"解說沒有通過輸入防護：{risk}。請改寫後再送。", 422)
    if d["license"] == "CC BY 4.0" and not (d.get("attribution") or "").strip():
        raise AppError("VALIDATION_ERROR", "選 CC BY 4.0 要填署名（標示文字）", 422)
    if _area(d["points"]) < 1e-4:
        raise AppError("VALIDATION_ERROR", "圈的範圍太小（點幾乎在同一條線上），請重新圈", 422)
    merged = _merge(a, d, _next_region_id(a))
    problems = artwork_problems(merged) + region_problems(merged, _image_path(merged))
    if problems:
        raise AppError("REGION_REJECTED", "；".join(problems[:3]), 422)


# ---------------------------------------------------------------- 對外
def create_draft(artwork_id: str, body: dict, account: Account, request_id: str) -> dict:
    _require_scope(account, artwork_id)
    a = _read_artwork(artwork_id)
    now = _now()
    d = {
        "draft_id": f"region_{uuid.uuid4().hex[:16]}",
        "artwork_id": artwork_id,
        "artwork_title": a["title"]["zh"],
        "status": "pending",
        "label": body["label"].strip(),
        "points": [[round(x, 4), round(y, 4)] for x, y in body["points"]],
        "text": body["text"].strip(),
        "license": body["license"],
        "attribution": (body.get("attribution") or "").strip() or None,
        "image_size": _image_size(a),
        "by": account.id,
        "by_label": account.label,
        "created_at": now,
        "updated_at": now,
        "review": None,
        "commit": None,
    }
    _check(d, a)
    _save(d)
    _audit(
        account,
        "申請",
        "kb_annotate",
        d,
        f"送出區域解說〈{d['artwork_title']}〉「{d['label']}」",
        request_id,
    )
    return d


def list_drafts(account: Account) -> dict:
    drafts = _all()
    can_commit = account.can("kb_intake")
    return {
        "pending": [d for d in drafts if d["status"] in COMMITTABLE | {"indexing"}]
        if can_commit
        else [],
        "mine": [d for d in drafts if d["by"] == account.id],
        "can_commit": can_commit,
    }


def pending_count() -> int:
    """頁首「待核准 N 件」用：待收錄與收錄失敗待重試的草稿。"""
    return sum(d["status"] in COMMITTABLE for d in _all())


def withdraw_draft(draft_id: str, account: Account) -> None:
    """送出的人撤回還沒收錄的草稿（直接刪掉）。"""
    with intake._lock:
        d = _load(draft_id)
        if d["by"] != account.id:
            raise AppError("PERMISSION_DENIED", "只能撤回自己送出的草稿", 403)
        if d["status"] not in COMMITTABLE | {"returned"}:
            raise AppError("REGION_DRAFT_CLOSED", "這份草稿已經收錄或正在收錄，不能撤回", 409)
        _path(draft_id).unlink(missing_ok=True)


def return_draft(draft_id: str, reason: str, account: Account, request_id: str) -> dict:
    require(account, "kb_intake", "退回區域解說")
    with intake._lock:
        d = _load(draft_id)
        if d["status"] not in COMMITTABLE:
            raise AppError("REGION_DRAFT_CLOSED", "這份草稿已經收錄、退回或正在收錄", 409)
        d.update(
            status="returned",
            review={
                "by": account.id,
                "by_label": account.label,
                "at": _now(),
                "reason": reason.strip(),
            },
        )
        _save(d)
    _audit(
        account,
        "退回",
        "kb_intake",
        d,
        f"退回區域解說〈{d['artwork_title']}〉「{d['label']}」：{reason}",
        request_id,
    )
    return d


def commit_draft(draft_id: str, account: Account, request_id: str) -> dict:
    require(account, "kb_intake", "收錄區域解說")
    with intake._lock:
        d = _load(draft_id)
        if d["status"] not in COMMITTABLE:
            raise AppError("REGION_DRAFT_CLOSED", "這份草稿已經收錄、退回或正在收錄", 409)
        if intake._busy["draft_id"]:
            raise AppError("INTAKE_BUSY", "另一筆資料正在收錄、重建索引，請稍候再按", 409)
        json_path = _kb_json(d["artwork_id"])
        a = _read_artwork(d["artwork_id"])
        if _image_size(a) != d["image_size"]:
            raise AppError(
                "REGION_REJECTED", "畫作的圖在送出之後換過了，座標會偏，請藝術家重新圈", 422
            )
        _check(d, a)  # 送出之後規則或知識庫可能變了：收錄前再驗一次
        region_id = _next_region_id(a)
        merged = _merge(a, d, region_id, account.label)
        version_path = intake.KB_DIR / "VERSION"
        old_json = json_path.read_text(encoding="utf-8")
        old_version = version_path.read_text(encoding="utf-8")
        json_path.write_text(dump_kb_json(merged) + "\n", encoding="utf-8", newline="\n")
        new_version = intake._pipeline("bump_version").next_version(old_version)
        version_path.write_text(new_version + "\n", encoding="utf-8", newline="\n")
        d.update(
            status="indexing",
            review={"by": account.id, "by_label": account.label, "at": _now(), "reason": None},
            commit={
                "by": account.id,
                "by_label": account.label,
                "at": _now(),
                "region_id": region_id,
                "kb_version": new_version,
                "index_ms": None,
                "error": None,
            },
        )
        _save(d)
        intake._busy["draft_id"] = draft_id
    _audit(
        account,
        "寫入",
        "kb_intake",
        d,
        f"收錄區域解說〈{d['artwork_title']}〉「{d['label']}」",
        request_id,
    )
    intake._start(_finish, draft_id, json_path, old_json, version_path, old_version)
    return d


def _in_index(artwork_id: str, region_id: str) -> bool:
    a = get_store().get_artwork(artwork_id) or {}
    return any(r["id"] == region_id for r in a.get("regions", {}).get("items", []))


def _finish(draft_id: str, json_path: Path, old_json: str, version_path: Path, old: str) -> None:
    t0, error = time.perf_counter(), None
    d = _load(draft_id)
    try:
        intake._rebuild_index()
        if not _in_index(d["artwork_id"], d["commit"]["region_id"]):
            error = "新的區域沒有進索引（知識庫驗證沒過，見 /health 的問題清單）"
    except (Exception, SystemExit) as e:  # noqa: BLE001 — 建索引失敗要還原，不能讓執行緒默默結束
        error = f"重建索引失敗：{e}"
    if error:
        # 還原：畫作 JSON 與版本改回去，再建一次索引讓知識庫與索引一致
        json_path.write_text(old_json, encoding="utf-8", newline="\n")
        version_path.write_text(old, encoding="utf-8", newline="\n")
        try:
            intake._rebuild_index()
        except (Exception, SystemExit) as e:  # noqa: BLE001
            error += f"；還原後重建也失敗：{e}"
        log.error(f"區域解說 {draft_id} 收錄失敗：{error}")
    with intake._lock:
        d = _load(draft_id)
        d["status"] = "failed" if error else "done"
        d["commit"].update(index_ms=round((time.perf_counter() - t0) * 1000), error=error)
        _save(d)
        intake._busy["draft_id"] = None


def _audit(account: Account, action: str, op: str, d: dict, summary: str, request_id: str) -> None:
    get_production_repo().add_audit(
        {"actor_id": account.id, "actor_label": account.label, "action": action, "op": op,
         "ref_no": d["draft_id"], "summary": summary,
         "detail": {"artwork_id": d["artwork_id"], "label": d["label"], "status": d["status"],
                    "kb_version": (d.get("commit") or {}).get("kb_version")},
         "request_id": request_id}
    )  # fmt: skip
