"""畫作、段落、向量與 manifest 存在 PostgreSQL + pgvector（.env 設了 DATABASE_URL 時用）。

見 docs/adr/009。

資料表照企劃書 §六：artworks／chunks、parts／part_chunks、index_manifest。
每筆的完整內容存在 doc（JSONB）；企劃書列的主要欄位（標題、畫家、授權…）由 doc 自動產生，
直接下 SQL 查得到，又不會跟 doc 對不上。

make index 在同一個交易裡刪表、重建、寫入：後端只會看到舊的或新的索引，不會讀到一半。
後端載入時把資料讀進記憶體（以文搜圖、領域路由要對全部向量運算），
以圖搜圖與段落檢索（search_images／search_chunks）由 pgvector 在資料庫裡排序。
"""

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from app.core.config import get_models_config
from app.repositories import db
from app.repositories.index_store import Collection, Hit, IndexMismatch, IndexStore

# 領域 → (項目表, 段落表, 段落指回項目的欄位)
_TABLES = {
    "art": ("artworks", "chunks", "artwork_id"),
    "mfg": ("parts", "part_chunks", "part_id"),
}


def _ddl(image_dim: int, text_dim: int) -> str:
    """索引表每次 make index 整批重建，維度照 models.yaml。使用紀錄表不在這裡（見 pg_logs_repo）。

    產生欄位（GENERATED）只是 doc 的投影，方便直接下 SQL 查；程式一律讀 doc。"""
    return f"""
DROP TABLE IF EXISTS index_manifest, chunks, artworks, part_chunks, parts;
CREATE TABLE artworks (
  id TEXT PRIMARY KEY,
  ord INTEGER NOT NULL,
  title_zh TEXT GENERATED ALWAYS AS (doc #>> '{{title,zh}}') STORED,
  title_en TEXT GENERATED ALWAYS AS (doc #>> '{{title,en}}') STORED,
  artist_zh TEXT GENERATED ALWAYS AS (doc #>> '{{artist,zh}}') STORED,
  date_text TEXT GENERATED ALWAYS AS (doc ->> 'date_text') STORED,
  medium TEXT GENERATED ALWAYS AS (doc ->> 'medium') STORED,
  collection TEXT GENERATED ALWAYS AS (doc ->> 'collection') STORED,
  license TEXT GENERATED ALWAYS AS (doc #>> '{{image,license}}') STORED,
  source_url TEXT GENERATED ALWAYS AS (doc ->> 'source_url') STORED,
  doc JSONB NOT NULL,
  image_vec vector({image_dim}) NOT NULL
);
CREATE TABLE chunks (
  chunk_id TEXT PRIMARY KEY,
  artwork_id TEXT NOT NULL REFERENCES artworks (id) ON DELETE CASCADE,
  ord INTEGER NOT NULL,
  lang TEXT GENERATED ALWAYS AS (doc ->> 'lang') STORED,
  topic TEXT GENERATED ALWAYS AS (doc ->> 'topic') STORED,
  text TEXT GENERATED ALWAYS AS (doc ->> 'text') STORED,
  source_url TEXT GENERATED ALWAYS AS (doc ->> 'source_url') STORED,
  doc JSONB NOT NULL,
  text_vec vector({text_dim}) NOT NULL
);
CREATE TABLE parts (
  id TEXT PRIMARY KEY,
  ord INTEGER NOT NULL,
  part_no TEXT GENERATED ALWAYS AS (doc ->> 'part_no') STORED,
  drawing_no TEXT GENERATED ALWAYS AS (doc ->> 'drawing_no') STORED,
  name_zh TEXT GENERATED ALWAYS AS (doc #>> '{{name,zh}}') STORED,
  material TEXT GENERATED ALWAYS AS (doc ->> 'material') STORED,
  confidentiality TEXT GENERATED ALWAYS AS (doc ->> 'confidentiality') STORED,
  doc JSONB NOT NULL,
  image_vec vector({image_dim}) NOT NULL
);
CREATE TABLE part_chunks (
  chunk_id TEXT PRIMARY KEY,
  part_id TEXT NOT NULL REFERENCES parts (id) ON DELETE CASCADE,
  ord INTEGER NOT NULL,
  topic TEXT GENERATED ALWAYS AS (doc ->> 'topic') STORED,
  source TEXT GENERATED ALWAYS AS (doc ->> 'source') STORED,
  text TEXT GENERATED ALWAYS AS (doc ->> 'text') STORED,
  doc JSONB NOT NULL,
  text_vec vector({text_dim}) NOT NULL
);
CREATE TABLE index_manifest (
  singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
  kb_version TEXT GENERATED ALWAYS AS (manifest ->> 'kb_version') STORED,
  kb_hash TEXT GENERATED ALWAYS AS (manifest ->> 'kb_hash') STORED,
  manifest JSONB NOT NULL,
  published_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
"""


# 寫完資料再建索引比較快；向量都已正規化，cosine 與檔案版的內積排序相同
_INDEXES = """
CREATE INDEX ON artworks USING hnsw (image_vec vector_cosine_ops);
CREATE INDEX ON chunks USING hnsw (text_vec vector_cosine_ops);
CREATE INDEX ON chunks (artwork_id);
CREATE INDEX ON parts USING hnsw (image_vec vector_cosine_ops);
CREATE INDEX ON part_chunks USING hnsw (text_vec vector_cosine_ops);
CREATE INDEX ON part_chunks (part_id);
"""


def publish_index(index_dir: Path, pool: ConnectionPool | None = None) -> None:
    """把 make index 建好的目錄（data/index/ 或暫存目錄）整批寫進資料庫，同一個交易。

    先照檔案版的規則讀進來：manifest 與 models.yaml、kb/VERSION 不一致就拒絕寫入。"""
    manifest, art, mfg = IndexStore(index_dir)._read()
    emb = get_models_config().embeddings
    with (pool or db.get_pool()).connection() as conn, conn.transaction():
        conn.execute(_ddl(emb["image"].dim, emb["text"].dim))
        with conn.cursor() as cur:
            for key, coll in (("art", art), ("mfg", mfg)):
                items_t, chunks_t, owner = _TABLES[key]
                cur.executemany(
                    f"INSERT INTO {items_t} (id, ord, doc, image_vec) VALUES (%s, %s, %s, %s)",
                    [(x["id"], i, Jsonb(x), coll.image_vecs[i]) for i, x in enumerate(coll.items)],
                )
                cur.executemany(
                    f"INSERT INTO {chunks_t} (chunk_id, {owner}, ord, doc, text_vec)"
                    " VALUES (%s, %s, %s, %s, %s)",
                    [
                        (c["chunk_id"], c[owner], i, Jsonb(c), coll.chunk_vecs[i])
                        for i, c in enumerate(coll.chunks)
                    ],
                )
        conn.execute(_INDEXES)
        conn.execute("INSERT INTO index_manifest (manifest) VALUES (%s)", (Jsonb(manifest),))


def _stack(vecs: list, dim: int) -> np.ndarray:
    if not vecs:
        return np.zeros((0, dim), np.float32)
    return np.stack([v.to_numpy() for v in vecs]).astype(np.float32)


@dataclass
class PgCollection(Collection):
    """記憶體裡的快照＋資料庫裡的向量搜尋。快照給需要全部向量的地方（以文搜圖、領域路由）。"""

    items_table: str = ""
    chunks_table: str = ""
    pool: ConnectionPool | None = None
    _chunk_by_id: dict = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self):
        super().__post_init__()
        self._chunk_by_id = {c["chunk_id"]: c for c in self.chunks}

    def search_images(self, query: np.ndarray, k: int) -> list[Hit]:
        if not self.items:
            return []
        with self.pool.connection() as conn:
            rows = conn.execute(
                f"SELECT id, 1 - (image_vec <=> %(q)s) FROM {self.items_table}"
                " ORDER BY image_vec <=> %(q)s LIMIT %(k)s",
                {"q": query, "k": k},
            ).fetchall()
        # make index 剛換上新資料、快照還沒重載時，略過快照裡沒有的項目
        return [Hit(self.by_id[i], float(s)) for i, s in rows if i in self.by_id]

    def search_chunks(
        self,
        query: np.ndarray,
        k: int,
        owner_id: str | None = None,
        exclude: set | None = None,
        owners: set | None = None,
    ) -> list[Hit]:
        if not self.chunks:
            return []
        with self.pool.connection() as conn:
            rows = conn.execute(
                f"SELECT chunk_id, 1 - (text_vec <=> %(q)s) FROM {self.chunks_table}"
                f" WHERE (%(owner)s::text IS NULL OR {self.owner_key} = %(owner)s)"
                f" AND (%(owners)s::text[] IS NULL OR {self.owner_key} = ANY (%(owners)s))"
                " AND chunk_id <> ALL (%(exclude)s::text[])"
                " ORDER BY text_vec <=> %(q)s LIMIT %(k)s",
                {
                    "q": query,
                    "k": k,
                    "owner": owner_id or None,
                    "owners": sorted(owners) if owners is not None else None,
                    "exclude": list(exclude or ()),
                },
            ).fetchall()
        return [Hit(self._chunk_by_id[c], float(s)) for c, s in rows if c in self._chunk_by_id]


class PgIndexStore(IndexStore):
    """index_dir 仍然要給：縮圖與標準模型的 STL／STEP 是檔案，放在 data/index/。"""

    def __init__(self, index_dir: Path, pool: ConnectionPool | None = None):
        super().__init__(index_dir)
        self._pool = pool

    @property
    def pool(self) -> ConnectionPool:
        return self._pool or db.get_pool()

    def _version(self) -> object:
        """每次 make index 寫入的時間；資料庫連不上或還沒建索引就回 None（維持目前的快照）。"""
        try:
            with self.pool.connection() as conn:
                row = conn.execute("SELECT published_at FROM index_manifest").fetchone()
        except (db.DatabaseUnavailable, psycopg.Error):
            return None
        return row[0] if row else None

    def _collection(self, conn: psycopg.Connection, key: str) -> PgCollection:
        items_t, chunks_t, owner = _TABLES[key]
        emb = get_models_config().embeddings
        items = conn.execute(f"SELECT doc, image_vec FROM {items_t} ORDER BY ord").fetchall()
        chunks = conn.execute(f"SELECT doc, text_vec FROM {chunks_t} ORDER BY ord").fetchall()
        return PgCollection(
            owner,
            [r[0] for r in items],
            _stack([r[1] for r in items], emb["image"].dim),
            [r[0] for r in chunks],
            _stack([r[1] for r in chunks], emb["text"].dim),
            items_table=items_t,
            chunks_table=chunks_t,
            pool=self.pool,
        )

    def _read(self) -> tuple[dict, Collection, Collection]:
        try:
            with self.pool.connection() as conn:
                # 同一個快照讀完 manifest 與所有表：讀到一半剛好 make index，也不會新舊混在一起
                conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                row = conn.execute("SELECT manifest FROM index_manifest").fetchone()
                if row is None:
                    raise IndexMismatch("資料庫裡還沒有索引，請先執行 make index")
                manifest = row[0]
                problems = self.check_manifest(manifest)
                if problems:
                    raise IndexMismatch("；".join(problems) + "。請執行 make index 重建索引")
                return manifest, self._collection(conn, "art"), self._collection(conn, "mfg")
        except psycopg.errors.UndefinedTable as e:
            raise IndexMismatch("資料庫裡還沒有索引，請先執行 make index") from e
        except db.DatabaseUnavailable as e:
            raise IndexMismatch(str(e)) from e
        except psycopg.OperationalError as e:
            raise IndexMismatch(f"讀取資料庫失敗：{e}") from e
