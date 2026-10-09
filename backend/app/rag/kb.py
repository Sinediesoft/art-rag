"""知識庫讀取與驗證：建索引指令與 /health 共用。

兩個領域：畫作（kb/artworks/*.json＋kb/images/）與工廠機械加工圖（kb/parts/*.json＋kb/cad/＋kb/drawings/）；
另有工廠庫存示範資料（kb/inventory/，Text-to-SQL 的資料庫來源）與生產排程設定
（kb/production/：機台、行事曆、一張圖紙一份製程途程）。
"""

import hashlib
import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from PIL import Image

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


# 區域面積下限（相對整張圖）：低於這個大概是點在同一條線上，或手滑點了兩下
_MIN_REGION_AREA = 1e-4


def _polygon_area(points: list[list[float]]) -> float:
    """鞋帶公式；點的順序順時針、逆時針都可以。"""
    n = len(points)
    s = sum(
        points[i][0] * points[(i + 1) % n][1] - points[(i + 1) % n][0] * points[i][1]
        for i in range(n)
    )
    return abs(s) / 2


def region_problems(data: dict, image: Path | None = None) -> list[str]:
    """區域標註（docs/adr/029）：段落指到的區域要存在、每塊區域至少有一段、多邊形不能退化。

    image：要比對尺寸的圖檔。座標綁在圈區域時的那張圖上，換過圖（重裁、換高解析度）尺寸會不同，
    區域就要重新圈；照片建檔收錄前圖還沒寫，傳 None 不比。
    """
    regions = data.get("regions", {})
    items = regions.get("items", [])
    ids = [r.get("id") for r in items]
    problems = [f"區域 id 重複：{i}" for i in sorted({i for i in ids if ids.count(i) > 1})]
    used = {d["region"] for d in data.get("descriptions", []) if d.get("region")}
    problems += [f"段落指向區域 {i}，但 regions 裡沒有這一塊" for i in sorted(used - set(ids))]
    problems += [f"區域 {i} 沒有任何段落講它" for i in ids if i not in used]
    for r in items:
        pts = r.get("points", [])
        well_formed = len(pts) >= 3 and all(isinstance(p, list) and len(p) == 2 for p in pts)
        if well_formed and _polygon_area(pts) < _MIN_REGION_AREA:  # 格式不對的交給 schema 報
            problems.append(f"區域 {r.get('id')} 的面積太小（點幾乎在同一條線上）")
    if regions and image is not None and image.is_file():
        with Image.open(image) as im:
            size = list(im.size)
        if regions.get("image_size") != size:
            drawn = "×".join(map(str, regions.get("image_size") or []))
            problems.append(
                f"圖檔現在是 {size[0]}×{size[1]}，圈區域時是 {drawn}：換過圖，區域要重新圈"
                "（只是等比例縮放的話，確認後把 regions.image_size 改成新尺寸）"
            )
    return problems


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
            problems += region_problems(data, img)
        if problems:
            errors.extend(f"{path.name}: {p}" for p in problems)
        else:
            ok.append(data)
    return ok, errors


def _file_problems(data: dict, check_drawing: bool) -> list[str]:
    problems = []
    # 照片建檔的零件沒有標準模型（docs/adr/013）：沒填 cad 就不檢查，schema 會要求 dimensions_mm
    if "cad" in data and not (REPO_ROOT / data["cad"]).is_file():
        problems.append(f"找不到標準模型 {data['cad']}")
    if check_drawing and not (REPO_ROOT / data.get("drawing", "")).is_file():
        problems.append(f"找不到圖紙 {data.get('drawing')}（請先執行 make drawings）")
    return problems


def validate_parts(check_drawing: bool = True) -> tuple[list[dict], list[str]]:
    """零件 JSON＋標準 CadQuery 模型（有填才檢查）＋圖紙。

    check_drawing=False 給 make drawings 用（圖紙還沒產生）。
    """
    validator = _validator("part.schema.json")
    ok, errors = [], []
    for path in sorted((REPO_ROOT / "kb" / "parts").glob("*.json")):
        data, problems = _load(path, validator)
        if data is not None:
            problems += _file_problems(data, check_drawing)
        if problems:
            errors.extend(f"{path.name}: {p}" for p in problems)
        else:
            ok.append(data)
    return ok, errors


def _schema_problems(data: dict, schema: str) -> list[str]:
    return [
        f"{'/'.join(map(str, e.absolute_path)) or '(root)'}: {e.message}"
        for e in _validator(schema).iter_errors(data)
    ]


def part_problems(data: dict) -> list[str]:
    """單筆零件 JSON 的 schema 問題（照片建檔收錄前用；不檢查檔案，檔案還沒寫）。"""
    return _schema_problems(data, "part.schema.json")


def artwork_problems(data: dict) -> list[str]:
    """單筆畫作 JSON 的 schema 與授權問題（同 validate_kb，不檢查圖檔，圖檔還沒寫）。"""
    problems = _schema_problems(data, "artwork.schema.json")
    for item in [data.get("image", {}), *data.get("descriptions", [])]:
        if item.get("license") == "CC BY 4.0" and not item.get("attribution"):
            problems.append("CC BY 4.0 的項目必須填 attribution（標示文字）")
    return problems + region_problems(data)


def validate_inventory(part_ids: set[str]) -> tuple[dict | None, list[dict], list[str]]:
    """工廠庫存示範資料（kb/inventory/）：JSON Schema＋帳務一致性。

    各倉異動加總＝目前庫存、流水帳不為負、訂單已出貨量＝銷貨出庫、工單完成量＝生產入庫、
    庫存狀態與倉庫類型相符（成品倉只放可用／保留、待檢區只放待檢、隔離區只放不良）。
    """
    root = REPO_ROOT / "kb" / "inventory"
    site_path = root / "site.json"
    if not site_path.is_file():
        return None, [], ["找不到 kb/inventory/site.json"]
    site, errors = _load(site_path, _validator("inventory_site.schema.json"))
    errors = [f"site.json: {e}" for e in errors]
    if errors or site is None:
        return None, [], errors
    kinds = {w["warehouse_id"]: w["kind"] for w in site["warehouses"]}
    allowed = {"成品": {"可用", "保留"}, "待檢": {"待檢"}, "隔離": {"不良"}}
    validator = _validator("inventory_item.schema.json")
    ok = []
    for path in sorted((root / "items").glob("*.json")):
        data, problems = _load(path, validator)
        if data is None or problems:
            errors.extend(f"{path.name}: {p}" for p in problems)
            continue
        if data["part_id"] != path.stem:
            problems.append(f"檔名 {path.name} 與 part_id {data['part_id']} 不一致")
        if data["part_id"] not in part_ids:
            problems.append(f"找不到對應的圖紙 kb/parts/{data['part_id']}.json")
        stock: dict[str, int] = {}
        for s in data["stock"]:
            wh = s["warehouse_id"]
            if wh not in kinds:
                problems.append(f"stock：未知倉庫 {wh}")
            elif s["status"] not in allowed[kinds[wh]]:
                problems.append(f"stock：{wh}（{kinds[wh]}）不能放狀態「{s['status']}」")
            stock[wh] = stock.get(wh, 0) + s["qty"]
        balance: dict[str, int] = {}
        for m in sorted(data["moves"], key=lambda m: m["moved_on"]):
            wh = m["warehouse_id"]
            if wh not in kinds:
                problems.append(f"moves：未知倉庫 {wh}")
            if m["moved_on"] > site["as_of"]:
                problems.append(f"moves：{m['moved_on']} 晚於資料日期 {site['as_of']}")
            balance[wh] = balance.get(wh, 0) + m["qty"]
            if balance[wh] < 0:
                problems.append(f"moves：{wh} 在 {m['moved_on']} 庫存變成負數")
        for wh in set(stock) | set(balance):
            if stock.get(wh, 0) != balance.get(wh, 0):
                problems.append(f"{wh} 目前庫存 {stock.get(wh, 0)} ≠ 異動加總 {balance.get(wh, 0)}")
        for so in {s["so_no"] for s in data["sales_orders"]}:
            shipped = sum(s["qty_shipped"] for s in data["sales_orders"] if s["so_no"] == so)
            out = -sum(
                m["qty"]
                for m in data["moves"]
                if m["move_type"] == "銷貨出庫" and m.get("ref_no") == so
            )
            if shipped != out:
                problems.append(f"{so} 已出貨 {shipped} ≠ 銷貨出庫 {out}")
        for s in data["sales_orders"]:
            expect = (
                "已出貨"
                if s["qty_shipped"] == s["qty"]
                else "部分出貨"
                if s["qty_shipped"]
                else "待出貨"
            )
            if s["qty_shipped"] > s["qty"] or s["status"] != expect:
                problems.append(f"{s['so_no']}-{s['line_no']} 狀態應為「{expect}」")
        for w in data["work_orders"]:
            done = sum(
                m["qty"]
                for m in data["moves"]
                if m["move_type"] == "生產入庫" and m.get("ref_no") == w["wo_no"]
            )
            if done != w["qty_done"] or w["qty_done"] > w["qty_planned"]:
                problems.append(f"{w['wo_no']} 完工 {w['qty_done']} ≠ 生產入庫 {done}")
            if (w["status"] == "已完工") != (w["qty_done"] == w["qty_planned"]):
                problems.append(f"{w['wo_no']} 狀態與完工數量不符")
        if problems:
            errors.extend(f"{path.name}: {p}" for p in problems)
        else:
            ok.append(data)
    return site, ok, errors


def validate_production(
    part_ids: set[str], items: list[dict] | None = None
) -> tuple[dict | None, dict[str, dict], list[str]]:
    """生產排程設定（kb/production/）：機台與行事曆、製程途程。

    另檢查：途程對應的圖紙存在、工序號遞增、機型都有機台；kb/inventory 工單的 line 必須是機台代碼。
    回傳 (site, {part_id: routing}, errors)。
    """
    root = REPO_ROOT / "kb" / "production"
    site_path = root / "site.json"
    if not site_path.is_file():
        return None, {}, ["找不到 kb/production/site.json"]
    site, errors = _load(site_path, _validator("production_site.schema.json"))
    errors = [f"production/site.json: {e}" for e in errors]
    if errors or site is None:
        return None, {}, errors
    types = {t["type"] for t in site["machine_types"]}
    ids = [m["machine_id"] for m in site["machines"]]
    if len(ids) != len(set(ids)):
        errors.append("production/site.json: machine_id 重複")
    for m in site["machines"]:
        if m["machine_type"] not in types:
            errors.append(
                f"production/site.json: {m['machine_id']} 的機型 {m['machine_type']}"
                " 不在 machine_types"
            )
    for t in types - {m["machine_type"] for m in site["machines"]}:
        errors.append(f"production/site.json: 機型 {t} 沒有任何機台")
    validator = _validator("routing.schema.json")
    routings: dict[str, dict] = {}
    for path in sorted((root / "routings").glob("*.json")):
        data, problems = _load(path, validator)
        if data is None or problems:
            errors.extend(f"routings/{path.name}: {p}" for p in problems)
            continue
        if data["part_id"] != path.stem:
            problems.append(f"檔名與 part_id {data['part_id']} 不一致")
        if data["part_id"] not in part_ids:
            problems.append(f"找不到對應的圖紙 kb/parts/{data['part_id']}.json")
        seqs = [o["op_seq"] for o in data["operations"]]
        if seqs != sorted(set(seqs)):
            problems.append("op_seq 必須遞增且不重複")
        for o in data["operations"]:
            if o["kind"] == "自製" and o["machine_type"] not in types:
                problems.append(f"工序 {o['op_seq']}：未知機型 {o['machine_type']}")
        if not any(o["kind"] == "自製" for o in data["operations"]):
            problems.append("至少要有一道自製工序")
        if problems:
            errors.extend(f"routings/{path.name}: {p}" for p in problems)
        else:
            routings[data["part_id"]] = data
    for it in items or []:
        for w in it["work_orders"]:
            if w["status"] != "已完工" and w["line"] not in ids:
                errors.append(
                    f"inventory/items/{it['part_id']}.json: {w['wo_no']} 的 line"
                    f" {w['line']} 不是機台代碼"
                )
    return site, routings, errors


def kb_hash(artworks: list[dict], parts: list[dict] | None = None) -> str:
    h = hashlib.sha256()
    for a in artworks:
        h.update(json.dumps(a, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        h.update((REPO_ROOT / a["image"]["path"]).read_bytes())
    for p in parts or []:
        h.update(json.dumps(p, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        if "cad" in p:
            h.update((REPO_ROOT / p["cad"]).read_bytes())
        h.update((REPO_ROOT / p["drawing"]).read_bytes())
    return h.hexdigest()[:16]


def current_kb_hash() -> str:
    return kb_hash(validate_kb()[0], validate_parts()[0])
