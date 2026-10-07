"""prompt 組裝（app/rag/prompt.py）：圖片放在文字前面、問題放在最後。

推論伺服器（Ollama、llama-server）會沿用「和前一個 prompt 開頭相同的那段」的計算結果；
同一張圖的追問只有問題不同，圖放前面才能沿用約 1,070 token 的圖片。
"""

import asyncio

import pytest

from app.rag.prompt import build_messages, load_template
from app.rag.providers import MockProvider

ARTWORK = {
    "title": {"zh": "谿山行旅圖"},
    "artist": {"zh": "范寬"},
    "date_text": "北宋",
    "medium": "絹本水墨",
    "collection": "國立故宮博物院",
}
SOURCES = [
    {"ref": 1, "title": "谿山行旅圖", "topic": "落款與發現", "text": "簽名藏在右下方的樹叢裡。"}
]
IMAGE = b"\xff\xd8\xff\xe0fake-jpeg"


def test_image_comes_before_text_and_question_is_last():
    content = build_messages("簽名在哪？", ARTWORK, SOURCES, IMAGE)[-1]["content"]
    assert [p["type"] for p in content] == ["image_url", "text"]
    assert content[-1]["text"].endswith("簽名在哪？")


def test_followups_on_same_image_share_everything_before_the_text():
    a = build_messages("簽名在哪？", ARTWORK, SOURCES, IMAGE)
    b = build_messages("作者是誰？", ARTWORK, SOURCES, IMAGE)
    assert a[0] == b[0]  # system
    assert a[-1]["content"][0] == b[-1]["content"][0]  # 圖片


def test_without_image_only_text():
    content = build_messages("簽名在哪？", ARTWORK, SOURCES, None)[-1]["content"]
    assert [p["type"] for p in content] == ["text"]


def test_mock_provider_reads_text_part_after_image():
    messages = build_messages("簽名在哪？", ARTWORK, SOURCES, IMAGE)

    async def run():
        return "".join([t async for t in MockProvider(strategy="mock", model="m").stream(messages)])

    answer = asyncio.run(run())
    assert "簽名藏在右下方的樹叢裡" in answer and "[1]" in answer


@pytest.mark.parametrize("version", ["answer_v3", "answer_v4"])
def test_image_rule_only_when_image_attached(version):
    """answer_v3 起（docs/adr/026）：[畫面] 規則只放在有附圖的 system；沒附圖時和 answer_v2 相同。

    只有一份「有圖」的規則時，沒附圖模型也會說「畫面上…」並標 [畫面]，自己編答案。
    """
    tpl, v2 = load_template(version), load_template("answer_v2")
    assert "[畫面]" in tpl["system_image"] and "[畫面]" not in tpl["system"]
    assert tpl["system"] == v2["system"] and tpl["user"] == v2["user"]
    assert v2["system_image"] == v2["system"]  # 沒有 SYSTEM_IMAGE 段的模板兩邊一樣
