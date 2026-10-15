"""3D 重建結果快取＋品質閘門（TASK-20261014-002，docs/adr/042）。

模型一律用假的 provider（照 seed 回不同的程式碼）；沙箱照常執行（命中也要經過 check_code 與沙箱）。
背景工作（cad_job）的命中路徑在 test_jobs_runner.py（要真的 PostgreSQL）。
"""

import asyncio
import os
import sqlite3
import uuid

import pytest
from conftest import as_account

from app.cad import sandbox
from app.cad.holes import DETECT_FAILED_REASON
from app.cad.sandbox import LIMITS_AVAILABLE
from app.core.config import REPO_ROOT, get_models_config, get_settings
from app.jobs.sse_util import parse
from app.rag.providers import OpenAICompatProvider, Usage
from app.repositories.production_repo import get_production_repo
from app.services import cad_access, cad_cache, cad_service
from app.services.cad_cache import CadCache, Candidate, Run
from app.services.identity import get_account
from app.services.security import CadAccess

needs_limits = pytest.mark.skipif(
    not LIMITS_AVAILABLE, reason="這個平台限制不了子行程（Windows），模型產生的程式碼不執行"
)

BOX = 'import cadquery as cq\nsolid = cq.Workplane("XY").box(1.0, 0.6, 0.4)\n'
IDENTITY = {
    "alias": "ortho2cad",
    "model": {"path": "models/o2c.gguf", "size": 100, "mtime_ns": 1},
    "mmproj": {"path": "models/mmproj.gguf", "size": 10, "mtime_ns": 1},
}


def gt_code(part_id: str) -> str:
    return (REPO_ROOT / "kb" / "cad" / f"{part_id}.py").read_text(encoding="utf-8")


# ---------------------------------------------------------------- 單元：key
def fields(**over):
    base = dict(
        part_id="mfg-002",
        input_png=b"png-bytes",
        strategy="ortho2cad",
        model=IDENTITY,
        prompt="Generate the CADQuery code",
        max_pixels=50176,
        max_tokens=4096,
        preprocess_version=1,
    )
    return cad_cache.key_fields(**(base | over))


def test_key_contains_every_input_and_changes_with_each():
    f = fields()
    assert set(f) == {
        "part_id",
        "input_sha256",
        "strategy",
        "model",
        "prompt",
        "max_pixels",
        "max_tokens",
        "preprocess_version",
    }
    base = cad_cache.make_key(f)
    assert cad_cache.make_key(fields()) == base  # 同樣的輸入＝同一個 key
    changed = [
        fields(part_id="mfg-005"),  # 影像相同、不同圖紙
        fields(input_png=b"other"),
        fields(strategy="hybrid"),
        fields(model=IDENTITY | {"model": IDENTITY["model"] | {"size": 101}}),
        fields(model=IDENTITY | {"model": IDENTITY["model"] | {"mtime_ns": 2}}),
        fields(model=IDENTITY | {"mmproj": IDENTITY["mmproj"] | {"size": 11}}),
        fields(model=IDENTITY | {"mmproj": IDENTITY["mmproj"] | {"mtime_ns": 2}}),
        fields(model=IDENTITY | {"model": IDENTITY["model"] | {"path": "models/other.gguf"}}),
        fields(prompt="other prompt"),
        fields(max_pixels=40000),
        fields(max_tokens=2048),
        fields(preprocess_version=2),
    ]
    keys = {cad_cache.make_key(c) for c in changed}
    assert base not in keys and len(keys) == len(changed)


def test_model_identity_reads_router_ini_and_tracks_size_and_mtime(tmp_path, monkeypatch):
    gguf, mmproj = tmp_path / "o2c.gguf", tmp_path / "mmproj.gguf"
    gguf.write_bytes(b"x" * 10)
    mmproj.write_bytes(b"y" * 5)
    ini = tmp_path / "router.ini"
    ini.write_text(
        f"version = 1\n\n[ortho2cad]\nmodel = {gguf}\nmmproj = {mmproj}\nc = 4608\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(cad_cache, "ROUTER_INI", ini)
    a = cad_cache.model_identity("ortho2cad")
    assert a["model"]["size"] == 10 and a["mmproj"]["size"] == 5
    gguf.write_bytes(b"x" * 11)  # 換了 GGUF（大小）
    b = cad_cache.model_identity("ortho2cad")
    assert b != a
    os.utime(mmproj, ns=(1, 1))  # 換了 mmproj（mtime）
    assert cad_cache.model_identity("ortho2cad") != b
    assert cad_cache.model_identity("other-alias") is None  # 沒有這個區段
    mmproj.unlink()
    assert cad_cache.model_identity("ortho2cad") is None  # 檔案找不到＝身分不明
    monkeypatch.setattr(cad_cache, "ROUTER_INI", tmp_path / "missing.ini")
    assert cad_cache.model_identity("ortho2cad") is None


def test_repo_router_ini_has_the_ortho2cad_section():
    sections = cad_cache._router_sections(cad_cache.ROUTER_INI)
    assert sections["ortho2cad"]["model"].endswith(".gguf")
    assert sections["ortho2cad"]["mmproj"].endswith(".gguf")


def test_only_kb_drawings_with_ortho2cad_and_known_model_are_cached(monkeypatch):
    monkeypatch.setattr(cad_cache, "model_identity", lambda alias: IDENTITY)
    part = {"id": "mfg-002"}
    common = dict(input_png=b"x", prompt="p", max_pixels=1, max_tokens=1, has_gt=True)
    ok = cad_cache.context(
        strategy="ortho2cad", layout="kb", part=part, model_alias="ortho2cad", **common
    )
    assert ok is not None and ok.part_id == "mfg-002"
    for strategy, layout, p, alias in [
        ("hybrid", "kb", part, "ortho2cad"),  # 對照組
        ("ortho2cad", "rectified", part, "ortho2cad"),  # 照片拉正
        ("ortho2cad", "upload", None, "ortho2cad"),  # 未收錄的照片
        ("ortho2cad", "kb", part, None),  # mock 模式（沒有 provider）
    ]:
        assert (
            cad_cache.context(strategy=strategy, layout=layout, part=p, model_alias=alias, **common)
            is None
        )
    monkeypatch.setattr(cad_cache, "model_identity", lambda alias: None)
    assert (
        cad_cache.context(
            strategy="ortho2cad", layout="kb", part=part, model_alias="ortho2cad", **common
        )
        is None
    )


def test_same_image_on_another_part_never_hits_its_result(tmp_path, monkeypatch):
    """兩張圖紙送進模型的影像相同：B（沒有標準模型）不能沿用 A 採用的結果（Review High）。"""
    monkeypatch.setattr(cad_cache, "model_identity", lambda alias: IDENTITY)
    store = CadCache(tmp_path / "cache.sqlite3")
    monkeypatch.setattr(cad_cache, "get_cache", lambda: store)
    common = dict(
        strategy="ortho2cad",
        layout="kb",
        model_alias="ortho2cad",
        input_png=b"same-image",
        prompt="p",
        max_pixels=1,
        max_tokens=1,
        has_gt=False,
    )
    a = cad_cache.context(part={"id": "part-A"}, **common)
    b = cad_cache.context(part={"id": "part-B"}, **common)
    assert a.key != b.key
    store.put(
        a.key,
        part_id="part-A",
        fields=a.fields,
        code="A-only-code",
        iou=None,
        candidate=Candidate(0.0),
        source="adopted",
    )
    assert a.lookup().code == "A-only-code"
    assert b.lookup() is None
    # 就算 key 撞在一起（舊資料、雜湊碰撞），讀取時也只認自己的零件
    assert store.get(a.key, part_id="part-B") is None
    assert store.get(a.key, part_id="part-A").code == "A-only-code"


# ---------------------------------------------------------------- 單元：閘門
GOOD = {"ok": True, "valid": True, "iou": 0.8, "holes": {"status": "applied", "detected": 2}}


@pytest.mark.parametrize(
    "result,reason",
    [
        (GOOD | {"ok": False}, "執行失敗"),
        (GOOD | {"valid": False}, "實體無效"),
        (GOOD | {"valid": None}, "實體無效"),
        (GOOD | {"iou": 0.59}, "低於門檻"),
        (GOOD | {"iou": None}, "低於門檻"),
        (
            GOOD | {"holes": {"status": "skipped", "reason": "孔位落在模型外", "detected": 2}},
            "孔位",
        ),
        (
            GOOD | {"holes": {"status": "skipped", "reason": DETECT_FAILED_REASON, "detected": 0}},
            "孔位",
        ),
    ],
)
def test_gate_rejects_bad_results(result, reason):
    ok, why = cad_cache.gate(result, has_gt=True, min_iou=0.6)
    assert not ok and reason in why


def test_gate_accepts_good_results_and_no_holes_is_not_a_failure():
    assert cad_cache.gate(GOOD, has_gt=True, min_iou=0.6) == (True, None)
    no_holes = GOOD | {
        "holes": {"status": "skipped", "reason": "俯視圖沒有偵測到圓孔", "detected": 0}
    }
    assert cad_cache.gate(no_holes, has_gt=True, min_iou=0.6) == (True, None)
    assert cad_cache.gate(GOOD | {"iou": 0.6}, has_gt=True, min_iou=0.6)[0]
    # 沒有標準模型：不自動寫入（要主管採用）
    ok, why = cad_cache.gate(GOOD | {"iou": None}, has_gt=False)
    assert not ok and "採用" in why


def test_gate_uses_configured_threshold():
    assert cad_cache.cache_spec().min_iou == pytest.approx(0.6)
    assert not cad_cache.gate(GOOD | {"iou": 0.55}, has_gt=True)[0]


# ---------------------------------------------------------------- 單元：儲存
@pytest.fixture
def cache(tmp_path):
    return CadCache(tmp_path / "cache.sqlite3")


def test_put_get_and_only_better_iou_replaces(cache):
    g = Candidate(0.0)
    e = cache.put("k", part_id="p", fields={}, code="a", iou=0.7, candidate=g, source="auto")
    assert e and cache.get("k").code == "a"
    assert (
        cache.put("k", part_id="p", fields={}, code="b", iou=0.65, candidate=g, source="auto")
        is None
    )
    s2 = Candidate(0.7, 2)
    assert cache.put("k", part_id="p", fields={}, code="c", iou=0.9, candidate=s2, source="auto")
    hit = cache.get("k")
    assert hit.code == "c" and hit.candidate == s2 and hit.info()["candidate"]["seed"] == 2
    # 採用（沒有 IoU）不會蓋掉已經有的那一筆
    assert (
        cache.put("k", part_id="p", fields={}, code="d", iou=None, candidate=g, source="adopted")
        is None
    )


def test_rejected_code_never_hits_or_gets_written_again(cache):
    g = Candidate(0.0)
    cache.put("k", part_id="p", fields={}, code="a", iou=0.9, candidate=g, source="auto")
    assert cache.reject("k", cad_cache.code_sha("a"), job_id="cad_x", actor_id="manager")
    assert cache.get("k") is None
    assert (
        cache.put("k", part_id="p", fields={}, code="a", iou=0.95, candidate=g, source="auto")
        is None
    )
    assert cache.get("k") is None
    # 別的程式碼照常可以寫入
    assert cache.put("k", part_id="p", fields={}, code="b", iou=0.7, candidate=g, source="auto")
    assert cache.get("k").code == "b"


def test_rejected_entry_left_in_table_is_not_served(cache):
    """即使快取表裡還留著一筆（例如舊版本寫入），被否決的雜湊也不會命中。"""
    g = Candidate(0.0)
    cache.put("k", part_id="p", fields={}, code="a", iou=0.9, candidate=g, source="auto")
    with cache._conn() as conn:
        conn.execute(
            "INSERT INTO rejected (key, code_sha, created_at) VALUES (?,?,?)",
            ("k", cad_cache.code_sha("a"), "t"),
        )
    assert cache.get("k") is None


def test_next_seed_skips_tried_and_stops_at_limit(cache):
    assert cache.next_seed("k", 3) == 1
    cache.mark_tried("k", 1)
    cache.mark_tried("k", 3)
    assert cache.next_seed("k", 3) == 2
    cache.mark_tried("k", 2)
    assert cache.next_seed("k", 3) is None
    assert cache.next_seed("other", 3) == 1


def test_cache_file_is_outside_download_paths():
    path = get_settings().cad_cache_dir / cad_cache.CACHE_FILE
    jobs = get_settings().cad_jobs_dir.resolve()
    assert jobs not in path.resolve().parents
    assert cad_cache.CACHE_FILE not in cad_access.JOB_FILES


# ---------------------------------------------------------------- 單元：provider
def test_provider_sends_temperature_and_seed():
    p = OpenAICompatProvider(strategy="ortho2cad", model="ortho2cad")
    p.temperature, p.seed, p.max_tokens = 0.7, 3, 4096
    body = p.body([{"role": "user", "content": "x"}])
    assert body["temperature"] == 0.7 and body["seed"] == 3 and body["max_tokens"] == 4096
    p.seed, p.temperature = None, 0
    body = p.body([{"role": "user", "content": "x"}])
    assert "seed" not in body and body["temperature"] == 0


# ---------------------------------------------------------------- 整合：_reconstruct_stream
class FakeProvider:
    """Ortho2CAD 的替身：記下每次的 temperature／seed，照 seed 回程式碼。"""

    def __init__(self, codes: dict):
        self.model = "ortho2cad"
        self.codes = codes  # {seed（None＝greedy）: 程式碼}
        self.calls: list[tuple] = []
        self.usage = Usage()
        self.max_tokens = self.temperature = self.seed = None

    async def stream(self, messages):
        self.calls.append((self.temperature, self.seed))
        code = self.codes.get(self.seed, self.codes.get("default", BOX))
        for i in range(0, len(code), 40):
            yield code[i : i + 40]


@pytest.fixture
def env(tmp_path, monkeypatch):
    """真模型模式（llm_mode 不是 mock）＋假的 provider、模型身分與獨立的快取檔。"""
    monkeypatch.setattr(get_settings(), "llm_mode", "local")
    ident = {"value": IDENTITY}
    monkeypatch.setattr(cad_cache, "model_identity", lambda alias: ident["value"])
    store = CadCache(tmp_path / "cache.sqlite3")
    monkeypatch.setattr(cad_cache, "get_cache", lambda: store)
    provider = FakeProvider({None: gt_code("mfg-002")})
    monkeypatch.setattr(cad_service, "get_provider", lambda strategy: provider)
    checked: list[str] = []
    real_check = sandbox.check_code
    monkeypatch.setattr(
        sandbox, "check_code", lambda code: (checked.append(code), real_check(code))
    )
    return {
        "provider": provider,
        "cache": store,
        "ident": ident,
        "checked": checked,
        "tmp": tmp_path,
    }


def reconstruct(env, part="mfg-002", account=None, **kw) -> dict:
    job_id = "cad_" + uuid.uuid4().hex[:16]

    async def go():
        return [
            parse(raw)
            async for raw in cad_service._reconstruct_stream(
                "req_test",
                part_id=part,
                account=account,
                job_id=job_id,
                out_dir=env["tmp"] / job_id,
                **kw,
            )
        ]

    events = asyncio.run(go())
    out = {"job_id": job_id, "events": events, "kinds": [n for n, _ in events]}
    for name, data in events:
        out.setdefault(name, data)
    return out


@needs_limits
def test_miss_then_hit_skips_generation_but_still_runs_check_and_sandbox(env):
    first = reconstruct(env)
    assert first["result"]["ok"] and first["result"]["iou"] > 0.95
    assert first["result"]["cache"]["status"] == "stored"
    assert first["result"]["cache"]["candidate"] == {
        "kind": "greedy",
        "temperature": 0.0,
        "seed": None,
    }
    assert env["provider"].calls == [(0.0, None)]
    env["checked"].clear()

    second = reconstruct(env)
    assert env["provider"].calls == [(0.0, None)]  # 沒有再呼叫模型
    assert "token" not in second["kinds"] and second["executing"]["cached"] is True
    cache = second["result"]["cache"]
    assert cache["status"] == "hit" and cache["iou"] > 0.95
    assert cache["created_at"] == first["result"]["cache"]["created_at"]
    assert second["result"]["ok"] and second["result"]["iou"] > 0.95  # 沙箱重跑的結果
    assert env["checked"] == [gt_code("mfg-002").strip()]  # 命中的程式碼照樣經過安全檢查
    assert (env["tmp"] / second["job_id"] / "model.stl").is_file()
    assert second["done"]["latency_ms"]["generation"] == 0


@needs_limits
def test_any_key_change_misses(env, monkeypatch):
    reconstruct(env)
    assert len(env["provider"].calls) == 1
    env["ident"]["value"] = IDENTITY | {"model": IDENTITY["model"] | {"mtime_ns": 99}}
    assert reconstruct(env)["result"]["cache"]["status"] == "stored"  # 換了模型：沒命中
    assert len(env["provider"].calls) == 2
    cad = dict(get_models_config().cad)
    monkeypatch.setattr(get_models_config(), "cad", cad | {"max_tokens": 2048})
    reconstruct(env)
    assert len(env["provider"].calls) == 3  # 換了 max_tokens：沒命中
    monkeypatch.setattr(get_models_config(), "cad", cad | {"prompt": cad["prompt"] + " "})
    reconstruct(env)
    assert len(env["provider"].calls) == 4  # 換了 prompt：沒命中
    monkeypatch.setattr(get_models_config(), "cad", cad)
    assert reconstruct(env)["result"]["cache"]["status"] == "hit"  # 原本的 key 仍在
    assert len(env["provider"].calls) == 4


@needs_limits
@pytest.mark.parametrize(
    "code,reason",
    [
        (BOX, "孔位校正失敗"),  # 可執行，但外形和圖紙對不上：孔位校正不了
        ("import cadquery as cq\nsolid = cq.Workplane().box(1, 1, 1)\nx = 1 / 0\n", "執行失敗"),
        ("import os\nsolid = None\n", "執行失敗"),  # 安全檢查沒過
    ],
)
def test_failing_results_are_not_written_or_hit(env, code, reason):
    env["provider"].codes = {None: code}
    first = reconstruct(env)
    assert first["result"]["cache"]["status"] == "not_stored"
    assert reason in first["result"]["cache"]["reason"]
    assert not first["result"]["cache"]["adoptable"]  # 有標準模型的零件不能手動採用
    reconstruct(env)
    assert len(env["provider"].calls) == 2  # 沒有被命中，再生成一次


@needs_limits
def test_low_iou_is_not_written(env, monkeypatch):
    real = cad_service.run_cad

    async def low(*a, **k):
        r = await real(*a, **k)
        r.data["iou"] = 0.3
        return r

    monkeypatch.setattr(cad_service, "run_cad", low)
    first = reconstruct(env)
    assert first["result"]["cache"]["status"] == "not_stored"
    assert "IoU 0.30 低於門檻 0.60" in first["result"]["cache"]["reason"]
    reconstruct(env)
    assert len(env["provider"].calls) == 2


@needs_limits
def test_hole_failure_is_not_written(env, monkeypatch):
    real = cad_service.run_cad

    async def holes_failed(*a, **k):
        r = await real(*a, **k)
        r.data["holes"] = {"status": "skipped", "reason": "孔位落在模型外", "detected": 2}
        return r

    monkeypatch.setattr(cad_service, "run_cad", holes_failed)
    first = reconstruct(env)
    assert first["result"]["ok"] and first["result"]["iou"] > 0.95
    assert first["result"]["cache"]["status"] == "not_stored"
    assert "孔位" in first["result"]["cache"]["reason"]
    reconstruct(env)
    assert len(env["provider"].calls) == 2


@needs_limits
def test_hit_that_fails_the_gate_again_is_evicted_and_regenerated(env, monkeypatch):
    reconstruct(env)
    spec = cad_cache.CacheSpec(min_iou=1.5)  # 門檻調高：快取裡那一筆重跑後不合格
    monkeypatch.setattr(cad_cache, "cache_spec", lambda: spec)
    second = reconstruct(env)
    assert len(env["provider"].calls) == 2  # 移除後改由模型重新產生
    assert second["result"]["cache"]["status"] == "not_stored"
    assert env["cache"].get(env["cache"].run_of(second["job_id"]).key) is None
    assert second["kinds"].count("result") == 1 and second["kinds"].count("executing") == 2


@needs_limits
def test_regenerate_samples_next_untried_seed_and_never_hits(env, monkeypatch):
    env["provider"].codes = {None: BOX, 1: BOX + "# s1\n", 2: gt_code("mfg-002")}
    greedy = reconstruct(env)
    assert greedy["result"]["cache"]["status"] == "not_stored"
    r1 = reconstruct(env, regenerate=True)
    assert env["provider"].calls[-1] == (0.7, 1)
    assert r1["result"]["cache"]["candidate"] == {"kind": "sample", "temperature": 0.7, "seed": 1}
    r2 = reconstruct(env, regenerate=True)
    assert env["provider"].calls[-1] == (0.7, 2)
    assert r2["result"]["cache"]["status"] == "stored"
    # 一般重建命中重抽出來的結果，標示候選來源
    hit = reconstruct(env)
    assert hit["result"]["cache"]["status"] == "hit"
    assert hit["result"]["cache"]["candidate"]["seed"] == 2
    # 重新生成不查快取
    n = len(env["provider"].calls)
    monkeypatch.setattr(cad_cache, "resample_spec", lambda: cad_cache.ResampleSpec(max_seeds=3))
    reconstruct(env, regenerate=True)
    assert len(env["provider"].calls) == n + 1 and env["provider"].calls[-1] == (0.7, 3)
    exhausted = reconstruct(env, regenerate=True)
    assert exhausted["error"]["code"] == "CAD_RESAMPLE_EXHAUSTED"
    assert len(env["provider"].calls) == n + 1


@needs_limits
def test_rejected_greedy_is_marked_and_not_written_again(env):
    first = reconstruct(env)
    run = env["cache"].run_of(first["job_id"])
    assert run and run.code_sha == cad_cache.code_sha(gt_code("mfg-002").strip())
    env["cache"].reject(run.key, run.code_sha, job_id=first["job_id"], actor_id="manager")
    again = reconstruct(env)  # 沒命中：greedy 跑出同一份程式碼
    assert len(env["provider"].calls) == 2
    assert again["result"]["cache"]["status"] == "rejected_before"
    assert env["cache"].get(run.key) is None


def test_scope_check_happens_before_cache_lookup(env, monkeypatch):
    looked: list = []
    monkeypatch.setattr(cad_cache, "context", lambda **k: looked.append(k))
    out = reconstruct(env, part="mfg-002", account=get_account("sales"))  # 業務看不到機密圖紙
    assert out["kinds"] == ["error"] and out["error"]["code"] == "DATA_SCOPE_DENIED"
    assert looked == [] and env["provider"].calls == []


def test_mock_mode_does_not_touch_the_cache(env, monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_mode", "mock")
    touched: list = []
    monkeypatch.setattr(cad_cache, "get_cache", lambda: touched.append(1))
    monkeypatch.setattr(sandbox, "check_code", lambda code: None)

    async def fake_run(code, job_dir, **k):
        return sandbox.RunResult(True, None, {"valid": True, "iou": 0.99})

    monkeypatch.setattr(cad_service, "run_cad", fake_run)
    out = reconstruct(env)
    assert "cache" not in out["result"] and touched == []


# ---------------------------------------------------------------- API：作廢、採用
def legacy_job(owner: str, part: str, code: str) -> str:
    """舊 SSE 入口留下的工作（工作資料夾裡有 access.json 與 code.py）。"""
    job_id = "cad_" + uuid.uuid4().hex[:16]
    cad_access.write_access(job_id, CadAccess(job_id, owner, part, None))
    (cad_access.job_dir(job_id) / "code.py").write_text(code, encoding="utf-8")
    return job_id


def record(store: CadCache, job_id: str, code: str, *, has_gt: bool, basic_ok=True, key="k1"):
    store.record_run(
        Run(
            job_id,
            key,
            {"input_sha256": "x"},
            "mfg-005",
            cad_cache.code_sha(code),
            Candidate(0.7, 4),
            None,
            has_gt,
            basic_ok,
        )
    )


@pytest.fixture
def api_cache(tmp_path, monkeypatch, client):
    store = CadCache(tmp_path / "cache.sqlite3")
    monkeypatch.setattr(cad_cache, "get_cache", lambda: store)
    yield store
    as_account(client, "manager")


def audits(op="cad_cache"):
    return [a for a in get_production_repo().audit(200) if a["op"] == op]


def test_reject_requires_read_access_and_is_audited(client, api_cache):
    job = legacy_job("manager", "mfg-002", BOX)
    record(api_cache, job, BOX, has_gt=True, key="kr")
    api_cache.put(
        "kr",
        part_id="mfg-002",
        fields={},
        code=BOX,
        iou=0.9,
        candidate=Candidate(0.0),
        source="auto",
    )
    as_account(client, "sales")  # 看不到機密圖紙：和不存在一樣
    r = client.post(f"/api/v1/cad/jobs/{job}/cache/reject")
    assert r.status_code == 404 and r.json()["error"]["code"] == "CAD_JOB_NOT_FOUND"
    as_account(client, "guest")  # 沒有工廠圖紙領域
    assert client.post(f"/api/v1/cad/jobs/{job}/cache/reject").status_code == 403
    assert api_cache.get("kr") is not None
    before = len(audits())
    as_account(client, "warehouse")  # 讀得到這個工作就能作廢（不需要 kb_intake）
    r = client.post(f"/api/v1/cad/jobs/{job}/cache/reject")
    assert r.status_code == 200
    assert r.json() == {"job_id": job, "rejected": True, "removed": True, "audit": "recorded"}
    assert api_cache.get("kr") is None and api_cache.is_rejected("kr", cad_cache.code_sha(BOX))
    rows = audits()
    assert len(rows) == before + 1
    assert rows[0]["action"] == "作廢" and rows[0]["actor_id"] == "warehouse"
    assert rows[0]["ref_no"] == job and rows[0]["detail"]["seed"] == 4


def test_reject_and_adopt_refuse_jobs_outside_the_cache(client, api_cache):
    job = legacy_job("manager", "mfg-002", BOX)  # 沒有候選紀錄（照片、對照組、mock）
    for action in ("reject", "adopt"):
        r = client.post(f"/api/v1/cad/jobs/{job}/cache/{action}")
        assert r.status_code == 409 and r.json()["error"]["code"] == "CAD_CACHE_NOT_APPLICABLE"
    record(api_cache, job, BOX + "# 不是畫面上那一份\n", has_gt=False)
    r = client.post(f"/api/v1/cad/jobs/{job}/cache/adopt")
    assert r.json()["error"]["code"] == "CAD_CACHE_NOT_APPLICABLE"
    assert client.post("/api/v1/cad/jobs/cad_nope/cache/reject").status_code == 404


def test_adopt_requires_kb_intake_and_is_audited(client, api_cache):
    job = legacy_job("manager", "mfg-005", BOX)
    record(api_cache, job, BOX, has_gt=False, key="ka")
    as_account(client, "warehouse")  # 讀得到，但沒有 kb_intake
    r = client.post(f"/api/v1/cad/jobs/{job}/cache/adopt")
    assert r.status_code == 403 and r.json()["error"]["code"] == "PERMISSION_DENIED"
    assert api_cache.get("ka") is None
    as_account(client, "manager")
    before = len(audits())
    r = client.post(f"/api/v1/cad/jobs/{job}/cache/adopt")
    assert r.status_code == 200 and r.json()["adopted"] is True
    hit = api_cache.get("ka")
    assert hit.code == BOX and hit.source == "adopted" and hit.created_by == "manager"
    rows = audits()
    assert len(rows) == before + 1 and rows[0]["action"] == "採用"
    assert rows[0]["actor_id"] == "manager" and rows[0]["ref_no"] == job
    r = client.post(f"/api/v1/cad/jobs/{job}/cache/adopt")  # 已經有了
    assert r.status_code == 409 and r.json()["error"]["code"] == "CAD_CACHE_EXISTS"


def test_adopt_refuses_gt_parts_failed_results_and_rejected_code(client, api_cache):
    as_account(client, "manager")
    gt_job = legacy_job("manager", "mfg-005", BOX)
    record(api_cache, gt_job, BOX, has_gt=True, key="k_gt")
    r = client.post(f"/api/v1/cad/jobs/{gt_job}/cache/adopt")
    assert r.status_code == 409 and r.json()["error"]["code"] == "CAD_CACHE_NOT_ADOPTABLE"
    bad_job = legacy_job("manager", "mfg-005", BOX)
    record(api_cache, bad_job, BOX, has_gt=False, basic_ok=False, key="k_bad")
    assert client.post(f"/api/v1/cad/jobs/{bad_job}/cache/adopt").status_code == 409
    rej_job = legacy_job("manager", "mfg-005", BOX)
    record(api_cache, rej_job, BOX, has_gt=False, key="k_rej")
    assert client.post(f"/api/v1/cad/jobs/{rej_job}/cache/reject").status_code == 200
    r = client.post(f"/api/v1/cad/jobs/{rej_job}/cache/adopt")
    assert r.status_code == 409 and "不對" in r.json()["error"]["message"]
    assert api_cache.get("k_gt") is None and api_cache.get("k_bad") is None
    assert api_cache.get("k_rej") is None
    assert not [a for a in audits() if a["action"] == "採用" and a["ref_no"] in (gt_job, bad_job)]


@needs_limits
def test_end_to_end_sse_hit_reject_and_regenerate(client, env, monkeypatch):
    """舊 SSE 入口：第一次寫入、第二次命中；作廢後同一份程式碼不再命中、不再寫入。"""
    as_account(client, "manager")

    def post(**body):
        r = client.post("/api/v1/cad/reconstruct", json={"part_id": "mfg-002", **body})
        ev = dict((n, d) for n, d in (parse(b) for b in r.text.strip().split("\n\n")))
        return ev

    first = post()
    assert first["result"]["cache"]["status"] == "stored"
    second = post()
    assert second["result"]["cache"]["status"] == "hit" and len(env["provider"].calls) == 1
    job = second["done"]["job_id"]
    r = client.post(f"/api/v1/cad/jobs/{job}/cache/reject")
    assert r.status_code == 200 and r.json()["removed"] is True
    third = post()
    assert len(env["provider"].calls) == 2
    assert third["result"]["cache"]["status"] == "rejected_before"
    assert post()["result"]["cache"]["status"] == "rejected_before"


# ------------------------------------------- 稽核寫不進去（Architecture High）：不能悄悄變更快取
@pytest.fixture
def audit_down(monkeypatch):
    """生產資料庫的稽核寫入故障；state["down"] 改成 False 就恢復。"""
    repo = get_production_repo()
    real = repo.add_audit
    state = {"down": True, "calls": 0}

    def add_audit(row, *a, **k):
        state["calls"] += 1
        if state["down"]:
            raise OSError("audit store unavailable")
        return real(row, *a, **k)

    monkeypatch.setattr(repo, "add_audit", add_audit)
    return state


def test_reject_with_audit_store_down_keeps_audit_pending_and_delivers_once(
    client, api_cache, audit_down
):
    job = legacy_job("manager", "mfg-002", BOX)
    record(api_cache, job, BOX, has_gt=True, key="k_down")
    g = Candidate(0.0)
    api_cache.put(
        "k_down", part_id="mfg-002", fields={}, code=BOX, iou=0.9, candidate=g, source="a"
    )
    before = len(audits())
    as_account(client, "warehouse")
    r = client.post(f"/api/v1/cad/jobs/{job}/cache/reject")
    # 否決和稽核已經在同一個交易存下：回 200，但明白告訴呼叫端稽核還沒寫進稽核紀錄
    assert r.status_code == 200 and r.json()["audit"] == "pending"
    assert api_cache.is_rejected("k_down", cad_cache.code_sha(BOX))
    pending = api_cache.pending_audit()
    assert len(pending) == 1 and pending[0][1]["action"] == "作廢"
    assert pending[0][1]["ref_no"] == job and pending[0][1]["detail"]["removed"] is True
    assert len(audits()) == before  # 稽核資料庫還沒有
    # 恢復後：主管讀稽核紀錄之前先補送，只有一筆
    audit_down["down"] = False
    as_account(client, "manager")
    items = client.get("/api/v1/audit", params={"limit": 200}).json()["items"]
    assert [a["ref_no"] for a in items if a["op"] == "cad_cache"].count(job) == 1
    assert api_cache.pending_audit() == []
    assert cad_cache.flush_audit() == 0
    assert len(audits()) == before + 1  # 再補送也不重複


def test_adopt_with_audit_store_down_is_recorded_later(client, api_cache, audit_down):
    job = legacy_job("manager", "mfg-005", BOX)
    record(api_cache, job, BOX, has_gt=False, key="k_adopt_down")
    as_account(client, "manager")
    before = len(audits())
    r = client.post(f"/api/v1/cad/jobs/{job}/cache/adopt")
    assert r.status_code == 200 and r.json()["audit"] == "pending"
    assert api_cache.get("k_adopt_down").source == "adopted"
    assert [row["action"] for _, row in api_cache.pending_audit()] == ["採用"]
    assert len(audits()) == before
    audit_down["down"] = False
    assert cad_cache.flush_audit() == 0  # 背景維護執行緒每一輪呼叫的同一個函式
    rows = [a for a in audits() if a["ref_no"] == job]
    assert len(rows) == 1 and rows[0]["action"] == "採用" and rows[0]["actor_id"] == "manager"


def test_audit_enqueue_failure_rolls_back_the_cache_change(cache, monkeypatch):
    """稽核放不進待送清單（同一個交易）：否決、採用一起回滾，不會只改了快取。"""
    g = Candidate(0.0)
    cache.put("k", part_id="p", fields={}, code="a", iou=0.9, candidate=g, source="auto")

    def boom(*a, **k):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(CadCache, "_enqueue", staticmethod(boom))
    row = {"action": "作廢", "op": "cad_cache", "ref_no": "cad_x", "request_id": "r1"}
    with pytest.raises(sqlite3.OperationalError):
        cache.reject("k", cad_cache.code_sha("a"), job_id="cad_x", actor_id="m", audit=row)
    assert not cache.is_rejected("k", cad_cache.code_sha("a")) and cache.get("k").code == "a"
    with pytest.raises(sqlite3.OperationalError):
        cache.put(
            "k2",
            part_id="p",
            fields={},
            code="b",
            iou=None,
            candidate=g,
            source="adopted",
            audit=row | {"action": "採用"},
        )
    assert cache.get("k2") is None


def test_redelivery_of_the_same_audit_is_not_duplicated(cache, monkeypatch):
    """寫進稽核紀錄之後、標記送出之前中斷：下一次補送以 request_id 去重，不會多一筆。"""
    monkeypatch.setattr(cad_cache, "get_cache", lambda: cache)
    row = {
        "actor_id": "manager",
        "actor_label": "主管",
        "action": "作廢",
        "op": "cad_cache",
        "ref_no": "cad_dup_0000000001",
        "summary": "s",
        "detail": None,
        "request_id": "req_dup_1",
    }
    with cache._conn() as conn:
        CadCache._enqueue(conn, row, "outbox-1")
    get_production_repo().add_audit(row, once_request=True)  # 第一次送出成功，但沒標記
    assert cad_cache.flush_audit() == 0
    assert len([a for a in audits() if a["ref_no"] == "cad_dup_0000000001"]) == 1
    # 不同請求作用在同一個工作：各記一筆
    get_production_repo().add_audit(row | {"request_id": "req_dup_2"}, once_request=True)
    assert len([a for a in audits() if a["ref_no"] == "cad_dup_0000000001"]) == 2


# ------------------------------------------- make cad-warm 退出碼（Architecture Medium）
@pytest.mark.parametrize(
    "statuses,code",
    [
        (["stored"], 0),
        (["hit", "stored"], 0),
        (["error"], 1),
        (["not_stored"], 1),
        (["not_cacheable"], 1),
        (["stored", "error"], 1),
    ],
)
def test_cad_warm_exit_code_reflects_failed_parts(monkeypatch, capsys, statuses, code):
    import cad_warm

    from app.repositories.index_store import get_store

    monkeypatch.setattr(get_settings(), "llm_mode", "local")
    monkeypatch.setattr(cad_cache, "model_identity", lambda alias: IDENTITY)
    monkeypatch.setattr(get_store(), "load", lambda: None)
    parts = ["mfg-001", "mfg-002"][: len(statuses)]
    it = iter(statuses)

    async def fake_warm(part, rs):
        return {"part_id": part["id"], "status": next(it), "tries": []}

    monkeypatch.setattr(cad_warm, "warm_part", fake_warm)
    assert asyncio.run(cad_warm.main_async(parts)) == code
    if code:
        assert "預熱沒有完成" in capsys.readouterr().err


def test_cad_warm_skipped_parts_without_gt_do_not_fail(monkeypatch, tmp_path):
    import cad_warm

    from app.repositories.index_store import get_store

    monkeypatch.setattr(get_settings(), "llm_mode", "local")
    monkeypatch.setattr(cad_cache, "model_identity", lambda alias: IDENTITY)
    store = get_store()
    monkeypatch.setattr(store, "load", lambda: None)
    monkeypatch.setattr(type(store), "parts_dir", property(lambda self: tmp_path))  # 沒有 gt/

    async def never(part, rs):
        raise AssertionError("沒有標準模型的零件不預熱")

    monkeypatch.setattr(cad_warm, "warm_part", never)
    assert asyncio.run(cad_warm.main_async(["mfg-001"])) == 0


def test_cad_warm_refuses_mock_mode():
    import cad_warm

    assert get_settings().llm_mode == "mock"
    assert asyncio.run(cad_warm.main_async(["mfg-001"])) == 2
