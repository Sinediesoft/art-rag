"""文字切塊：以段落為界，不跨段落；超過 max_chars 的段落再切並保留重疊（共用層 §三）。"""

from app.core.config import get_models_config


def split_paragraph(text: str) -> list[str]:
    cfg = get_models_config().chunking
    max_chars, overlap = cfg["max_chars"], cfg["overlap_chars"]
    text = text.strip()
    if len(text) <= max_chars:
        return [text]
    pieces, start = [], 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        # 盡量在句號處斷開
        cut = max(text.rfind(p, start + cfg["min_chars"], end) for p in "。！？")
        if end < len(text) and cut > start:
            end = cut + 1
        pieces.append(text[start:end])
        if end >= len(text):
            break
        start = end - overlap
    return pieces


def metadata_text(a: dict) -> str:
    """把畫作基本資料組成一段可檢索、可引用的文字。"""
    title = a["title"]["zh"] + (f"（{a['title']['en']}）" if a["title"].get("en") else "")
    artist = a["artist"]["zh"] + (f"（{a['artist']['en']}）" if a["artist"].get("en") else "")
    parts = [f"〈{title}〉的作者是{artist}，創作年代為{a['date_text']}"]
    if a.get("medium"):
        parts.append(f"材質為{a['medium']}")
    if a.get("dimensions"):
        parts.append(f"尺寸 {a['dimensions']}")
    parts.append(f"現藏於{a['collection']}")
    text = "，".join(parts) + "。"
    if a.get("style_tags"):
        text += "風格關鍵字：" + "、".join(a["style_tags"]) + "。"
    return text


def build_chunks(a: dict) -> list[dict]:
    chunks = [
        {
            "chunk_id": f"{a['id']}#meta",
            "artwork_id": a["id"],
            "lang": "zh",
            "topic": "基本資料",
            "text": metadata_text(a),
            "source_url": a["source_url"],
            "license": "CC0",
        }
    ]
    for i, d in enumerate(a["descriptions"]):
        for j, piece in enumerate(split_paragraph(d["text"])):
            chunks.append(
                {
                    "chunk_id": f"{a['id']}#{i:02d}-{j}",
                    "artwork_id": a["id"],
                    "lang": d["lang"],
                    "topic": d.get("topic", ""),
                    "text": piece,
                    "source_url": d["source_url"],
                    "license": d["license"],
                }
            )
    return chunks


# ---------------------------------------------------------------- 工廠圖紙
def part_metadata_text(p: dict) -> str:
    """零件基本資料＋由標準 3D 模型算出的外形尺寸與重量，組成一段可檢索、可引用的文字。"""
    g = p["geometry"]
    name = p["name"]["zh"] + (f"（{p['name']['en']}）" if p["name"].get("en") else "")
    text = (
        f"〈{name}〉料號 {p['part_no']}，圖號 {p['drawing_no']}，版次 {p['revision']}，"
        f"類別為{p['category']}，材料為{p['material']}"
    )
    if p.get("surface"):
        text += f"，表面處理：{p['surface']}"
    text += (
        f"。外形尺寸 {g['width']:g} × {g['depth']:g} × {g['height']:g} mm（寬 × 深 × 高），"
        f"依標準 3D 模型計算體積 {g['volume_mm3']:,.0f} mm³，"
        f"以密度 {p['density_g_cm3']} g/cm³ 估算重量約 {g['weight_kg']:.3f} kg。"
        f"負責單位：{p['owner']}；機密等級：{p['confidentiality']}。"
    )
    if p.get("tags"):
        text += "關鍵字：" + "、".join(p["tags"]) + "。"
    return text


def build_part_chunks(p: dict) -> list[dict]:
    """零件段落沒有網址出處：source_url 為 None，改用 source（內部文件名稱與版次）。"""
    base = {"part_id": p["id"], "lang": "zh", "source_url": None, "license": p["confidentiality"]}
    chunks = [
        {
            **base,
            "chunk_id": f"{p['id']}#meta",
            "topic": "基本資料",
            "text": part_metadata_text(p),
            "source": f"圖紙 {p['drawing_no']} rev.{p['revision']}＋標準 3D 模型",
        }
    ]
    for i, d in enumerate(p["descriptions"]):
        for j, piece in enumerate(split_paragraph(d["text"])):
            chunks.append(
                {
                    **base,
                    "chunk_id": f"{p['id']}#{i:02d}-{j}",
                    "topic": d["topic"],
                    "text": piece,
                    "source": d["source"],
                }
            )
    return chunks
