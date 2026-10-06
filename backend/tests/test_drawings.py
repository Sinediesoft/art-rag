"""工廠機械加工圖：知識庫、圖紙驗證、CadQuery 沙箱、3D 重建串流、圖紙問答（一律 mock 模型）。"""

import asyncio
import json
import random
import signal
import sys
from pathlib import PurePath, PureWindowsPath

import pytest
from PIL import Image

from app.cad import sandbox
from app.cad.sandbox import (
    LIMITS_AVAILABLE,
    UnsafeCode,
    WslTarget,
    check_code,
    extract_code,
    run_cad,
    wsl_path,
)
from app.core.config import REPO_ROOT
from app.rag import verify
from app.rag.chunking import build_part_chunks
from app.rag.kb import validate_parts
from app.services.cad_service import wsl_target

# Windows 沒有 resource 模組，限制不了子行程的 CPU 與檔案大小：模型產生的程式碼要在 WSL2 執行
# （.env 設 CAD_WSL_PYTHON，docs/adr/027），沒設就一律不執行
WSL = None if LIMITS_AVAILABLE else wsl_target()
needs_limits = pytest.mark.skipif(
    not LIMITS_AVAILABLE and WSL is None,
    reason="這個平台沒有 resource 模組、也沒設 CAD_WSL_PYTHON，模型產生的程式碼不執行",
)

GEOMETRY = {"width": 1, "depth": 2, "height": 3, "volume_mm3": 6, "weight_kg": 0.1, "faces": 6}


def parse_sse(text: str) -> list[tuple[str, dict]]:
    events = []
    for block in text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        events.append((lines["event"], json.loads(lines["data"])))
    return events


def test_parts_pass_schema_and_have_drawings():
    ok, errors = validate_parts()
    assert errors == []
    assert len(ok) >= 6
    for p in ok:
        assert (REPO_ROOT / p["cad"]).is_file() and (REPO_ROOT / p["drawing"]).is_file()


def test_part_chunks_cite_internal_documents():
    p = validate_parts()[0][0]
    chunks = build_part_chunks({**p, "geometry": GEOMETRY})
    assert chunks[0]["topic"] == "基本資料" and "1 × 2 × 3 mm" in chunks[0]["text"]
    assert all(c["source_url"] is None and c["source"] and c["part_id"] == p["id"] for c in chunks)


@pytest.mark.parametrize(
    "code",
    [
        "import os\nos.system('ls')",
        "x = ().__class__.__bases__",
        "open('/etc/passwd').read()",
        "import cadquery as cq\ncq.exporters.export(1, '/tmp/x.stl')",
        "from cadquery import exporters",
        "import cadquery as cq\nsolid = cq.Workplane().box(1, 1, 1).val().exportStl('/tmp/x')",
        "getattr(__builtins__, 'eval')('1')",
        "'{0.__class__}'.format(1)",
    ],
)
def test_sandbox_rejects_unsafe_code(code):
    with pytest.raises(UnsafeCode):
        check_code(code)


def test_extract_code_from_markdown_fence():
    text = "好的：\n```python\nimport cadquery as cq\nsolid = cq.Workplane().box(1, 2, 3)\n```\n"
    assert extract_code(text).startswith("import cadquery as cq")


@needs_limits
def test_sandbox_runs_code_and_scales_to_drawing(tmp_path):
    code = "import cadquery as cq\nsolid = cq.Workplane('XY').box(1.0, 0.5, 0.25)\n"
    r = asyncio.run(
        run_cad(code, tmp_path, scale_to={"width": 80.0, "depth": 40.0, "height": 20.0}, wsl=WSL)
    )
    assert r.ok, r.error
    assert r.data["dims"]["width"] == pytest.approx(80.0, rel=1e-3)
    assert r.data["scale"] == pytest.approx(80.0, rel=1e-3)
    assert {"model.stl", "model.step", "reproj.png"} <= {p.name for p in tmp_path.iterdir()}


@needs_limits
def test_sandbox_reports_runtime_errors(tmp_path):
    code = "import cadquery as cq\nsolid = cq.Workplane().box(1, 1)\n"
    r = asyncio.run(run_cad(code, tmp_path, wsl=WSL))
    assert not r.ok and "程式碼執行錯誤" in r.error


def test_voxel_iou():
    import cadquery as cq

    from app.cad.metrics import aligned_iou, mesh, voxel_iou

    a = cq.Workplane().box(120, 100, 10).val()
    b = cq.Workplane().box(107, 112, 10.5).val()
    assert aligned_iou(a, a) == pytest.approx(1.0)
    assert voxel_iou(mesh(a), mesh(b)) == pytest.approx(0.771, abs=0.05)


def _photo(path):
    """模擬斜拍：透視變形＋牆面背景（同 eval/make_synthetic_photos.py 的 tilt）。"""
    random.seed(3)
    img = Image.open(path).convert("RGB")
    w, h = img.size
    quad = tuple(random.uniform(0, 0.06) * (w if i % 2 == 0 else h) for i in range(8))
    quad = (quad[0], quad[1], quad[2], h - quad[3], w - quad[4], h - quad[5], w - quad[6], quad[7])
    img = img.transform((w, h), Image.Transform.QUAD, quad, Image.Resampling.BICUBIC)
    wall = Image.new("RGB", (int(w * 1.2), int(h * 1.2)), (205, 198, 186))
    wall.paste(img, (int(w * 0.1), int(h * 0.1)))
    return wall


def test_drawing_verification_accepts_same_and_rejects_other():
    """幾何驗證＋線條重合：同一張圖紙的斜拍照要通過，版面相同的另一張圖紙要擋下。"""
    a, b = REPO_ROOT / "kb/drawings/mfg-006.png", REPO_ROOT / "kb/drawings/mfg-005.png"
    photo = _photo(a)
    q = verify.features(photo)
    inl, h = verify.match(q, verify.kb_features(a, a.stat().st_mtime, drawing=True), strict=True)
    assert inl >= 25 and h is not None
    assert verify.ink_overlap(photo, h, a) >= 0.55
    inl_b, h_b = verify.match(
        q, verify.kb_features(b, b.stat().st_mtime, drawing=True), strict=True
    )
    assert inl_b < 25 or verify.ink_overlap(photo, h_b, b) < 0.55


def test_parts_api(client):
    items = client.get("/api/v1/parts").json()["items"]
    assert {p["id"] for p in items} >= {"mfg-001", "mfg-006"}
    p = client.get("/api/v1/parts/mfg-002").json()
    assert p["geometry"]["width"] == pytest.approx(100.0) and p["descriptions"]
    assert client.get("/api/v1/parts/mfg-002/model.stl").status_code == 200
    assert client.get("/api/v1/parts/nope-1").json()["error"]["code"] == "PART_NOT_FOUND"


@needs_limits
def test_reconstruct_stream_mock_returns_ground_truth(client):
    """LLM_MODE=mock：回傳標準模型的程式碼，所以 IoU 應接近 1，外送恆為 0。"""
    r = client.post("/api/v1/cad/reconstruct", json={"part_id": "mfg-002"})
    events = parse_sse(r.text)
    kinds = [e for e, _ in events]
    assert kinds[0] == "meta" and "token" in kinds and kinds[-2:] == ["result", "done"]
    assert "executing" in kinds
    meta, result, done = events[0][1], events[-2][1], events[-1][1]
    assert meta["layout"] == "kb" and meta["scale_to"]["width"] == pytest.approx(100.0)
    assert result["ok"] and result["iou"] > 0.95
    assert done["egress"] == {"images": 0, "chunks": 0, "bytes": 0}
    job = client.get(f"/api/v1/cad/jobs/{done['job_id']}").json()
    assert job["result"]["ok"]
    assert client.get(f"/api/v1/cad/jobs/{done['job_id']}/model.step").status_code == 200
    assert client.get(f"/api/v1/cad/jobs/{done['job_id']}/result.json").status_code == 404
    assert client.get("/api/v1/cad/jobs/cad_nope/model.step").status_code == 404


def test_part_chat_uses_drawing_prompt(client):
    r = client.post("/api/v1/chat", json={"question": "加工製程是什麼？", "part_id": "mfg-001"})
    events = parse_sse(r.text)
    sources = events[0][1]["sources"]
    assert sources and all(s["part_id"] == "mfg-001" and s["source_label"] for s in sources)
    assert events[-1][0] == "done" and events[-1][1]["prompt_version"] == "drawing_v2"


def test_confidential_drawings_never_go_to_cloud(client):
    r = client.post(
        "/api/v1/chat", json={"question": "公差？", "part_id": "mfg-001", "strategy": "api_kb"}
    )
    (event, data), *_ = parse_sse(r.text)
    assert event == "error" and data["code"] == "CLOUD_CONFIDENTIAL_FORBIDDEN"


def test_untrusted_code_refused_without_limits(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "LIMITS_AVAILABLE", False)
    code = "import cadquery as cq\nsolid = cq.Workplane().box(1, 1, 1)\n"
    r = asyncio.run(run_cad(code, tmp_path))
    assert not r.ok and "不執行模型產生的程式碼" in r.error


def test_runner_skips_limits_only_for_trusted(monkeypatch):
    from app.cad import runner

    monkeypatch.setattr(runner, "resource", None)
    runner._limit(60, trusted=True)  # 知識庫自己的標準模型：沒有 resource 也照跑
    with pytest.raises(RuntimeError, match="不執行模型產生的程式碼"):
        runner._limit(60, trusted=False)


# ------------------------------------------------------ Windows 主機在 WSL2 執行（ADR 027）
class FakeProc:
    def __init__(self, returncode: int, on_run):
        self.returncode, self.on_run = returncode, on_run

    async def communicate(self, data: bytes):
        self.on_run(json.loads(data))
        return b"", b""

    def kill(self):
        pass

    async def wait(self):
        return self.returncode


def fake_subprocess(monkeypatch, job_dir, returncode=0, write_result=True) -> dict:
    """不真的開子行程：記下指令、環境變數與工作 JSON。

    write_result 時代替 runner 寫 result.json。
    """
    calls: dict = {}

    async def create(*argv, **kw):
        calls["argv"], calls["env"] = list(argv), kw["env"]

        def on_run(job):
            calls["job"] = job
            if write_result:
                (job_dir / "result.json").write_text('{"ok": true}', encoding="utf-8")

        return FakeProc(returncode, on_run)

    monkeypatch.setattr(sandbox.asyncio, "create_subprocess_exec", create)
    return calls


def test_wsl_path_maps_drive_letters():
    assert (
        wsl_path(PureWindowsPath(r"C:\Users\me\art-rag\data\cad_jobs\cad_1"))
        == "/mnt/c/Users/me/art-rag/data/cad_jobs/cad_1"
    )
    assert wsl_path(PureWindowsPath("D:/kb")) == "/mnt/d/kb"
    with pytest.raises(ValueError):
        wsl_path(PureWindowsPath(r"\\server\share\x"))


def test_untrusted_code_runs_in_wsl_without_limits(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "LIMITS_AVAILABLE", False)
    monkeypatch.setattr(sandbox, "wsl_path", lambda p: "/mnt/x/" + PurePath(p).name)
    monkeypatch.setenv("WSLENV", "API_KEY")
    calls = fake_subprocess(monkeypatch, tmp_path)
    code = "import cadquery as cq\nsolid = cq.Workplane().box(1, 1, 1)\n"
    wsl = WslTarget("/home/me/cad/bin/python", "Ubuntu-24.04")
    assert asyncio.run(run_cad(code, tmp_path, wsl=wsl)).ok
    argv = calls["argv"]
    assert argv[:3] == ["wsl.exe", "-d", "Ubuntu-24.04"]
    # 時間到一定結束 → 沒有網路 → 不帶環境變數 → WSL 裡的 Python 跑同一支 runner
    order = [argv.index(x) for x in ("/usr/bin/timeout", "/usr/bin/unshare", "/usr/bin/env")]
    assert order == sorted(order) and "-rn" in argv and "-i" in argv
    assert argv[-3:] == ["/home/me/cad/bin/python", "-m", "app.cad.runner"]
    assert "WSLENV" not in calls["env"]  # Windows 的環境變數不會被轉進 WSL
    assert calls["job"]["code_path"] == "/mnt/x/code.py"
    assert calls["job"]["out_dir"] == f"/mnt/x/{tmp_path.name}"


def test_unsafe_code_never_reaches_wsl(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "LIMITS_AVAILABLE", False)
    calls = fake_subprocess(monkeypatch, tmp_path)
    r = asyncio.run(run_cad("import os\nsolid = None\n", tmp_path, wsl=WslTarget("/x/python")))
    assert not r.ok and "安全檢查未通過" in r.error and not calls


def test_trusted_code_never_goes_to_wsl(tmp_path, monkeypatch):
    calls = fake_subprocess(monkeypatch, tmp_path)
    r = asyncio.run(run_cad("solid = None\n", tmp_path, trusted=True, wsl=WslTarget("/x/python")))
    assert r.ok and "wsl.exe" not in calls["argv"]
    assert calls["argv"][-2:] == ["-m", "app.cad.runner"]


@pytest.mark.parametrize("returncode", [-9, 9, 124, 137])
def test_killed_runner_reported_as_timeout(tmp_path, monkeypatch, returncode):
    """CPU 硬限制的 SIGKILL（直接執行 -9、經過 wsl.exe 是 9）或 timeout 指令（124、137）。"""
    fake_subprocess(monkeypatch, tmp_path, returncode=returncode, write_result=False)
    r = asyncio.run(run_cad("solid = None\n", tmp_path, trusted=True, timeout_s=7))
    assert not r.ok and r.error == "執行逾時（>7 秒），已中止"


def test_cpu_limit_message_wins_over_import_errors(monkeypatch):
    """逾時發生在 import cadquery 途中時，之後的例外是看不懂的 ImportError：一律改說逾時。"""
    from app.cad import runner

    monkeypatch.setattr(runner, "CPU_LIMIT_HIT", [])
    with pytest.raises(RuntimeError, match="CPU 時間限制（5 秒）"):
        runner._cpu_exceeded(5)(signal.SIGTERM, None)
    assert runner.CPU_LIMIT_HIT == ["執行超過 CPU 時間限制（5 秒），已中止"]


@pytest.mark.skipif(not hasattr(signal, "SIGXCPU"), reason="Windows 沒有 SIGXCPU")
def test_runner_limits_never_leave_core_dumps(monkeypatch):
    """WSL 會把 core dump 交給程式處理（不看 RLIMIT_CORE），每次約 370 MB：行程要設成不可傾印。"""
    from app.cad import runner

    limits, prctl, handlers = [], [], {}

    class FakeResource:
        RLIMIT_CPU, RLIMIT_FSIZE, RLIMIT_CORE = "cpu", "fsize", "core"

        @staticmethod
        def setrlimit(kind, value):
            limits.append((kind, value))

    class FakeLibc:
        def prctl(self, *args):
            prctl.append(args)

    monkeypatch.setattr(runner, "resource", FakeResource)
    monkeypatch.setattr(runner.signal, "signal", lambda s, h: handlers.setdefault(s, h))
    monkeypatch.setattr(runner.ctypes, "CDLL", lambda name: FakeLibc())
    runner._limit(30, trusted=False)
    assert ("cpu", (30, 35)) in limits and ("core", (0, 0)) in limits
    assert signal.SIGXCPU in handlers
    if sys.platform == "linux":
        assert prctl == [(4, 0, 0, 0, 0)]
