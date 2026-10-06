"""共用 prompt 模板組裝：三種策略用同一份（shared/prompts/<version>.md）。

畫作用 answer_v2，工廠圖紙用 drawing_v2（models.yaml 的 prompt.version／prompt.drawing_version）。
"""

import base64
import re
from functools import lru_cache

from app.core.config import get_models_config, get_settings

NO_CONTEXT = (
    "（本次關閉檢索，沒有提供參考資料。請依你自己的知識回答，並在句尾標註 [0] 表示無來源。）"
)
NO_CARD = "（未提供畫作資料，請從照片判斷）"


@lru_cache
def load_template(version: str) -> dict[str, str]:
    raw = (get_settings().shared_dir / "prompts" / f"{version}.md").read_text(encoding="utf-8")
    raw = re.sub(r"<!--.*?-->", "", raw, flags=re.S)
    _, system, user = re.split(r"^===(?:SYSTEM|USER)===\s*$", raw, flags=re.M)
    return {"system": system.strip(), "user": user.strip()}


def prompt_version(domain: str = "art") -> str:
    p = get_models_config().prompt
    return p.get("drawing_version", "drawing_v2") if domain == "mfg" else p["version"]


def artwork_card(a: dict | None) -> str:
    if not a:
        return "（未指定畫作，請從參考資料中判斷）"
    return (
        f"〈{a['title']['zh']}〉，{a['artist']['zh']}，{a['date_text']}，"
        f"{a.get('medium', '')}，{a['collection']}"
    )


def part_card(p: dict | None) -> str:
    if not p:
        return "（未指定圖紙，請從參考資料中判斷）"
    g = p["geometry"]
    return (
        f"〈{p['name']['zh']}〉，料號 {p['part_no']}，圖號 {p['drawing_no']} 版次 {p['revision']}，"
        f"{p['material']}，外形 {g['width']:g}×{g['depth']:g}×{g['height']:g} mm，"
        f"機密等級：{p['confidentiality']}"
    )


def format_context(sources: list[dict]) -> str:
    return "\n".join(f"[{s['ref']}]（〈{s['title']}〉・{s['topic']}）{s['text']}" for s in sources)


def build_messages(
    question: str,
    artwork: dict | None,
    sources: list[dict],
    image_jpeg: bytes | None,
    use_retrieval: bool = True,
    include_card: bool = True,
    domain: str = "art",
) -> list[dict]:
    """include_card=False 給 A1 對照組（api_nokb）：只送照片與問題，不帶任何知識庫內容。

    domain="mfg" 時 artwork 傳的是零件，改用圖紙模板與零件卡。
    """
    tpl = load_template(prompt_version(domain))
    card = part_card(artwork) if domain == "mfg" else artwork_card(artwork)
    system = tpl["system"]
    if not use_retrieval:
        # 檢索增益對照組：同模型、同問題，只拿掉參考資料與「只根據資料」規則
        system = system.split("\n")[0] + "\n請用繁體中文（台灣用語）簡潔回答 2–5 句。"
    user_text = (
        tpl["user"]
        .replace("{{artwork_card}}", card if include_card else NO_CARD)
        .replace("{{context}}", format_context(sources) if use_retrieval else NO_CONTEXT)
        .replace("{{question}}", question)
    )
    # 圖放在文字前面：推論伺服器會沿用和前一個 prompt 開頭相同那段的計算，同一張圖的追問
    # 只重算文字，不必每題重算約 1,070 token 的圖（GTX 1650 實測每題 22–29 秒 → 7–12 秒）
    content: list[dict] = []
    if image_jpeg:
        b64 = base64.b64encode(image_jpeg).decode()
        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
    content.append({"type": "text", "text": user_text})
    return [{"role": "system", "content": system}, {"role": "user", "content": content}]
