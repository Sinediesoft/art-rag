"""檢索層評估的執行環境（TASK-20261027-001）：不讀 .env 的 bootstrap、real 索引收據、gold 推導。

1. bootstrap()：在任何程式第一次呼叫 get_settings() 之前，把 pydantic Settings 的 env_file
   關掉並清掉設定快取。env -i 或 DATABASE_URL= 都關不掉 env_file（Settings 預設照樣讀
   repo 根目錄的 .env），所以要在程序內明確關。評估腳本要在 import 其他 app 模組之前呼叫：

       import retrieval_runtime
       retrieval_runtime.bootstrap()
       from app.services import chat_service  # noqa: E402

   所有設定改由環境變數與 shared/*.yaml 取得（見 README 的代跑指令）。

2. 建索引收據：build-index 呼叫既有 pipelines/build_index.py（會寫 DATA_DIR/index），
   再對載入的索引記錄 real／mock、模型 name／revision／dim、manifest、各領域 chunk 數與
   chunk／向量 digest、KB／QA／models.yaml hash、檢索參數、程式 HEAD＋dirty 與 bootstrap 狀態。
   verify_index_receipt() 有任何問題就不能當正式成績用。

       python eval/retrieval_runtime.py build-index [--run-id ID] [--out-dir eval/runs]
       python eval/retrieval_runtime.py receipt     # 不重建，只替目前的索引出收據

   EMBED_MODE 不是 real 時直接失敗；--allow-mock 只給測試，而且不能寫進 eval/runs。

3. gold 推導（純函式）：題目的文件 ID＋topics → KB descriptions[].topic → 索引 chunks 中
   該文件、該 description 位置的全部 chunk_id（含切塊後的每一段）。任何一個 topic 對不上就
   整題排除並記原因，不保留對到的子集合；正答案確定為空（expect_refusal／no_answer、
   不存在的文件）與「缺少標記」分開。系統產生的段落（#meta 基本資料、#color 色彩分析）
   不在 descriptions 裡，排除，不當 no_answer。再用 apply_visibility() 依身分可見集合求交集。

本模組 import 時不載入 app，也不碰模型；只有 bootstrap 之後的函式會 import app。
"""

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
import uuid
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
RUNS_DIR = ROOT / "eval" / "runs"
DEFAULT_QA = (ROOT / "eval" / "qa.jsonl", ROOT / "eval" / "drawing_retrieval" / "qa.jsonl")
RECEIPT_SCHEMA = "retrieval-index-receipt/1"

# 領域 → 段落指回文件的欄位、kb/ 底下的資料夾
DOMAINS = {
    "art": {"owner_key": "artwork_id", "kb_subdir": "artworks"},
    "mfg": {"owner_key": "part_id", "kb_subdir": "parts"},
}
# 題目明確標「答案為空」：expect_refusal=true，或 type 是下列之一
EXPLICIT_EMPTY_TYPES = ("no_answer", "unknown")
# KB 的 descriptions topic 帶這個字＝未審核內容：topic 相關不代表內容安全可用，另外標記
UNREVIEWED_MARK = "未審核"
# bootstrap 前可以已經載入的 app 模組（只有設定模組本身，它 import 時不讀設定）
_ALLOWED_PRELOADED = {"app", "app.core", "app.core.config"}
_DESC_CHUNK = re.compile(r"^(?P<owner>.+)#(?P<i>\d{2,})-(?P<j>\d+)$")
_SETTINGS_FIELDS = (
    "embed_mode",
    "embed_device",
    "llm_mode",
    "hf_offline",
    "memory_guard",
    "jev_enabled",
    "allow_cloud",
    "rearrange",
    "artrag_shared_dir",
)


class RuntimeIsolationError(RuntimeError):
    """設定隔離沒有生效（可能已經讀過 .env），不能繼續。"""


# ---------------------------------------------------------------- 雜項


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def new_run_id() -> str:
    """和既有 eval 腳本同一種格式：20261027T150405-1a2b。"""
    return f"{datetime.now(UTC):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:4]}"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str | None:
    """檔案不存在回 None。"""
    p = Path(path)
    return sha256_bytes(p.read_bytes()) if p.is_file() else None


def sha256_json(obj) -> str:
    """鍵排序後的 JSON 雜湊：同內容不同鍵順序得到同一個值。"""
    text = json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return sha256_bytes(text.encode("utf-8"))


def repo_relative(path: Path) -> str:
    p = Path(path).resolve()
    try:
        return p.relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return str(p)


def file_hashes(paths: Iterable[Path]) -> dict[str, str | None]:
    """{repo 相對路徑: sha256 或 None（檔案不存在）}。"""
    return {repo_relative(p): sha256_file(p) for p in paths}


def is_official_runs_dir(path: Path) -> bool:
    """path 在 eval/runs（git 追蹤的正式產物）底下；mock 產物不能寫到這裡。"""
    try:
        Path(path).resolve().relative_to(RUNS_DIR.resolve())
        return True
    except ValueError:
        return False


def git_provenance(root: Path = ROOT) -> dict:
    """執行時的程式版本：{"head": str|None, "dirty": bool|None, "dirty_paths": [str],
    "error": str|None}。不能用 manifest 的 git_commit（那是建索引時的版本）代替。"""
    git = ["git", "-C", str(root), "-c", "core.quotePath=false"]
    try:
        head = subprocess.run(
            [*git, "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
        status = subprocess.run(
            [*git, "status", "--porcelain=v1"], capture_output=True, text=True, check=True
        ).stdout
    except (OSError, subprocess.CalledProcessError) as e:
        return {"head": None, "dirty": None, "dirty_paths": [], "error": str(e)}
    paths = [line[3:] for line in status.splitlines() if line.strip()]
    return {"head": head, "dirty": bool(paths), "dirty_paths": paths, "error": None}


# ---------------------------------------------------------------- 不讀 .env 的 bootstrap


def _config():
    if str(BACKEND) not in sys.path:
        sys.path.insert(0, str(BACKEND))
    from app.core import config

    return config


def bootstrap(*, strict: bool = True) -> dict:
    """關掉 Settings 的 env_file（不讀 repo 根目錄的 .env），清掉設定相關快取。

    strict=True（正式執行）：如果 get_settings() 已經被呼叫過（可能已讀過 .env），或已經
    import 了 app.core.config 以外的 app 模組（它們可能持有舊設定），直接丟
    RuntimeIsolationError，不改任何東西。strict=False 只給測試程序（conftest 已先載入設定）。

    回傳（寫進收據的 isolation 欄位）：
    {"env_file_disabled": True, "env_file_before": str|None, "settings_were_loaded": bool,
     "preloaded_app_modules": [str], "strict": bool}
    """
    config = _config()
    preloaded = sorted(
        m
        for m in sys.modules
        if (m == "app" or m.startswith("app.")) and m not in _ALLOWED_PRELOADED
    )
    loaded = config.get_settings.cache_info().currsize > 0
    if strict and (loaded or preloaded):
        reasons = ["get_settings() 已經被呼叫過"] if loaded else []
        if preloaded:
            reasons.append(f"已載入 {', '.join(preloaded[:5])} 等 app 模組")
        raise RuntimeIsolationError("bootstrap 要在第一次讀設定之前呼叫：" + "；".join(reasons))
    before = config.Settings.model_config.get("env_file")
    config.Settings.model_config["env_file"] = None
    for cached in (
        config.get_settings,
        config.get_models_config,
        config.get_agent_config,
        config.get_access_config,
    ):
        cached.cache_clear()
    return {
        "env_file_disabled": True,
        "env_file_before": None if before is None else repo_relative(Path(before)),
        "settings_were_loaded": loaded,
        "preloaded_app_modules": preloaded,
        "strict": strict,
    }


def env_file_disabled() -> bool:
    """目前 Settings 是否不會讀任何 env_file。"""
    return _config().Settings.model_config.get("env_file") is None


# ---------------------------------------------------------------- 設定與索引 provenance


def settings_snapshot(settings) -> dict:
    """影響檢索的非機密設定。不記任何金鑰與 DATABASE_URL 本身（可能含密碼），只記有沒有設。

    回傳：{embed_mode, embed_device, llm_mode, hf_offline, memory_guard, jev_enabled,
          allow_cloud, rearrange, artrag_shared_dir, data_dir, index_dir,
          index_backend: "file"|"pg", database_url_set: bool}
    """
    snap = {f: getattr(settings, f, None) for f in _SETTINGS_FIELDS}
    snap["data_dir"] = str(settings.data_dir)
    snap["index_dir"] = str(settings.index_dir)
    snap["index_backend"] = "pg" if settings.database_url else "file"
    snap["database_url_set"] = bool(settings.database_url)
    return snap


def retrieval_params(models_cfg) -> dict:
    """shared/models.yaml 中 retrieve 用到的參數（只讀取記錄，不改）。

    回傳：{top_k_chunks, min_chunk_score, relative_chunk_ratio, global_fill_keywords, chunking}
    """
    r = models_cfg.retrieval
    return {
        "top_k_chunks": r["top_k_chunks"],
        "min_chunk_score": r["min_chunk_score"],
        "relative_chunk_ratio": r["relative_chunk_ratio"],
        "global_fill_keywords": list(models_cfg.global_fill_keywords),
        "chunking": dict(models_cfg.chunking),
    }


def model_specs(models_cfg) -> dict:
    """shared/models.yaml 的 embeddings：{key: {"name", "revision", "dim", "dtype"}}。"""
    return {
        k: {"name": s.name, "revision": s.revision, "dim": s.dim, "dtype": s.dtype}
        for k, s in models_cfg.embeddings.items()
    }


def _array_digest(arr) -> dict:
    import numpy as np

    a = np.ascontiguousarray(arr)
    return {"shape": list(a.shape), "dtype": str(a.dtype), "sha256": sha256_bytes(a.tobytes())}


def collection_digest(coll) -> dict:
    """已載入的一個領域（store.art／store.mfg）：corpus ID、數量與內容 digest。

    回傳：
    {"owner_key": str, "item_ids": [str], "item_count": int,
     "chunk_ids": [str], "chunk_count": int, "chunks_by_owner": {owner: int},
     "items_sha256": str, "chunks_sha256": str,
     "chunk_vecs": {"shape", "dtype", "sha256"}, "image_vecs": {"shape", "dtype", "sha256"}}
    """
    key = coll.owner_key
    chunk_ids = [c["chunk_id"] for c in coll.chunks]
    return {
        "owner_key": key,
        "item_ids": [x["id"] for x in coll.items],
        "item_count": len(coll.items),
        "chunk_ids": chunk_ids,
        "chunk_count": len(chunk_ids),
        "chunks_by_owner": dict(sorted(Counter(c[key] for c in coll.chunks).items())),
        "items_sha256": sha256_json(coll.items),
        "chunks_sha256": sha256_json(coll.chunks),
        "chunk_vecs": _array_digest(coll.chunk_vecs),
        "image_vecs": _array_digest(coll.image_vecs),
    }


def assemble_index_receipt(
    *,
    manifest: dict,
    manifest_problems: Sequence[str],
    collections: Mapping[str, dict],
    settings: dict,
    models: dict,
    retrieval_params: dict,
    models_yaml_sha256: str | None,
    kb: dict,
    qa: Mapping[str, str | None],
    code: dict,
    isolation: dict,
    index_build: dict | None,
    created_at: str,
) -> dict:
    """把收集到的資料組成收據（純函式）。real＝EMBED_MODE 與 manifest.embed_mode 都是 real。

    回傳：
    {
      "schema_version": RECEIPT_SCHEMA, "kind": "retrieval-index-receipt", "created_at": str,
      "real": bool, "embed_mode": str, "manifest_embed_mode": str, "index_backend": str,
      "manifest": dict, "manifest_sha256": str, "manifest_problems": [str],
      "models": {key: {name, revision, dim, dtype}},
      "collections": {"art": collection_digest, "mfg": collection_digest},
      "chunk_counts": {"art", "mfg", "manifest_art", "manifest_mfg"}, "chunk_counts_match": bool,
      "kb": {"version", "hash_now", "version_manifest"?, "hash_manifest", "match": bool},
      "qa": {path: sha256|None},
      "config": {"models_yaml_sha256", "retrieval_params", "retrieval_params_sha256"},
      "settings": settings_snapshot, "code": git_provenance, "isolation": bootstrap 結果,
      "index_build": run_build_index 結果 | {"invoked": False} | None
    }
    """
    art, mfg = collections.get("art", {}), collections.get("mfg", {})
    counts = {
        "art": art.get("chunk_count"),
        "mfg": mfg.get("chunk_count"),
        "manifest_art": manifest.get("chunk_count"),
        "manifest_mfg": manifest.get("part_chunk_count"),
    }
    return {
        "schema_version": RECEIPT_SCHEMA,
        "kind": "retrieval-index-receipt",
        "created_at": created_at,
        "real": settings.get("embed_mode") == "real" and manifest.get("embed_mode") == "real",
        "embed_mode": settings.get("embed_mode"),
        "manifest_embed_mode": manifest.get("embed_mode"),
        "index_backend": settings.get("index_backend"),
        "manifest": manifest,
        "manifest_sha256": sha256_json(manifest),
        "manifest_problems": list(manifest_problems),
        "models": models,
        "collections": dict(collections),
        "chunk_counts": counts,
        "chunk_counts_match": counts["art"] == counts["manifest_art"]
        and counts["mfg"] == counts["manifest_mfg"],
        "kb": {
            **kb,
            "match": kb.get("hash_now") is not None
            and kb.get("hash_now") == kb.get("hash_manifest"),
        },
        "qa": dict(qa),
        "config": {
            "models_yaml_sha256": models_yaml_sha256,
            "retrieval_params": retrieval_params,
            "retrieval_params_sha256": sha256_json(retrieval_params),
        },
        "settings": settings,
        "code": code,
        "isolation": isolation,
        "index_build": index_build,
    }


def verify_index_receipt(
    receipt: dict,
    *,
    require_real: bool = True,
    require_build: bool = False,
    current: dict | None = None,
) -> list[str]:
    """收據能不能支撐正式成績；回傳問題清單，空清單＝可以。

    require_real：EMBED_MODE 與 manifest 都要是 real（mock 只給測試，傳 False）。
    require_build：收據必須來自這次 build-index（不是只對既有索引出的收據）。
    current：評估當下對已載入索引重新 collect 的收據；要和 receipt 的 manifest、
    各領域 chunk 數、chunk 內容與向量 digest 一致（索引在收據之後被換過就失敗）。
    """
    problems = []
    if receipt.get("schema_version") != RECEIPT_SCHEMA:
        problems.append(f"收據格式不是 {RECEIPT_SCHEMA}：{receipt.get('schema_version')}")
    if require_real and not receipt.get("real"):
        problems.append(
            f"不是真模型索引：EMBED_MODE={receipt.get('embed_mode')}，"
            f"manifest.embed_mode={receipt.get('manifest_embed_mode')}"
        )
    problems += [f"索引與設定不一致：{p}" for p in receipt.get("manifest_problems", [])]
    if not (receipt.get("isolation") or {}).get("env_file_disabled"):
        problems.append("沒有關閉 env_file（可能讀了 .env）")
    if not receipt.get("chunk_counts_match"):
        problems.append(f"索引段落數和 manifest 不同：{receipt.get('chunk_counts')}")
    if not (receipt.get("kb") or {}).get("match"):
        problems.append("目前 KB 的 hash 和建索引時不同，請重建索引")
    build = receipt.get("index_build") or {}
    if build.get("invoked") and build.get("exit_code") != 0:
        problems.append(
            f"建索引失敗：exit_code={build.get('exit_code')} {build.get('error') or ''}"
        )
    if require_build and not build.get("invoked"):
        problems.append("收據不是這次重建索引產生的")
    missing = [p for p, h in (receipt.get("qa") or {}).items() if h is None]
    if missing:
        problems.append(f"題庫檔不存在：{', '.join(missing)}")
    if not (receipt.get("code") or {}).get("head"):
        problems.append("沒有記錄程式 HEAD")
    collections = receipt.get("collections") or {}
    for d in DOMAINS:
        if not (collections.get(d) or {}).get("chunk_count"):
            problems.append(f"{d} 領域沒有任何段落")
    if current is not None:
        if current.get("manifest_sha256") != receipt.get("manifest_sha256"):
            problems.append("目前載入的索引 manifest 和收據不同")
        if current.get("embed_mode") != receipt.get("embed_mode"):
            problems.append(
                f"EMBED_MODE 和收據不同：現在={current.get('embed_mode')}，"
                f"收據={receipt.get('embed_mode')}"
            )
        now = current.get("collections") or {}
        for d in DOMAINS:
            a, b = collections.get(d) or {}, now.get(d) or {}
            for key in ("chunk_count", "chunks_sha256"):
                if a.get(key) != b.get(key):
                    problems.append(f"{d} 索引的 {key} 和收據不同")
            if (a.get("chunk_vecs") or {}).get("sha256") != (b.get("chunk_vecs") or {}).get(
                "sha256"
            ):
                problems.append(f"{d} 索引的段落向量和收據不同")
    return problems


def collect_index_receipt(
    store,
    *,
    isolation: dict,
    qa_paths: Iterable[Path] = DEFAULT_QA,
    index_build: dict | None = None,
) -> dict:
    """對已載入（store.load()）的索引收集收據；必須先 bootstrap()。會讀 KB、QA、models.yaml 與 git。

    isolation 傳 bootstrap() 的回傳值；env_file_disabled 一律以收集當下的實際狀態覆寫。
    """
    if not env_file_disabled():
        raise RuntimeIsolationError("還沒有 bootstrap()：Settings 仍會讀 env_file")
    from app.core.config import get_models_config, get_settings, kb_version
    from app.rag.kb import current_kb_hash

    settings, cfg = get_settings(), get_models_config()
    manifest = dict(store.manifest)
    return assemble_index_receipt(
        manifest=manifest,
        manifest_problems=store.check_manifest(manifest),
        collections={"art": collection_digest(store.art), "mfg": collection_digest(store.mfg)},
        settings=settings_snapshot(settings),
        models=model_specs(cfg),
        retrieval_params=retrieval_params(cfg),
        models_yaml_sha256=sha256_file(settings.shared_dir / "models.yaml"),
        kb={
            "version": kb_version(),
            "hash_now": current_kb_hash(),
            "version_manifest": manifest.get("kb_version"),
            "hash_manifest": manifest.get("kb_hash"),
        },
        qa=file_hashes(qa_paths),
        code=git_provenance(),
        isolation={**isolation, "env_file_disabled": env_file_disabled()},
        index_build=index_build,
        created_at=utc_now(),
    )


def run_build_index() -> dict:
    """呼叫既有 pipelines/build_index.py 的 main([])（不改 pipeline）：重建 DATA_DIR/index，
    DATABASE_URL 有值時也寫進 PostgreSQL。必須先 bootstrap()。

    回傳：{"invoked": True, "command": "pipelines/build_index.py", "exit_code": int,
          "error": str|None, "started_at": str, "finished_at": str, "seconds": float}
    """
    if not env_file_disabled():
        raise RuntimeIsolationError("還沒有 bootstrap()：建索引會讀 .env")
    if str(ROOT / "pipelines") not in sys.path:
        sys.path.insert(0, str(ROOT / "pipelines"))
    import build_index

    started, t0, error = utc_now(), time.perf_counter(), None
    try:
        code = build_index.main([])
    except SystemExit as e:  # 標準模型執行失敗時 build_parts 以 SystemExit(訊息) 結束
        code = e.code if isinstance(e.code, int) else 1
        error = None if isinstance(e.code, int) else str(e.code)
    return {
        "invoked": True,
        "command": "pipelines/build_index.py",
        "exit_code": code,
        "error": error,
        "started_at": started,
        "finished_at": utc_now(),
        "seconds": round(time.perf_counter() - t0, 1),
    }


# ---------------------------------------------------------------- 題庫與 KB 讀取


def load_jsonl(path: Path) -> list[dict]:
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def load_kb_docs(domain: str, kb_root: Path = ROOT / "kb") -> dict[str, dict]:
    """{文件 ID: KB JSON}（kb/artworks 或 kb/parts）。"""
    d = Path(kb_root) / DOMAINS[domain]["kb_subdir"]
    docs = (json.loads(p.read_text(encoding="utf-8")) for p in sorted(d.glob("*.json")))
    return {doc["id"]: doc for doc in docs}


def load_staging_ids(domain: str, staging_root: Path = ROOT / "kb_staging") -> set[str]:
    """kb_staging 裡還沒收錄的文件 ID（題目指到它們時排除，不當負例）。"""
    return set(load_kb_docs(domain, staging_root))


def chunk_owner_map(chunks: Iterable[dict], owner_key: str) -> dict[str, str]:
    """{chunk_id: 文件 ID}，給 retrieval_metrics.find_leaks 用。"""
    return {c["chunk_id"]: c[owner_key] for c in chunks}


# ---------------------------------------------------------------- gold 推導（純函式）


def row_domain(row: dict) -> str | None:
    """part_id 非空→mfg、否則 artwork_id 非空→art（和 retrieve 一樣 part_id 優先）；都沒有→None。"""
    if row.get("part_id"):
        return "mfg"
    if row.get("artwork_id"):
        return "art"
    return None


def is_explicit_empty(row: dict) -> bool:
    return row.get("expect_refusal") is True or row.get("type") in EXPLICIT_EMPTY_TYPES


def _topic_problems(
    topic: str, owner: str, doc: dict, owner_chunks: Sequence[dict]
) -> tuple[list[tuple[str, str]], set[str]]:
    """一個 topic 的 (問題清單, 對到的 chunk_id)。問題是 (reason, detail)。"""
    want = {i for i, d in enumerate(doc.get("descriptions", [])) if d.get("topic") == topic}
    parsed = {c["chunk_id"]: _DESC_CHUNK.match(c["chunk_id"]) for c in owner_chunks}
    desc_index = {
        cid: int(m["i"]) for cid, m in parsed.items() if m is not None and m["owner"] == owner
    }
    if not want:
        system = [
            c["chunk_id"]
            for c in owner_chunks
            if c.get("topic") == topic and c["chunk_id"] not in desc_index
        ]
        if system:
            detail = f"{topic}：索引只有系統產生的段落 {', '.join(system)}，不是 KB descriptions"
            return [("system_generated_topic", detail)], set()
        return [
            ("topic_not_in_descriptions", f"{topic}：{owner} 的 descriptions 沒有這個 topic")
        ], set()
    problems, found = [], set()
    for i in sorted(want):
        matched = [c for c in owner_chunks if desc_index.get(c["chunk_id"]) == i]
        if not matched:
            detail = f"{topic}：descriptions[{i}] 在索引裡沒有段落 {owner}#{i:02d}-*"
            problems.append(("index_kb_mismatch", detail))
        for c in matched:
            if c.get("topic") != topic:
                detail = f"{topic}：索引 {c['chunk_id']} 的 topic 是 {c.get('topic')}"
                problems.append(("index_kb_mismatch", detail))
            found.add(c["chunk_id"])
    elsewhere = [
        c["chunk_id"]
        for c in owner_chunks
        if c.get("topic") == topic
        and c["chunk_id"] in desc_index
        and desc_index[c["chunk_id"]] not in want
    ]
    if elsewhere:
        detail = f"{topic}：索引 {', '.join(elsewhere)} 的位置和 KB descriptions 不同"
        problems.append(("index_kb_mismatch", detail))
    return problems, found


def resolve_gold(
    row: dict,
    *,
    domain: str,
    kb_docs: Mapping[str, dict],
    chunks: Sequence[dict],
    staging_ids: Iterable[str] = (),
) -> dict:
    """一題的完整 gold（還沒套身分可見性）。不手填、不猜：任一必要對應缺失就整題排除。

    row：題庫一列，欄位 id、question、artwork_id 或 part_id、topics，選填 expect_refusal、
    type（no_answer／unknown 表示答案為空；distractor 等其他值照常推導並原樣記在 qa_type）、split。
    domain："art"／"mfg"，題目領域不同就排除（例如畫作評估遇到圖紙題）。
    kb_docs：load_kb_docs(domain)；chunks：已載入索引的 coll.chunks；
    staging_ids：kb_staging 的文件 ID。

    回傳：
    {
      "case_id", "domain", "owner_id": str|None, "question", "topics": [str],
      "qa_type": str|None, "split": str|None, "explicit_empty": bool,
      "status": "answerable" | "expected_empty" | "excluded",
      "empty_kind": "no_answer" | "unknown" | None,    # 只有 expected_empty 有值
      "gold_chunk_ids": [str],                         # 索引順序；只有 answerable 非空
      "gold_chunk_owner": {chunk_id: 文件 ID}, "gold_chunk_topic": {chunk_id: topic},
      "multi_relevant": bool, "unreviewed_gold": bool,
      "excluded_reason": str|None, "excluded_detail": str|None
    }
    excluded_reason：domain_mismatch、missing_owner、conflicting_labels、owner_in_staging、
    owner_not_in_kb、owner_not_in_index、missing_topics、topic_not_in_descriptions、
    system_generated_topic（#meta、#color）、index_kb_mismatch。
    """
    owner_key = DOMAINS[domain]["owner_key"]
    owner = row.get("part_id") or row.get("artwork_id") or None
    topics = list(row.get("topics") or [])
    explicit = is_explicit_empty(row)
    out = {
        "case_id": row["id"],
        "domain": domain,
        "owner_id": owner,
        "question": row["question"],
        "topics": topics,
        "qa_type": row.get("type"),
        "split": row.get("split"),
        "explicit_empty": explicit,
        "status": "answerable",
        "empty_kind": None,
        "gold_chunk_ids": [],
        "gold_chunk_owner": {},
        "gold_chunk_topic": {},
        "multi_relevant": False,
        "unreviewed_gold": False,
        "excluded_reason": None,
        "excluded_detail": None,
    }

    def excluded(reason: str, detail: str) -> dict:
        return out | {"status": "excluded", "excluded_reason": reason, "excluded_detail": detail}

    def empty(kind: str) -> dict:
        return out | {"status": "expected_empty", "empty_kind": kind}

    if owner is None:
        return empty("no_answer") if explicit else excluded("missing_owner", "沒有文件 ID")
    if row_domain(row) != domain:
        return excluded("domain_mismatch", f"{owner} 屬於 {row_domain(row)}，不是 {domain}")
    if explicit and topics:
        return excluded("conflicting_labels", "標了答案為空，又列了 topics")
    in_kb = owner in kb_docs
    if not in_kb and owner in set(staging_ids):
        return excluded("owner_in_staging", f"{owner} 在 kb_staging，還沒收錄")
    owner_chunks = [c for c in chunks if c.get(owner_key) == owner]
    if not in_kb:
        if owner_chunks:
            return excluded("index_kb_mismatch", f"{owner} 不在 KB，索引卻有它的段落")
        if explicit:
            return empty("unknown")
        return excluded("owner_not_in_kb", f"{owner} 不在 KB")
    if row.get("type") == "unknown":
        return excluded("conflicting_labels", f"標成不存在的文件，但 {owner} 在 KB")
    if not owner_chunks:
        return excluded("owner_not_in_index", f"{owner} 在 KB，但索引沒有它的段落")
    if explicit:
        return empty("no_answer")
    if not topics:
        return excluded("missing_topics", "沒有 topics，也沒有標答案為空")
    problems, found = [], set()
    for t in topics:
        p, f = _topic_problems(t, owner, kb_docs[owner], owner_chunks)
        problems += p
        found |= f
    if problems:
        return excluded(problems[0][0], "；".join(d for _, d in problems))
    gold = [c for c in owner_chunks if c["chunk_id"] in found]
    return out | {
        "gold_chunk_ids": [c["chunk_id"] for c in gold],
        "gold_chunk_owner": {c["chunk_id"]: c[owner_key] for c in gold},
        "gold_chunk_topic": {c["chunk_id"]: c["topic"] for c in gold},
        "multi_relevant": len(gold) > 1,
        "unreviewed_gold": any(UNREVIEWED_MARK in t for t in topics),
    }


def resolve_gold_cases(
    rows: Sequence[dict],
    *,
    domain: str,
    kb_docs: Mapping[str, dict],
    chunks: Sequence[dict],
    staging_ids: Iterable[str] = (),
) -> dict:
    """整份題庫的 gold；題號重複直接報錯。

    回傳：{"included": [resolve_gold 結果（answerable＋expected_empty）],
          "excluded": [resolve_gold 結果（excluded，含原因）],
          "counts": {"rows", "answerable", "expected_empty", "excluded",
                     "excluded_by_reason": {reason: int}}}
    """
    dup = sorted(k for k, n in Counter(r["id"] for r in rows).items() if n > 1)
    if dup:
        raise ValueError(f"題號重複：{', '.join(dup)}")
    staging = set(staging_ids)
    resolved = [
        resolve_gold(r, domain=domain, kb_docs=kb_docs, chunks=chunks, staging_ids=staging)
        for r in rows
    ]
    excluded = [g for g in resolved if g["status"] == "excluded"]
    return {
        "included": [g for g in resolved if g["status"] != "excluded"],
        "excluded": excluded,
        "counts": {
            "rows": len(rows),
            "answerable": sum(g["status"] == "answerable" for g in resolved),
            "expected_empty": sum(g["status"] == "expected_empty" for g in resolved),
            "excluded": len(excluded),
            "excluded_by_reason": dict(
                sorted(Counter(g["excluded_reason"] for g in excluded).items())
            ),
        },
    }


def apply_visibility(case: dict, visible: Iterable[str] | None) -> dict:
    """原始 gold 完整解析後，再和身分看得到的文件求交集（回傳新 dict，不改原本的）。

    visible：看得到的文件 ID（例如 chat_service.visible_parts(scope)）；None＝不限（畫作）。
    - answerable 的 gold 全部看不到 → expected_empty、empty_kind="hidden"（合法空答案，不排除）
    - 部分看不到 → 保留看得到的，hidden_target_count 記少掉的段數
    - no_answer 但文件本身看不到 → empty_kind="hidden"；unknown 維持 unknown；excluded 原樣

    多出的欄位：
    {"gold_chunk_ids_all": [str]（套用前）, "original_empty_kind": str|None,
     "visibility_applied": bool, "hidden_target_count": int, "hidden_owner_ids": [str],
     "owner_visible": bool|None（沒有限制或沒有文件 ID 為 None）}
    gold_chunk_owner 保留全部（含看不到的）段落的文件對應。
    """
    out = dict(case) | {
        "gold_chunk_ids_all": list(case["gold_chunk_ids"]),
        "original_empty_kind": case["empty_kind"],
        "visibility_applied": visible is not None,
        "hidden_target_count": 0,
        "hidden_owner_ids": [],
        "owner_visible": None,
    }
    if case["status"] == "excluded" or visible is None:
        return out
    allowed = set(visible)
    owner_of = case["gold_chunk_owner"]
    keep = [c for c in case["gold_chunk_ids"] if owner_of[c] in allowed]
    hidden = [c for c in case["gold_chunk_ids"] if owner_of[c] not in allowed]
    owner_visible = None if case["owner_id"] is None else case["owner_id"] in allowed
    out |= {
        "gold_chunk_ids": keep,
        "hidden_target_count": len(hidden),
        "hidden_owner_ids": sorted({owner_of[c] for c in hidden}),
        "owner_visible": owner_visible,
        "multi_relevant": len(keep) > 1,
    }
    if case["status"] == "answerable" and not keep:
        out |= {"status": "expected_empty", "empty_kind": "hidden"}
    elif case["empty_kind"] == "no_answer" and owner_visible is False:
        out["empty_kind"] = "hidden"
    return out


# ---------------------------------------------------------------- 建索引入口


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="不讀 .env 建檢索評估用索引，並寫 real 索引收據")
    ap.add_argument("command", choices=["build-index", "receipt"])
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--out-dir", default=str(RUNS_DIR))
    ap.add_argument("--qa", action="append", default=None, help="題庫路徑（可重複）")
    ap.add_argument("--allow-mock", action="store_true", help="測試用：mock 收據不能寫進 eval/runs")
    args = ap.parse_args(argv)

    isolation = bootstrap(strict=True)
    from app.core.config import get_settings
    from app.repositories.index_store import IndexMismatch, get_store

    mode = get_settings().embed_mode
    out_dir = Path(args.out_dir)
    if mode != "real":
        if not args.allow_mock:
            print(f"EMBED_MODE={mode}：正式收據只接受 real", file=sys.stderr)
            return 2
        if is_official_runs_dir(out_dir):
            print("mock 收據不能寫進 eval/runs，請用 --out-dir 指到暫存目錄", file=sys.stderr)
            return 2
    build = {"invoked": False}
    if args.command == "build-index":
        build = run_build_index()
        if build["exit_code"] != 0:
            print(f"建索引失敗：{json.dumps(build, ensure_ascii=False)}", file=sys.stderr)
            return 1
    store = get_store()
    try:
        store.load()
    except IndexMismatch as e:
        print(f"索引不能用：{e}", file=sys.stderr)
        return 1
    qa_paths = [Path(p) for p in args.qa] if args.qa else list(DEFAULT_QA)
    receipt = collect_index_receipt(
        store, isolation=isolation, qa_paths=qa_paths, index_build=build
    )
    problems = verify_index_receipt(receipt, require_real=not args.allow_mock)
    if problems:
        print("收據檢查失敗，不寫檔：\n- " + "\n- ".join(problems), file=sys.stderr)
        return 1
    receipt["run_id"] = args.run_id or new_run_id()
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{receipt['run_id']}-retrieval-index.json"
    out.write_text(json.dumps(receipt, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"索引收據：{repo_relative(out)}（real={receipt['real']}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
