"""知識庫讀取與驗證：建索引指令與 /health 共用。

兩個領域：畫作（kb/artworks/*.json＋kb/images/）與工廠機械加工圖（kb/parts/*.json＋kb/cad/＋kb/drawings/）。
"""

import hashlib
import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from app.core.config import REPO_ROOT


def _validator(schema: str) -> Draft202012Validator:
    path = REPO_ROOT / "shared" / "schemas" / schema
    return Draft202012Validator(
        json.loads(path.read_text(encoding="utf-8")), format_checker=FormatChecker()
    )


def _load(path: Path, validator: Draft202012Validator) -> tuple[dict | None, list[str]]:
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        return None, ["檔案含 BOM，請存成 UTF-8（無 BOM）"]
    data = json.loads(raw.decode("utf-8"))
    problems = [
        f"{'/'.join(map(str, e.absolute_path)) or '(root)'}: {e.message}"
        for e in validator.iter_errors(data)
    ]
    if data.get("id") and path.stem != data["id"]:
        problems.append(f"檔名 {path.name} 與 id {data['id']} 不一致")
    return data, problems


def validate_kb() -> tuple[list[dict], list[str]]:
    validator = _validator("artwork.schema.json")
    ok, errors = [], []
    for path in sorted((REPO_ROOT / "kb" / "artworks").glob("*.json")):
        data, problems = _load(path, validator)
        if data is not None:
            img = REPO_ROOT / data.get("image", {}).get("path", "")
            if not img.is_file():
                problems.append(f"找不到圖片 {data.get('image', {}).get('path')}")
            for item in [data.get("image", {}), *data.get("descriptions", [])]:
                if item.get("license") == "CC BY 4.0" and not item.get("attribution"):
                    problems.append("CC BY 4.0 的項目必須填 attribution（標示文字）")
        if problems:
            errors.extend(f"{path.name}: {p}" for p in problems)
        else:
            ok.append(data)
    return ok, errors


def validate_parts(check_drawing: bool = True) -> tuple[list[dict], list[str]]:
    """零件 JSON＋標準 CadQuery 模型＋圖紙。

    check_drawing=False 給 make drawings 用（圖紙還沒產生）。
    """
    validator = _validator("part.schema.json")
    ok, errors = [], []
    for path in sorted((REPO_ROOT / "kb" / "parts").glob("*.json")):
        data, problems = _load(path, validator)
        if data is not None:
            if not (REPO_ROOT / data.get("cad", "")).is_file():
                problems.append(f"找不到標準模型 {data.get('cad')}")
            if check_drawing and not (REPO_ROOT / data.get("drawing", "")).is_file():
                problems.append(f"找不到圖紙 {data.get('drawing')}（請先執行 make drawings）")
        if problems:
            errors.extend(f"{path.name}: {p}" for p in problems)
        else:
            ok.append(data)
    return ok, errors


def kb_hash(artworks: list[dict], parts: list[dict] | None = None) -> str:
    h = hashlib.sha256()
    for a in artworks:
        h.update(json.dumps(a, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        h.update((REPO_ROOT / a["image"]["path"]).read_bytes())
    for p in parts or []:
        h.update(json.dumps(p, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        h.update((REPO_ROOT / p["cad"]).read_bytes())
        h.update((REPO_ROOT / p["drawing"]).read_bytes())
    return h.hexdigest()[:16]


def current_kb_hash() -> str:
    return kb_hash(validate_kb()[0], validate_parts()[0])
