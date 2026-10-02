"""問答送給模型的圖：知識庫畫作送原圖（長邊 1024 px），不是網頁卡片用的 480 px 縮圖。

Ollama 會把圖換算成差不多的 token 數（縮圖約 1,060、原圖約 1,065），縮圖省不到時間，
模型反而看到縮小再放大的圖。
"""

import asyncio
import io

from PIL import Image

from app.core.config import REPO_ROOT
from app.services import chat_service


def sent_image_size(monkeypatch, **kwargs) -> tuple[int, int]:
    seen = {}
    real = chat_service.build_messages

    def spy(question, artwork, sources, image_jpeg, *args, **kw):
        seen["size"] = Image.open(io.BytesIO(image_jpeg)).size
        return real(question, artwork, sources, image_jpeg, *args, **kw)

    monkeypatch.setattr(chat_service, "build_messages", spy)

    async def run():
        return [e async for e in chat_service.chat_stream("作者是誰？", "req_img", **kwargs)]

    asyncio.run(run())
    return seen["size"]


def test_kb_artwork_sends_original_image_not_thumbnail(monkeypatch):
    original = Image.open(REPO_ROOT / "kb/images/npm-000001.jpg").size  # 511×1024；縮圖是 240×480
    assert sent_image_size(monkeypatch, artwork_id="npm-000001") == original
