import json

import pytest

from app.core.config import get_models_config
from app.rag.chunking import build_chunks, split_paragraph
from app.rag.kb import validate_kb
from app.rag.textproc import to_taiwan
from app.repositories.index_store import IndexMismatch, get_store


def test_kb_passes_schema():
    ok, errors = validate_kb()
    assert errors == []
    assert len(ok) >= 3


def test_opencc_keeps_ambiguous_traditional_chars():
    assert to_taiwan("范寬的谿山行旅圖") == "范寬的谿山行旅圖"
    assert to_taiwan("这幅画") == "這幅畫"
    assert to_taiwan("栩達設備的訂單") == "栩達設備的訂單"  # 已是繁體就不做詞彙轉換


def test_long_paragraph_is_split_with_overlap():
    cfg = get_models_config().chunking
    text = ("這是一段很長的說明文字。" * 80)[: cfg["max_chars"] * 2]
    pieces = split_paragraph(text)
    assert len(pieces) >= 2
    assert all(len(p) <= cfg["max_chars"] for p in pieces)


def test_every_chunk_has_source_and_license():
    """每段都有出處：網址，或沒有網址時的出處文字（使用者投稿、外部文件，例如示範用的觀眾留言）。"""
    ok, _ = validate_kb()
    for a in ok:
        for c in build_chunks(a):
            assert (c["source_url"] or "").startswith("http") or c.get("source")
            assert c["license"]


def test_manifest_mismatch_is_rejected(mock_env):
    store = get_store()
    manifest = json.loads(store.manifest_path.read_text(encoding="utf-8"))
    manifest["models"]["image"]["revision"] = "deadbeef"
    assert any("revision" in p for p in store.check_manifest(manifest))
    manifest["kb_version"] = "1999.01.1"
    assert any("知識庫版本" in p for p in store.check_manifest(manifest))


def test_missing_index_refuses_to_load(tmp_path):
    from app.repositories.index_store import IndexStore

    with pytest.raises(IndexMismatch):
        IndexStore(tmp_path).load()
