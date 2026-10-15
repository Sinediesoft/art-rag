"""3D 重建結果快取＋品質閘門（TASK-20261014-002，docs/adr/042）。

同一張知識庫圖紙在 temperature 0 下，Ortho2CAD 每次輸出逐位元相同：錯的結果重跑也修不好，
直接快取只會把錯的固定下來。所以：
- 只收「合格」的結果（`gate`）：執行成功、實體有效、孔位校正沒有失敗，有標準模型時 IoU 達
  `cad.cache.min_iou`；沒有標準模型的零件不自動寫入，要主管（kb_intake）按「採用這個結果」
- key 是「哪張圖紙＋送進模型的一切」：零件 id、input.png 的 sha256、策略、模型身分（GGUF 與
  mmproj 的路徑＋大小＋mtime）、prompt、max_pixels、max_tokens、前處理版本；任何一項變了都不命中
  （兩張圖紙影像相同也不會互相沿用）
- 「結果不對，重新生成」：記住被否決的程式碼雜湊（不再命中、寫入或採用），改用 temperature＋seed
  重抽一個新候選（下一個沒試過的 seed）

只快取 `strategy == "ortho2cad"` 且 `layout == "kb"`（知識庫圖紙本身）；照片與對照組不進快取。
模型身分讀 `deploy/llama-router.ini`；檔案找不到或 mock 模式＝身分不明，不查也不寫。
快取存在 `data/cad_cache/cache.sqlite3`（WAL），不在 `data/cad/` 底下，下載端點讀不到。
命中時程式碼照樣經過 `check_code` 與沙箱重跑，重跑後再過一次閘門。
"""

import hashlib
import json
import sqlite3
import threading
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

from app.cad.holes import DETECT_FAILED_REASON
from app.cad.preprocess import PREPROCESS_VERSION
from app.core.config import REPO_ROOT, get_models_config, get_settings
from app.core.errors import AppError
from app.core.logging import log

ROUTER_INI = REPO_ROOT / "deploy" / "llama-router.ini"
CACHE_FILE = "cache.sqlite3"


class CacheSpec(BaseModel):
    enabled: bool = True
    min_iou: float = 0.6


class ResampleSpec(BaseModel):
    temperature: float = 0.7
    max_seeds: int = 8
    warm_seeds: int = 3
    time_limit_s: float = 900


def cache_spec() -> CacheSpec:
    return CacheSpec.model_validate(get_models_config().cad.get("cache") or {})


def resample_spec() -> ResampleSpec:
    return ResampleSpec.model_validate(get_models_config().cad.get("resample") or {})


@dataclass(frozen=True)
class Candidate:
    """產生程式碼的取樣方式：seed 是 None＝greedy（models.yaml 的 cad.temperature，預設 0）。"""

    temperature: float
    seed: int | None = None

    @property
    def kind(self) -> str:
        return "greedy" if self.seed is None else "sample"

    def to_dict(self) -> dict:
        return {"kind": self.kind, "temperature": self.temperature, "seed": self.seed}

    @classmethod
    def from_dict(cls, d: dict) -> "Candidate":
        return cls(float(d.get("temperature") or 0), d.get("seed"))


def code_sha(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# ---------------------------------------------------------------- 模型身分與 key
def _router_sections(path: Path) -> dict[str, dict[str, str]]:
    """llama-server 的 models-preset（INI，開頭有不屬於任何區段的 version = 1）
    → {區段: {鍵: 值}}。"""
    sections: dict[str, dict[str, str]] = {}
    current: dict[str, str] | None = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith((";", "#")):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = sections.setdefault(line[1:-1].strip(), {})
        elif current is not None and "=" in line:
            k, v = line.split("=", 1)
            current[k.strip()] = v.strip()
    return sections


def _file_identity(rel: str) -> dict | None:
    p = Path(rel) if Path(rel).is_absolute() else REPO_ROOT / rel
    try:
        st = p.stat()
    except OSError:
        return None
    return {"path": rel, "size": st.st_size, "mtime_ns": st.st_mtime_ns}


def model_identity(alias: str) -> dict | None:
    """Ortho2CAD 的模型身分：router 設定裡這個 alias 的 GGUF 與 mmproj（路徑＋大小＋mtime）。
    設定檔、區段或任一個檔案找不到回 None＝身分不明（不查也不寫快取）。"""
    try:
        section = _router_sections(ROUTER_INI).get(alias)
    except OSError:
        return None
    if not section or not section.get("model") or not section.get("mmproj"):
        return None
    model, mmproj = _file_identity(section["model"]), _file_identity(section["mmproj"])
    if model is None or mmproj is None:
        return None
    return {"alias": alias, "model": model, "mmproj": mmproj}


def key_fields(
    *,
    part_id: str,
    input_png: bytes,
    strategy: str,
    model: dict,
    prompt: str,
    max_pixels: int,
    max_tokens: int,
    preprocess_version: int = PREPROCESS_VERSION,
) -> dict:
    # part_id：兩張圖紙送進模型的影像相同時，也不能互相沿用（沒有標準模型的零件只認自己的採用結果）
    return {
        "part_id": part_id,
        "input_sha256": hashlib.sha256(input_png).hexdigest(),
        "strategy": strategy,
        "model": model,
        "prompt": prompt,
        "max_pixels": int(max_pixels),
        "max_tokens": int(max_tokens),
        "preprocess_version": int(preprocess_version),
    }


def make_key(fields: dict) -> str:
    return hashlib.sha256(
        json.dumps(fields, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


# ---------------------------------------------------------------- 品質閘門
def hole_failed(holes: dict | None) -> bool:
    """孔位校正失敗：圖紙上偵測到孔卻沒校正成（模型外形對不上、孔位落在模型外…），
    或偵測本身出錯。圖紙沒有孔、沒有標準三視圖（不校正）不算失敗。"""
    if not holes or holes.get("status") == "applied":
        return False
    return bool(holes.get("detected")) or holes.get("reason") == DETECT_FAILED_REASON


def basic_failure(result: dict) -> str | None:
    """不看 IoU 的部分：執行失敗、實體無效、孔位校正失敗就回原因。"""
    if not result.get("ok"):
        return "程式碼執行失敗"
    if result.get("valid") is not True:
        return "實體無效（有瑕疵）"
    if hole_failed(result.get("holes")):
        return "孔位校正失敗"
    return None


def gate(result: dict, *, has_gt: bool, min_iou: float | None = None) -> tuple[bool, str | None]:
    """能不能自動寫入快取：基本檢查通過，而且有標準模型、IoU ≥ 門檻。回 (能不能, 不能的原因)。"""
    reason = basic_failure(result)
    if reason:
        return False, reason
    if not has_gt:
        return False, "沒有標準模型可比對，要主管按「採用這個結果」才寫入"
    threshold = cache_spec().min_iou if min_iou is None else min_iou
    iou = result.get("iou")
    if not isinstance(iou, (int, float)) or iou < threshold:
        shown = f"{iou:.2f}" if isinstance(iou, (int, float)) else "未算出"
        return False, f"IoU {shown} 低於門檻 {threshold:.2f}"
    return True, None


def regate(result: dict, *, has_gt: bool) -> tuple[bool, str | None]:
    """命中後在沙箱重跑的結果還合不合格：有標準模型的重新比 IoU 門檻（門檻可能調高了）；
    沒有標準模型的（主管採用的）只看基本檢查。"""
    if has_gt:
        return gate(result, has_gt=True)
    reason = basic_failure(result)
    return reason is None, reason


# ---------------------------------------------------------------- 儲存（SQLite，WAL）
SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    key TEXT PRIMARY KEY,
    part_id TEXT NOT NULL,
    key_fields TEXT NOT NULL,
    code TEXT NOT NULL,
    code_sha TEXT NOT NULL,
    iou REAL,
    candidate TEXT NOT NULL,
    source TEXT NOT NULL,
    job_id TEXT,
    created_by TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rejected (
    key TEXT NOT NULL,
    code_sha TEXT NOT NULL,
    job_id TEXT,
    actor_id TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (key, code_sha)
);
CREATE TABLE IF NOT EXISTS tried_seeds (
    key TEXT NOT NULL,
    seed INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (key, seed)
);
CREATE TABLE IF NOT EXISTS runs (
    job_id TEXT PRIMARY KEY,
    key TEXT NOT NULL,
    part_id TEXT NOT NULL,
    key_fields TEXT NOT NULL,
    code_sha TEXT NOT NULL,
    candidate TEXT NOT NULL,
    iou REAL,
    has_gt INTEGER NOT NULL,
    basic_ok INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_outbox (
    id TEXT PRIMARY KEY,
    row TEXT NOT NULL,
    created_at TEXT NOT NULL,
    delivered_at TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT
);
"""


@dataclass
class Entry:
    key: str
    part_id: str
    code: str
    code_sha: str
    iou: float | None
    candidate: Candidate
    source: str  # auto＝通過 IoU 門檻自動寫入；adopted＝主管採用；warm＝make cad-warm
    job_id: str | None
    created_by: str | None
    created_at: str

    def info(self) -> dict:
        return {
            "created_at": self.created_at,
            "iou": self.iou,
            "candidate": self.candidate.to_dict(),
            "source": self.source,
        }


@dataclass
class Run:
    """某個工作跑出的候選（作廢、採用時用來找回 key 與程式碼雜湊）。"""

    job_id: str
    key: str
    fields: dict
    part_id: str
    code_sha: str
    candidate: Candidate
    iou: float | None
    has_gt: bool
    basic_ok: bool


class CadCache:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._ready = False

    def _conn(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        if not self._ready:
            with self._lock:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.executescript(SCHEMA)
                self._ready = True
        return conn

    @staticmethod
    def _entry(row: sqlite3.Row) -> Entry:
        return Entry(
            key=row["key"],
            part_id=row["part_id"],
            code=row["code"],
            code_sha=row["code_sha"],
            iou=row["iou"],
            candidate=Candidate.from_dict(json.loads(row["candidate"])),
            source=row["source"],
            job_id=row["job_id"],
            created_by=row["created_by"],
            created_at=row["created_at"],
        )

    def get(self, key: str, part_id: str | None = None) -> Entry | None:
        """命中的結果；被否決的程式碼不算（順便清掉）。part_id：只認這張圖紙自己的結果
        （key 已含零件身分，這裡再確認一次）。"""
        with closing(self._conn()) as conn, conn:
            row = conn.execute("SELECT * FROM entries WHERE key = ?", (key,)).fetchone()
            if row is None or (part_id is not None and row["part_id"] != part_id):
                return None
            if self._rejected(conn, key, row["code_sha"]):
                conn.execute("DELETE FROM entries WHERE key = ?", (key,))
                return None
            return self._entry(row)

    @staticmethod
    def _rejected(conn: sqlite3.Connection, key: str, sha: str) -> bool:
        return (
            conn.execute(
                "SELECT 1 FROM rejected WHERE key = ? AND code_sha = ?", (key, sha)
            ).fetchone()
            is not None
        )

    def is_rejected(self, key: str, sha: str) -> bool:
        with closing(self._conn()) as conn:
            return self._rejected(conn, key, sha)

    def put(
        self,
        key: str,
        *,
        part_id: str,
        fields: dict,
        code: str,
        iou: float | None,
        candidate: Candidate,
        source: str,
        job_id: str | None = None,
        created_by: str | None = None,
        audit: dict | None = None,
        audit_id: str | None = None,
    ) -> Entry | None:
        """寫入一筆。被否決的程式碼不寫；已經有一筆時，只有 IoU 比較高才換掉
        （採用的結果沒有 IoU：只在還沒有任何一筆時寫入）。回寫入後的那一筆，沒寫入回 None。
        audit：這次寫入的稽核紀錄，和寫入同一個交易放進待送清單（_enqueue），
        之後由 flush_audit 送出。"""
        sha = code_sha(code)
        with closing(self._conn()) as conn, conn:
            if self._rejected(conn, key, sha):
                return None
            old = conn.execute("SELECT * FROM entries WHERE key = ?", (key,)).fetchone()
            if old is not None and not self._rejected(conn, key, old["code_sha"]):
                if old["code_sha"] == sha:
                    return None
                if iou is None or (old["iou"] is not None and old["iou"] >= iou):
                    return None
            conn.execute(
                "INSERT OR REPLACE INTO entries (key, part_id, key_fields, code, code_sha, iou,"
                " candidate, source, job_id, created_by, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    key,
                    part_id,
                    json.dumps(fields, ensure_ascii=False, sort_keys=True),
                    code,
                    sha,
                    iou,
                    json.dumps(candidate.to_dict()),
                    source,
                    job_id,
                    created_by,
                    now(),
                ),
            )
            if audit is not None:
                self._enqueue(conn, audit, audit_id)
            row = conn.execute("SELECT * FROM entries WHERE key = ?", (key,)).fetchone()
            return self._entry(row)

    def remove(self, key: str, sha: str | None = None) -> bool:
        with closing(self._conn()) as conn, conn:
            if sha is None:
                cur = conn.execute("DELETE FROM entries WHERE key = ?", (key,))
            else:
                cur = conn.execute("DELETE FROM entries WHERE key = ? AND code_sha = ?", (key, sha))
            return cur.rowcount > 0

    def reject(
        self,
        key: str,
        sha: str,
        *,
        job_id: str | None,
        actor_id: str | None,
        audit: dict | None = None,
        audit_id: str | None = None,
    ) -> bool:
        """否決一份程式碼：之後不命中、不寫入、不能採用。回這次有沒有移除快取裡的那一筆。
        audit：稽核紀錄（detail 補上 removed），和否決同一個交易放進待送清單。"""
        with closing(self._conn()) as conn, conn:
            conn.execute(
                "INSERT OR IGNORE INTO rejected (key, code_sha, job_id, actor_id, created_at)"
                " VALUES (?,?,?,?,?)",
                (key, sha, job_id, actor_id, now()),
            )
            cur = conn.execute("DELETE FROM entries WHERE key = ? AND code_sha = ?", (key, sha))
            removed = cur.rowcount > 0
            if audit is not None:
                detail = {**(audit.get("detail") or {}), "removed": removed}
                self._enqueue(conn, audit | {"detail": detail}, audit_id)
            return removed

    # ---- 稽核待送清單（outbox）：作廢、採用的稽核和快取變更同一個交易寫進這裡，
    # 再送進生產資料庫的 audit_log；送不進去（資料庫故障）就留著，之後補送，不會遺失
    @staticmethod
    def _enqueue(conn: sqlite3.Connection, audit: dict, audit_id: str | None = None) -> None:
        conn.execute(
            "INSERT INTO audit_outbox (id, row, created_at) VALUES (?,?,?)",
            (audit_id or uuid.uuid4().hex, json.dumps(audit, ensure_ascii=False), now()),
        )

    def delivered(self, outbox_id: str) -> bool:
        with closing(self._conn()) as conn:
            row = conn.execute(
                "SELECT delivered_at FROM audit_outbox WHERE id = ?", (outbox_id,)
            ).fetchone()
        return row is not None and row["delivered_at"] is not None

    def pending_audit(self, limit: int = 100) -> list[tuple[str, dict]]:
        with closing(self._conn()) as conn:
            rows = conn.execute(
                "SELECT id, row FROM audit_outbox WHERE delivered_at IS NULL"
                " ORDER BY created_at, rowid LIMIT ?",
                (limit,),
            ).fetchall()
        return [(r["id"], json.loads(r["row"])) for r in rows]

    def mark_delivered(self, outbox_id: str) -> None:
        with closing(self._conn()) as conn, conn:
            conn.execute(
                "UPDATE audit_outbox SET delivered_at = ?, attempts = attempts + 1,"
                " last_error = NULL WHERE id = ?",
                (now(), outbox_id),
            )

    def mark_failed(self, outbox_id: str, error: str) -> None:
        with closing(self._conn()) as conn, conn:
            conn.execute(
                "UPDATE audit_outbox SET attempts = attempts + 1, last_error = ? WHERE id = ?",
                (error[:300], outbox_id),
            )

    def mark_tried(self, key: str, seed: int) -> None:
        with closing(self._conn()) as conn, conn:
            conn.execute(
                "INSERT OR IGNORE INTO tried_seeds (key, seed, created_at) VALUES (?,?,?)",
                (key, int(seed), now()),
            )

    def next_seed(self, key: str, max_seeds: int) -> int | None:
        """1…max_seeds 裡第一個沒試過的 seed；全部試過回 None。"""
        with closing(self._conn()) as conn:
            tried = {
                r["seed"]
                for r in conn.execute("SELECT seed FROM tried_seeds WHERE key = ?", (key,))
            }
        return next((n for n in range(1, max_seeds + 1) if n not in tried), None)

    def record_run(self, run: Run) -> None:
        with closing(self._conn()) as conn, conn:
            conn.execute(
                "INSERT OR REPLACE INTO runs (job_id, key, key_fields, part_id, code_sha,"
                " candidate, iou, has_gt, basic_ok, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    run.job_id,
                    run.key,
                    json.dumps(run.fields, ensure_ascii=False, sort_keys=True),
                    run.part_id,
                    run.code_sha,
                    json.dumps(run.candidate.to_dict()),
                    run.iou,
                    int(run.has_gt),
                    int(run.basic_ok),
                    now(),
                ),
            )

    def run_of(self, job_id: str) -> Run | None:
        with closing(self._conn()) as conn:
            row = conn.execute("SELECT * FROM runs WHERE job_id = ?", (job_id,)).fetchone()
        if row is None:
            return None
        return Run(
            job_id=row["job_id"],
            key=row["key"],
            fields=json.loads(row["key_fields"]),
            part_id=row["part_id"],
            code_sha=row["code_sha"],
            candidate=Candidate.from_dict(json.loads(row["candidate"])),
            iou=row["iou"],
            has_gt=bool(row["has_gt"]),
            basic_ok=bool(row["basic_ok"]),
        )


_caches: dict[Path, CadCache] = {}


def get_cache() -> CadCache:
    path = get_settings().cad_cache_dir / CACHE_FILE
    if path not in _caches:
        _caches[path] = CadCache(path)
    return _caches[path]


# ---------------------------------------------------------------- 重建流程用的情境
@dataclass
class Context:
    """一次 3D 重建在快取裡的位置。只有知識庫圖紙＋Ortho2CAD＋模型身分確定時才有。"""

    key: str
    fields: dict
    part_id: str
    has_gt: bool

    def lookup(self) -> Entry | None:
        return get_cache().get(self.key, part_id=self.part_id)


def context(
    *,
    strategy: str,
    layout: str,
    part: dict | None,
    model_alias: str | None,
    input_png: bytes,
    prompt: str,
    max_pixels: int,
    max_tokens: int,
    has_gt: bool,
) -> Context | None:
    if strategy != "ortho2cad" or layout != "kb" or part is None or not model_alias:
        return None
    if not cache_spec().enabled:
        return None
    model = model_identity(model_alias)
    if model is None:
        return None
    fields = key_fields(
        part_id=part["id"],
        input_png=input_png,
        strategy=strategy,
        model=model,
        prompt=prompt,
        max_pixels=max_pixels,
        max_tokens=max_tokens,
    )
    return Context(make_key(fields), fields, part["id"], has_gt)


def _record(ctx: Context, job_id: str, sha: str, candidate: Candidate, result: dict) -> None:
    get_cache().record_run(
        Run(
            job_id,
            ctx.key,
            ctx.fields,
            ctx.part_id,
            sha,
            candidate,
            result.get("iou"),
            ctx.has_gt,
            basic_failure(result) is None,
        )
    )


def after_hit(ctx: Context, job_id: str, entry: Entry, result: dict) -> dict:
    """命中並通過重跑閘門：記下這個工作用的是哪一份程式碼（之後可以作廢），回給前端的快取狀態。"""
    _record(ctx, job_id, entry.code_sha, entry.candidate, result)
    return {
        "status": "hit",
        "reason": None,
        "adoptable": False,
        "regenerable": True,
        **entry.info(),
    }


def after_run(
    ctx: Context,
    *,
    job_id: str,
    code: str,
    candidate: Candidate,
    result: dict,
    source: str = "auto",
) -> dict:
    """生成並執行完一個候選後：記下這次的候選、通過閘門就寫入，回給前端的快取狀態。"""
    cache = get_cache()
    sha = code_sha(code)
    reason = basic_failure(result)
    _record(ctx, job_id, sha, candidate, result)
    info = {"status": "not_stored", "reason": None, "adoptable": False, "regenerable": True}
    info |= {"candidate": candidate.to_dict(), "iou": result.get("iou"), "created_at": None}
    if cache.is_rejected(ctx.key, sha):
        return info | {
            "status": "rejected_before",
            "reason": "這份程式碼先前被標記為不對，沒有寫入；請按「結果不對，重新生成」換一個候選",
        }
    ok, why = gate(result, has_gt=ctx.has_gt)
    if not ok:
        return info | {"reason": why, "adoptable": not ctx.has_gt and reason is None}
    entry = cache.put(
        ctx.key,
        part_id=ctx.part_id,
        fields=ctx.fields,
        code=code,
        iou=result.get("iou"),
        candidate=candidate,
        source=source,
        job_id=job_id,
    )
    if entry is None:
        return info | {"reason": "快取裡已經有 IoU 相同或更高的結果"}
    log.info(
        "cad_cache_stored",
        extra={"fields": {"part_id": ctx.part_id, "iou": entry.iou, **candidate.to_dict()}},
    )
    return info | {"status": "stored", "created_at": entry.created_at}


# ---------------------------------------------------------------- 作廢、採用（API）
NOT_APPLICABLE = (
    "CAD_CACHE_NOT_APPLICABLE",
    "這個結果不在快取範圍（只有知識庫圖紙本身的 Ortho2CAD 結果）",
    409,
)


def _job_run(account, job_id: str) -> tuple[Run, object]:
    """工作看得到、已發布、而且是快取範圍內的候選：回 (候選紀錄, 存取紀錄)。
    已發布的 code.py 要和紀錄的雜湊一致，作廢、採用的才是畫面上那一份。"""
    from app.services import cad_access, identity, security

    identity.require_domain(account, "mfg")
    access = security.cad_require_read(account, cad_access.read_access(job_id))
    run = get_cache().run_of(job_id)
    d = cad_access.published_dir(job_id)
    if run is None or d is None or not (d / "code.py").is_file():
        raise AppError(*NOT_APPLICABLE)
    if code_sha((d / "code.py").read_text(encoding="utf-8")) != run.code_sha:
        raise AppError(*NOT_APPLICABLE)
    return run, access


_flush_lock = threading.Lock()


def flush_audit(limit: int = 100) -> int:
    """把待送的作廢／採用稽核送進生產資料庫的 audit_log，回還沒送出的筆數。
    送不進去（資料庫故障）就停在那一筆、記下錯誤，下次再送；同一筆以 request_id 去重
    （add_audit 的 once_request），送到一半中斷後重送也不會重複。作廢、採用之後、
    讀稽核紀錄（GET /audit）之前、背景維護執行緒每一輪都會呼叫。"""
    from app.repositories.production_repo import get_production_repo

    cache = get_cache()
    with _flush_lock:
        for outbox_id, row in cache.pending_audit(limit):
            try:
                get_production_repo().add_audit(row, once_request=True)
            except Exception as e:  # noqa: BLE001 — 資料庫故障：留在待送清單，之後補送
                cache.mark_failed(outbox_id, f"{type(e).__name__}: {e}")
                log.warning(
                    "cad_cache_audit_pending",
                    extra={"fields": {"outbox_id": outbox_id, "error": type(e).__name__}},
                )
                break
            cache.mark_delivered(outbox_id)
        return len(cache.pending_audit(limit))


def _audit_row(account, action: str, job_id: str, summary: str, request_id, detail) -> dict:
    from app.jobs import audit

    # request_id 是補送去重的依據：沒有（直接呼叫）就給一個唯一值
    rid = request_id or "cadcache_" + uuid.uuid4().hex[:16]
    return audit.row(account, action, "cad_cache", job_id, summary, rid, detail)


def _audit_state(audit_id: str) -> str:
    """recorded＝已寫進稽核紀錄；pending＝快取變更與稽核已一起存下，稽核資料庫暫時寫不進去，稍後補送。"""
    flush_audit()
    return "recorded" if get_cache().delivered(audit_id) else "pending"


def reject_job(account, job_id: str, request_id: str | None) -> dict:
    """「結果不對」：讀得到這個工作的人都可以作廢；記下程式碼雜湊（之後不命中、不寫入、
    不能採用）。稽核和否決在同一個交易存進待送清單，再送進稽核紀錄（送不進去就稍後補送）。"""
    run, _ = _job_run(account, job_id)
    audit_id = uuid.uuid4().hex
    row = _audit_row(
        account,
        "作廢",
        job_id,
        f"3D 重建結果標記為不對（{run.part_id}）",
        request_id,
        {"part_id": run.part_id, **run.candidate.to_dict()},
    )
    removed = get_cache().reject(
        run.key, run.code_sha, job_id=job_id, actor_id=account.id, audit=row, audit_id=audit_id
    )
    return {"job_id": job_id, "rejected": True, "removed": removed, "audit": _audit_state(audit_id)}


def adopt_job(account, job_id: str, request_id: str | None) -> dict:
    """「採用這個結果」：只給沒有標準模型的零件（有標準模型的依 IoU 門檻自動寫入），
    要 kb_intake（主管）；被否決過、沒通過基本檢查的不能採用。稽核和寫入在同一個交易。"""
    from app.services import identity

    identity.require_domain(account, "mfg")
    identity.require(account, "kb_intake", "採用 3D 重建結果")
    run, _ = _job_run(account, job_id)
    if run.has_gt:
        raise AppError(
            "CAD_CACHE_NOT_ADOPTABLE",
            "這張圖紙有標準模型，結果依 IoU 門檻自動寫入，不能手動採用",
            409,
        )
    if not run.basic_ok:
        raise AppError(
            "CAD_CACHE_NOT_ADOPTABLE",
            "這個結果沒有通過檢查（執行失敗、實體無效或孔位校正失敗），不能採用",
            409,
        )
    cache = get_cache()
    if cache.is_rejected(run.key, run.code_sha):
        raise AppError("CAD_CACHE_NOT_ADOPTABLE", "這份程式碼已被標記為不對，不能採用", 409)
    from app.services import cad_access

    d = cad_access.published_dir(job_id)
    code = (d / "code.py").read_text(encoding="utf-8")
    audit_id = uuid.uuid4().hex
    entry = cache.put(
        run.key,
        part_id=run.part_id,
        fields=run.fields,
        code=code,
        iou=None,
        candidate=run.candidate,
        source="adopted",
        job_id=job_id,
        created_by=account.id,
        audit=_audit_row(
            account,
            "採用",
            job_id,
            f"採用 3D 重建結果為標準答案（{run.part_id}）",
            request_id,
            {"part_id": run.part_id, **run.candidate.to_dict()},
        ),
        audit_id=audit_id,
    )
    if entry is None:
        raise AppError("CAD_CACHE_EXISTS", "這張圖紙已經有快取的結果", 409)
    return {
        "job_id": job_id,
        "adopted": True,
        "created_at": entry.created_at,
        "audit": _audit_state(audit_id),
    }
