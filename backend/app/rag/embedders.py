"""embed_image() / embed_text()：建索引與查詢呼叫同一組函式（共用層 §三）。

- 影像與以文搜圖：Chinese-CLIP（512 維，影像–文字共同空間）
- 知識段落：bge-m3（1024 維，dense，CLS pooling）
一律 fp32、L2 正規化；模型名稱與 revision 只從 shared/models.yaml 讀。
EMBED_MODE=mock 時改用雜湊產生的固定向量，給 CI 與沒有模型的電腦用。
"""

import hashlib
import os
import threading
from functools import lru_cache

import numpy as np
from PIL import Image

from app.core.config import get_models_config, get_settings

_lock = threading.Lock()


def _normalize(v: np.ndarray) -> np.ndarray:
    v = v.astype(np.float32)
    return v / np.clip(np.linalg.norm(v, axis=-1, keepdims=True), 1e-12, None)


def _mock_vec(key: str, dim: int) -> np.ndarray:
    seed = int.from_bytes(hashlib.sha256(key.encode("utf-8")).digest()[:8], "little")
    return _normalize(np.random.default_rng(seed).standard_normal(dim))


def _is_mock() -> bool:
    return get_settings().embed_mode == "mock"


def _hf_offline() -> None:
    """HF_OFFLINE=true：只讀本機快取，不連 Hugging Face（斷網可用）。

    須在匯入 transformers 前呼叫。
    """
    if get_settings().hf_offline:
        os.environ["HF_HUB_OFFLINE"] = "1"


@lru_cache
def _clip():
    _hf_offline()
    import torch
    from transformers import ChineseCLIPModel, ChineseCLIPProcessor

    spec = get_models_config().embeddings["image"]
    model = ChineseCLIPModel.from_pretrained(spec.name, revision=spec.revision, dtype=torch.float32)
    proc = ChineseCLIPProcessor.from_pretrained(spec.name, revision=spec.revision)
    return model.eval().to(get_settings().embed_device), proc


@lru_cache
def _bge():
    _hf_offline()
    import torch
    from transformers import AutoModel, AutoTokenizer

    spec = get_models_config().embeddings["text"]
    model = AutoModel.from_pretrained(spec.name, revision=spec.revision, dtype=torch.float32)
    tok = AutoTokenizer.from_pretrained(spec.name, revision=spec.revision)
    return model.eval().to(get_settings().embed_device), tok


def _features(out) -> "np.ndarray":
    # transformers 5 回傳 BaseModelOutputWithPooling（pooler_output 為投影後特徵）；
    # 舊版直接回 tensor
    t = out if hasattr(out, "detach") else out.pooler_output
    return t.detach().cpu().float().numpy()


def embed_image(img: Image.Image) -> np.ndarray:
    """單張已前處理的圖片 → (512,) 正規化向量。"""
    if _is_mock():
        return _mock_vec("img:" + hashlib.sha256(img.tobytes()).hexdigest(), 512)
    import torch

    model, proc = _clip()
    with _lock, torch.no_grad():
        inputs = proc(images=img, return_tensors="pt").to(model.device)
        return _normalize(_features(model.get_image_features(**inputs)))[0]


def embed_text_clip(texts: list[str]) -> np.ndarray:
    """以文搜圖：文字 → Chinese-CLIP 文字向量 (n, 512)。"""
    if _is_mock():
        return np.stack([_mock_vec("clip:" + t, 512) for t in texts])
    import torch

    model, proc = _clip()
    with _lock, torch.no_grad():
        inputs = proc(text=texts, return_tensors="pt", padding=True, truncation=True, max_length=52)
        return _normalize(_features(model.get_text_features(**inputs.to(model.device))))


def embed_text(texts: list[str]) -> np.ndarray:
    """知識段落與問題 → bge-m3 dense 向量 (n, 1024)。"""
    spec = get_models_config().embeddings["text"]
    if _is_mock():
        return np.stack([_mock_vec("bge:" + t, spec.dim) for t in texts])
    import torch

    model, tok = _bge()
    with _lock, torch.no_grad():
        batch = tok(
            texts,
            return_tensors="pt",
            padding=True,
            truncation=True,
            max_length=spec.max_length or 512,
        ).to(model.device)
        cls = model(**batch).last_hidden_state[:, 0]
        return _normalize(cls.cpu().float().numpy())


def warmup() -> None:
    if not _is_mock():
        _clip()
        _bge()
