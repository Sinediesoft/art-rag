"""向量索引存取層。

兩種存放方式，介面相同，services 與 rag 不必知道是哪一種（docs/adr/009）：
- .env 設了 DATABASE_URL：PostgreSQL + pgvector（Docker），見 pg_index_store.py
- 留空：data/index/ 下的檔案（numpy + JSON），給沒有 Docker 的電腦

兩個領域各一個 Collection：畫作（data/index/*）與工廠圖紙（data/index/parts/*），
共用同一份 manifest，一起建、一起換上。縮圖與標準模型的 STL／STEP 兩種方式都放在 data/index/。
"""

import json
import threading
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from app.core.config import get_models_config, get_settings, kb_version


class IndexMismatch(Exception):
    pass


@dataclass
class Hit:
    item: dict
    score: float


@dataclass
class Collection:
    """一個知識庫領域的項目、影像向量與段落向量。owner_key 是段落指回項目的欄位。"""

    owner_key: str
    items: list[dict] = field(default_factory=list)
    image_vecs: np.ndarray = field(default_factory=lambda: np.zeros((0, 512), np.float32))
    chunks: list[dict] = field(default_factory=list)
    chunk_vecs: np.ndarray = field(default_factory=lambda: np.zeros((0, 1024), np.float32))

    def __post_init__(self):
        self.by_id = {x["id"]: x for x in self.items}

    def search_images(self, query: np.ndarray, k: int, owners: set | None = None) -> list[Hit]:
        """owners：只在這些項目裡找（例如目前身分看得到的圖紙，docs/adr/030）；None＝不限。
        看不到的項目連相似度都不算，之後的幾何驗證也就不會讀它的圖檔。"""
        rows = [i for i, x in enumerate(self.items) if owners is None or x["id"] in owners]
        if not rows:
            return []
        sims = self.image_vecs[rows] @ query
        return [Hit(self.items[rows[j]], float(sims[j])) for j in np.argsort(-sims)[:k]]

    def search_chunks(
        self,
        query: np.ndarray,
        k: int,
        owner_id: str | None = None,
        exclude: set | None = None,
        owners: set | None = None,
    ) -> list[Hit]:
        """owners：Metadata Filter 允許的項目（例如看得到的圖紙）；None＝不限。"""
        if not self.chunks:
            return []
        sims = self.chunk_vecs @ query
        hits = []
        for i in np.argsort(-sims):
            c = self.chunks[i]
            if owner_id and c[self.owner_key] != owner_id:
                continue
            if owners is not None and c[self.owner_key] not in owners:
                continue
            if exclude and c["chunk_id"] in exclude:
                continue
            hits.append(Hit(c, float(sims[i])))
            if len(hits) >= k:
                break
        return hits


class IndexStore:
    """檔案版。PostgreSQL 版（PgIndexStore）只覆寫 _version() 與 _read()。"""

    def __init__(self, index_dir: Path):
        self.dir = index_dir
        self._lock = threading.Lock()
        self._token: object = None
        self.manifest: dict = {}
        self.art = Collection("artwork_id")
        self.mfg = Collection("part_id")

    # 畫作沿用舊介面（services／評估腳本直接讀這些屬性）
    @property
    def artworks(self) -> list[dict]:
        return self.art.items

    @property
    def by_id(self) -> dict[str, dict]:
        return self.art.by_id

    @property
    def image_vecs(self) -> np.ndarray:
        return self.art.image_vecs

    @property
    def chunks(self) -> list[dict]:
        return self.art.chunks

    @property
    def chunk_vecs(self) -> np.ndarray:
        return self.art.chunk_vecs

    @property
    def parts(self) -> list[dict]:
        return self.mfg.items

    # ---- 載入與一致性檢查 ----
    @property
    def manifest_path(self) -> Path:
        return self.dir / "manifest.json"

    @property
    def parts_dir(self) -> Path:
        return self.dir / "parts"

    def check_manifest(self, manifest: dict) -> list[str]:
        """回傳不一致的原因；空清單代表一致。"""
        problems = []
        cfg = get_models_config()
        for key, spec in cfg.embeddings.items():
            m = manifest.get("models", {}).get(key, {})
            for f in ("name", "revision", "dim"):
                if m.get(f) != getattr(spec, f):
                    problems.append(
                        f"{key} 模型的 {f} 不一致：索引={m.get(f)}，models.yaml={getattr(spec, f)}"
                    )
        if manifest.get("embed_mode") != get_settings().embed_mode:
            problems.append(
                f"embedding 模式不一致：索引={manifest.get('embed_mode')}，"
                f"EMBED_MODE={get_settings().embed_mode}"
            )
        if manifest.get("chunking") != cfg.chunking:
            problems.append("切塊規則與 models.yaml 不一致")
        if manifest.get("color_analysis") != cfg.color_analysis.model_dump(mode="json"):
            problems.append("色彩分析參數與 models.yaml 不一致")
        if manifest.get("kb_version") != kb_version():
            problems.append(
                f"知識庫版本不一致：索引={manifest.get('kb_version')}，kb/VERSION={kb_version()}"
            )
        return problems

    def _load_collection(self, d: Path, owner_key: str, items_file: str) -> Collection:
        if not (d / items_file).exists():
            return Collection(owner_key)
        return Collection(
            owner_key,
            json.loads((d / items_file).read_text(encoding="utf-8")),
            np.load(d / "image_vecs.npy"),
            json.loads((d / "chunks.json").read_text(encoding="utf-8")),
            np.load(d / "chunk_vecs.npy"),
        )

    def _version(self) -> object:
        """索引換過就會變的值（檔案版：manifest 的修改時間）；讀不到回 None。"""
        try:
            return self.manifest_path.stat().st_mtime
        except FileNotFoundError:
            return None

    def _read(self) -> tuple[dict, Collection, Collection]:
        if not self.manifest_path.exists():
            raise IndexMismatch("找不到索引，請先執行 make index")
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        problems = self.check_manifest(manifest)
        if problems:
            raise IndexMismatch("；".join(problems) + "。請執行 make index 重建索引")
        art = self._load_collection(self.dir, "artwork_id", "artworks.json")
        mfg = self._load_collection(self.parts_dir, "part_id", "parts.json")
        return manifest, art, mfg

    def load(self) -> None:
        token = self._version()  # 先記版本再讀：讀到一半被重建，下一個請求還會再載一次
        manifest, art, mfg = self._read()
        with self._lock:
            self.manifest = manifest
            self.art, self.mfg = art, mfg
            self._token = token

    def maybe_reload(self) -> bool:
        """make index 重建後自動載入新索引（不必重啟後端）；新索引不一致就維持舊的。"""
        token = self._version()
        if token is None or token == self._token:
            return False
        try:
            self.load()
            return True
        except IndexMismatch:
            self._token = token
            return False

    # ---- 查詢（畫作；圖紙直接用 self.mfg）----
    def get_artwork(self, artwork_id: str) -> dict | None:
        return self.art.by_id.get(artwork_id)

    def get_part(self, part_id: str) -> dict | None:
        return self.mfg.by_id.get(part_id)

    def search_images(self, query: np.ndarray, k: int) -> list[Hit]:
        return self.art.search_images(query, k)

    def search_chunks(
        self, query: np.ndarray, k: int, artwork_id: str | None = None, exclude: set | None = None
    ) -> list[Hit]:
        return self.art.search_chunks(query, k, owner_id=artwork_id, exclude=exclude)


_store: IndexStore | None = None


def get_store() -> IndexStore:
    global _store
    if _store is None:
        s = get_settings()
        if s.database_url:
            from app.repositories.pg_index_store import PgIndexStore

            _store = PgIndexStore(s.index_dir)
        else:
            _store = IndexStore(s.index_dir)
    return _store
