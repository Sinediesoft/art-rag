"""
search.py：搜尋功能（你的 AI 核心）

對照《00_專案學習地圖.md》第 4 節，這個檔案負責：
  A2 切段      make_passages()     把每件作品的描述切成小段（索引卡）
  A3 向量化    Embedder            把文字變成 512 個數字（意思地圖上的座標）
  A4 存索引    save_index()        把段落和向量存進 index/（卡櫃）
  B3 搜尋      Searcher.search()   找出跟問題最接近的 3 段，名字對上再加分

這個檔案不會自己執行；build_index.py、server.py、test_search.py 會來用它。
"""

from __future__ import annotations

import contextlib
import difflib
import hashlib
import json
import os
import re
import sys
import time
import zlib
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

# ─────────────────────────────────────────────
# 設定：階段 4 會用測驗結果調整這些數字
# ─────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent
# 作品資料放哪裡：
#   個人專案 C:\dev\art-mcp → 讀自己的 data/artworks/
#   放在小組 repo 的 integrations/art-mcp/ → 沒有自己的 data/，直接讀小組知識庫 kb/artworks/（同一份 23 筆，不重複放）
_OWN_DATA = ROOT / "data" / "artworks"
_GROUP_KB = ROOT.parent.parent / "kb" / "artworks"
DATA_DIR = Path(
    os.environ.get(
        "ARTMCP_DATA_DIR", _OWN_DATA if _OWN_DATA.exists() or not _GROUP_KB.exists() else _GROUP_KB
    )
)
INDEX_DIR = Path(os.environ.get("ARTMCP_INDEX_DIR", ROOT / "index"))
MODEL_DIR = ROOT / "models"  # 向量模型下載到這裡（不放在使用者資料夾，避免中文路徑）
# 名字清單（你可以自己加人名、廟名，一行一個）。小組 repo 會忽略所有 data/ 資料夾，所以那邊放在 keywords.txt
KEYWORDS_FILE = next(
    (p for p in (ROOT / "data" / "keywords.txt", ROOT / "keywords.txt") if p.exists()),
    ROOT / "keywords.txt",
)

MODEL_NAME = "BAAI/bge-small-zh-v1.5"  # 翻譯員：小型中文向量模型，約 90 MB，輸出 512 個數字
MAX_CHARS = 300  # 一段最多約 300 字
TOP_K = 3  # 預設取前 3 段（重複的段落只算一次，見 Searcher.ranked）
KEYWORD_BONUS = 0.05  # 每對上一個名字加的分數
MAX_BONUS = 0.10  # 加分上限（避免名字分數蓋過意思分數）
NEAR_DUP = 0.97  # 兩張卡文字有 97% 以上相同，就當成同一段話（去重用，見 Searcher._near_dup_groups）
# ↑ 10/26 步驟 7 實驗決定：館方重複段落有的差一兩個字（例如「花」→「黏」），只認「一字不差」會漏掉。
#   97 張卡兩兩比：館方重複的都在 98.6% 以上；不同作品只是格式像的「基本資料」最高 94.5%，所以線畫在 97%。
MIN_SCORE = float(os.environ.get("ARTMCP_MIN_SCORE", 0.60))
# ↑ 第 1 名低於這個分數，就當作「找不到」，一張卡都不交。
#   10/25 用 Door 出的 25 題校正（原本 0.45 會讓 5 題沒答案的裡有 4 題被說成「找到了」）：
#   0.60 擋掉 2 題完全離題（0.402、0.583），只漏掉 2 題只打關鍵字的題目；
#   線畫在離題最高 0.583 和下一題 0.623 中間，兩邊各留約 0.02。
#   寧可少漏：漏掉＝Claude 什麼都拿不到；同領域沒收、資料沒寫的題目交給 Claude 讀卡後判斷。

LICENSE_WHITELIST = {"CC0", "公有領域", "CC BY 4.0"}  # 和小組資料規格相同
REQUIRED_FIELDS = [
    "id",
    "title",
    "artist",
    "date_text",
    "collection",
    "image",
    "source_url",
    "descriptions",
]

# 雲端測試用：雲端下載不到真模型，設 ARTMCP_FAKE_EMBED=1 就改用「假向量」只測程式邏輯
FAKE_EMBED = os.environ.get("ARTMCP_FAKE_EMBED") == "1"
FAKE_DIM = 512


# ─────────────────────────────────────────────
# A1 讀資料：讀 data/artworks/ 裡的 JSON
# ─────────────────────────────────────────────
def load_artworks(data_dir: Path = DATA_DIR) -> tuple[dict[str, dict], list[str]]:
    """讀入所有作品。回傳（作品字典, 問題清單）。格式有問題的作品會跳過並記在問題清單。"""
    artworks: dict[str, dict] = {}
    problems: list[str] = []
    for path in sorted(Path(data_dir).glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            problems.append(f"{path.name}：不是正確的 JSON（第 {e.lineno} 行附近）")
            continue
        missing = [f for f in REQUIRED_FIELDS if f not in record]
        if missing:
            problems.append(f"{path.name}：缺少必填欄位 {missing}")
            continue
        if record["id"] != path.stem:
            problems.append(f"{path.name}：檔名和 id（{record['id']}）不一致")
            continue
        license_ = record["image"].get("license")
        if license_ not in LICENSE_WHITELIST:
            problems.append(
                f"{path.name}：圖片授權「{license_}」不在白名單 {sorted(LICENSE_WHITELIST)}"
            )
            continue
        artworks[record["id"]] = record
    return artworks, problems


def data_fingerprint(data_dir: Path = DATA_DIR) -> str:
    """把所有資料檔的內容算成一個「指紋」。資料一改，指紋就變，用來發現「改了資料卻忘了重建索引」。"""
    h = hashlib.sha256()
    for path in sorted(Path(data_dir).glob("*.json")):
        h.update(path.name.encode("utf-8"))
        h.update(path.read_bytes())
    return h.hexdigest()[:16]


# ─────────────────────────────────────────────
# A2 切段：一張索引卡只講一件事
# ─────────────────────────────────────────────
@dataclass
class Passage:
    passage_id: str  # 例如 npm-000001#3
    artwork_id: str
    title: str
    artist: str
    topic: str
    text: str
    source_url: str
    license: str
    attribution: str

    def text_for_embedding(self) -> str:
        """送去向量化的文字：前面加上作品名、作者、主題，讓「卡片」知道自己出自哪本書。"""
        return f"{self.title}（{self.artist}）｜{self.topic}｜{self.text}"


def split_text(text: str, max_chars: int = MAX_CHARS) -> list[str]:
    """太長的段落，照句號切成幾段，每段不超過 max_chars 字。"""
    text = text.strip()
    if len(text) <= max_chars:
        return [text]
    sentences = [s for s in re.split(r"(?<=[。！？!?])", text) if s.strip()]
    chunks, current = [], ""
    for s in sentences:
        while len(s) > max_chars:  # 一句就超過上限：硬切
            if current:
                chunks.append(current)
                current = ""
            chunks.append(s[:max_chars])
            s = s[max_chars:]
        if len(current) + len(s) > max_chars:
            chunks.append(current)
            current = s
        else:
            current += s
    if current:
        chunks.append(current)
    return chunks


def should_skip(desc: dict) -> str | None:
    """不收的段落，回傳原因；要收就回傳 None。
    沒有官方出處網址、或標示「未審核」的段落（例如觀眾留言）不收，避免來路不明的內容混進 AI 的回答。"""
    if "未審核" in (desc.get("topic") or ""):
        return "標示為未審核"
    if not desc.get("source_url"):
        return "沒有官方出處網址"
    if desc.get("license") not in LICENSE_WHITELIST:
        return f"授權「{desc.get('license')}」不在白名單"
    return None


def make_passages(artworks: dict[str, dict]) -> tuple[list[Passage], list[str]]:
    """把所有作品切成段落。回傳（段落清單, 跳過的段落說明）。"""
    passages: list[Passage] = []
    skipped: list[str] = []
    for art_id, rec in artworks.items():
        n = 0
        for desc in rec["descriptions"]:
            reason = should_skip(desc)
            if reason:
                skipped.append(f"{art_id}「{desc.get('topic', '')}」：{reason}")
                continue
            text = desc["text"]
            if desc.get("speaker"):  # 小組規格：第一人稱解說要寫明是誰說的
                text = f"{desc['speaker']}說：「{text}」"
            pieces = split_text(text)
            for i, piece in enumerate(pieces, start=1):
                topic = desc.get("topic") or "介紹"
                if len(pieces) > 1:
                    topic = f"{topic}（{i}/{len(pieces)}）"
                n += 1
                passages.append(
                    Passage(
                        passage_id=f"{art_id}#{n}",
                        artwork_id=art_id,
                        title=rec["title"]["zh"],
                        artist=rec["artist"]["zh"],
                        topic=topic,
                        text=piece,
                        source_url=desc["source_url"],
                        license=desc["license"],
                        attribution=desc.get("attribution")
                        or f"{rec['collection']}，{desc['license']}",
                    )
                )
    return passages, skipped


# ─────────────────────────────────────────────
# A3 向量化：把文字變成 512 個數字
# ─────────────────────────────────────────────
class Embedder:
    """翻譯員。allow_download=True 時，第一次會自動下載模型（約 90 MB）到 models/。"""

    def __init__(self, allow_download: bool = False):
        self.fake = FAKE_EMBED
        if self.fake:
            self.name = "FAKE（雲端測試用的假向量，不是真的模型）"
            self.dim = FAKE_DIM
            return
        from fastembed import TextEmbedding

        # 載入模型時，套件可能會印訊息到螢幕；改送到 stderr，避免弄亂 MCP 的「點單管線」
        with contextlib.redirect_stdout(sys.stderr):
            self._model = TextEmbedding(
                MODEL_NAME,
                cache_dir=str(MODEL_DIR),
                local_files_only=not allow_download,
            )
        self.name = MODEL_NAME
        self.dim = 512

    def embed(self, texts: list[str]) -> np.ndarray:
        """回傳形狀為（段落數, 512）的數字表，每一列的長度都調成 1（之後才能直接算方向有多接近）。"""
        if self.fake:
            vecs = np.stack([_fake_vector(t) for t in texts])
        else:
            with contextlib.redirect_stdout(sys.stderr):
                vecs = np.array(list(self._model.embed(texts)), dtype=np.float32)
        norms = np.linalg.norm(vecs, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return (vecs / norms).astype(np.float32)


def _fake_vector(text: str) -> np.ndarray:
    """假向量：把文字裡每兩個相鄰字丟進 512 個格子裡數次數。只看「字有沒有重複」，不懂意思。"""
    v = np.zeros(FAKE_DIM, dtype=np.float32)
    for a, b in zip(text, text[1:]):
        v[zlib.crc32((a + b).encode("utf-8")) % FAKE_DIM] += 1.0
    return v


# ─────────────────────────────────────────────
# A4 存索引／讀索引（卡櫃）
# ─────────────────────────────────────────────
def save_index(
    passages: list[Passage],
    vectors: np.ndarray,
    embedder: Embedder,
    n_artworks: int,
    index_dir: Path = INDEX_DIR,
    data_dir: Path = DATA_DIR,
) -> dict:
    index_dir.mkdir(parents=True, exist_ok=True)
    np.save(index_dir / "vectors.npy", vectors)
    (index_dir / "passages.json").write_text(
        json.dumps([asdict(p) for p in passages], ensure_ascii=False, indent=1), encoding="utf-8"
    )
    meta = {
        "model": embedder.name,
        "fake": embedder.fake,
        "dim": int(vectors.shape[1]),
        "n_passages": len(passages),
        "n_artworks": n_artworks,
        "data_fingerprint": data_fingerprint(data_dir),
        "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    (index_dir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    return meta


def load_keywords(artworks: dict[str, dict]) -> list[str]:
    """關鍵字加分用的名字清單：作品名、作者、典藏單位、風格標籤，加上 data/keywords.txt 裡你自己寫的。"""
    words: set[str] = set()
    for rec in artworks.values():
        words.add(rec["title"]["zh"])
        artist = rec["artist"]["zh"]
        words.add(artist)
        words.update(re.split(r"[．·・]", artist))  # 「喬治．秀拉」也拆出「秀拉」
        words.add(re.sub(r"（.*?）|\(.*?\)", "", rec["collection"]).strip())
        words.update(rec.get("style_tags", []))
    if KEYWORDS_FILE.exists():
        for line in KEYWORDS_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                words.add(line)
    # 「不詳」「未載明…」是館方沒寫時的佔位字，不是名字；放進來的話，問題裡有「不詳」就會讓 12 件版印全部加分
    words = {w for w in words if not w.startswith(("不詳", "未載明"))}
    return sorted((w for w in words if len(w) >= 2), key=len, reverse=True)


# ─────────────────────────────────────────────
# B3 搜尋：找最近的 3 段，名字對上再加分
# ─────────────────────────────────────────────
class IndexNotReady(Exception):
    """卡櫃還沒建好（還沒執行 build_index.bat）。"""


class Searcher:
    def __init__(self, index_dir: Path = INDEX_DIR, data_dir: Path = DATA_DIR):
        if not (index_dir / "meta.json").exists():
            raise IndexNotReady("索引還沒建立：請先雙擊 build_index.bat")
        self.meta = json.loads((index_dir / "meta.json").read_text(encoding="utf-8"))
        self.passages = [
            Passage(**p)
            for p in json.loads((index_dir / "passages.json").read_text(encoding="utf-8"))
        ]
        self.vectors = np.load(index_dir / "vectors.npy")
        artworks, _ = load_artworks(data_dir)
        self.keywords = load_keywords(artworks)
        self.stale = self.meta["data_fingerprint"] != data_fingerprint(data_dir)
        self.dup_group = self._near_dup_groups()  # 每張卡屬於哪一組「同一段話」（啟動時算一次）
        self._embedder: Embedder | None = None  # 第一次搜尋時才載入模型（讓 Server 啟動快一點）

    def _near_dup_groups(self) -> list[int]:
        """幫每張卡標上組別：文字有 NEAR_DUP 以上相同的卡算同一組（組別＝那組第一張卡的編號）。
        一字不差的當然也算同一組。只在啟動時比一次（97 張約 4,600 對），之後每次搜尋直接查表。"""
        groups: list[int] = []
        for i, p in enumerate(self.passages):
            group = i
            for j in range(i):
                if groups[j] != j:  # 只跟每組的代表比
                    continue
                q = self.passages[j].text
                if p.text == q or (
                    abs(len(p.text) - len(q)) <= 20
                    and difflib.SequenceMatcher(None, p.text, q).ratio() >= NEAR_DUP
                ):
                    group = j
                    break
            groups.append(group)
        return groups

    @property
    def embedder(self) -> Embedder:
        if self._embedder is None:
            self._embedder = Embedder(allow_download=False)
            if self._embedder.fake != self.meta["fake"]:
                raise IndexNotReady("索引是用另一種向量建的，請重新雙擊 build_index.bat")
        return self._embedder

    def score_all(self, query: str, use_bonus: bool = True) -> list[dict]:
        """幫每一段打分數，照總分由高到低排好。"""
        q = self.embedder.embed([query])[0]
        semantic = self.vectors @ q  # 餘弦相似度：兩個長度為 1 的向量相乘再加總
        in_query = [w for w in self.keywords if w in query]
        rows = []
        for i, (p, sem) in enumerate(zip(self.passages, semantic)):
            full = f"{p.title} {p.artist} {p.topic} {p.text}"
            matched = [w for w in in_query if w in full] if use_bonus else []
            bonus = min(KEYWORD_BONUS * len(matched), MAX_BONUS)
            rows.append(
                {
                    "index": i,  # 第幾張卡（去重時用來查組別）
                    "passage": p,
                    "semantic": round(float(sem), 4),
                    "bonus": round(bonus, 4),
                    "score": round(float(sem) + bonus, 4),
                    "matched_keywords": matched,
                }
            )
        rows.sort(key=lambda r: r["score"], reverse=True)
        return rows

    def ranked(self, query: str, use_bonus: bool = True, dedup: bool = True) -> list[dict]:
        """排好名次的段落。dedup=True 時，同一段話（文字 97% 以上相同）只留分數最高的那張。
        （10/25 決定搜尋時去重；10/26 步驟 7 實驗後，從「一字不差」放寬成「97% 以上相同」）
        館方原文本來就重複（例如陳天乞的匠師介紹出現在褒忠祠 4 件作品裡），
        不去重的話，前 3 名可能是同一段話出現 3 次，等於只給 Claude 1 段資料。
        被拿掉的那幾張不是丟掉，而是記在留下那張的 also_in：「這段話也出現在哪些作品」。"""
        rows = self.score_all(query, use_bonus=use_bonus)
        for r in rows:
            r["also_in"] = []
        if not dedup:
            return rows
        kept: dict[int, dict] = {}  # 組別 → 留下的那一列（rows 已排序，第一次遇到的分數最高）
        out = []
        for r in rows:
            p = r["passage"]
            g = self.dup_group[r["index"]]
            first = kept.get(g)
            if first is None:
                kept[g] = r
                out.append(r)
            elif p.artwork_id != first["passage"].artwork_id and p.artwork_id not in [
                a["artwork_id"] for a in first["also_in"]
            ]:
                first["also_in"].append({"artwork_id": p.artwork_id, "title": p.title})
        return out

    def search(
        self, query: str, top_k: int = TOP_K, use_bonus: bool = True, dedup: bool = True
    ) -> dict:
        """找出最相關的 top_k 段（重複的段落只算一次）。第 1 名分數太低時，found=False（老實說找不到）。"""
        rows = self.ranked(query, use_bonus=use_bonus, dedup=dedup)
        best = rows[0]["score"] if rows else 0.0
        found = best >= MIN_SCORE
        results = []
        if found:
            for rank, r in enumerate(rows[:top_k], start=1):
                p = r["passage"]
                results.append(
                    {
                        "rank": rank,
                        "score": r["score"],
                        "semantic_score": r["semantic"],
                        "keyword_bonus": r["bonus"],
                        "matched_keywords": r["matched_keywords"],
                        "artwork_id": p.artwork_id,
                        "title": p.title,
                        "artist": p.artist,
                        "topic": p.topic,
                        "text": p.text,
                        "source_url": p.source_url,
                        "license": p.license,
                        "attribution": p.attribution,
                        "also_in": r["also_in"],  # 同一段話也出現在這些作品（館方原文重複）
                    }
                )
        return {
            "query": query,
            "found": found,
            "best_score": best,
            "threshold": MIN_SCORE,
            "results": results,
        }

    def search_artworks(self, query: str, limit: int = 5) -> dict:
        """以「作品」為單位：每件作品取它分數最高的一段當代表，再排名。"""
        rows = self.score_all(query)
        best_by_art: dict[str, dict] = {}
        for r in rows:  # rows 已排序，第一次遇到的就是該作品最高分
            best_by_art.setdefault(r["passage"].artwork_id, r)
        ranked = list(best_by_art.values())[:limit]
        best = ranked[0]["score"] if ranked else 0.0
        found = best >= MIN_SCORE
        results = (
            [
                {
                    "rank": i,
                    "artwork_id": r["passage"].artwork_id,
                    "title": r["passage"].title,
                    "artist": r["passage"].artist,
                    "score": r["score"],
                    "best_matching_topic": r["passage"].topic,
                    "matched_keywords": r["matched_keywords"],
                }
                for i, r in enumerate(ranked, start=1)
            ]
            if found
            else []
        )
        return {
            "query": query,
            "found": found,
            "best_score": best,
            "threshold": MIN_SCORE,
            "results": results,
        }
