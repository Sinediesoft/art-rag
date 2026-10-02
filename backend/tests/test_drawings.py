"""工廠機械加工圖：知識庫、圖紙驗證、CadQuery 沙箱、3D 重建串流、圖紙問答（一律 mock 模型）。"""

import asyncio
import json
import random

import pytest
from PIL import Image

from app.cad import sandbox
from app.cad.sandbox import LIMITS_AVAILABLE, UnsafeCode, check_code, extract_code, run_cad
from app.core.config import REPO_ROOT
from app.rag import verify
from app.rag.chunking import build_part_chunks
from app.rag.kb import validate_parts

# Windows 沒有 resource 模組，限制不了子行程的 CPU 與檔案大小：模型產生的程式碼一律不執行
needs_limits = pytest.mark.skipif(
    not LIMITS_AVAILABLE, reason="這個平台沒有 resource 模組，模型產生的程式碼不執行"
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
        run_cad(code, tmp_path, scale_to={"width": 80.0, "depth": 40.0, "height": 20.0})
    )
    assert r.ok, r.error
    assert r.data["dims"]["width"] == pytest.approx(80.0, rel=1e-3)
    assert r.data["scale"] == pytest.approx(80.0, rel=1e-3)
    assert {"model.stl", "model.step", "reproj.png"} <= {p.name for p in tmp_path.iterdir()}


@needs_limits
def test_sandbox_reports_runtime_errors(tmp_path):
    r = asyncio.run(run_cad("import cadquery as cq\nsolid = cq.Workplane().box(1, 1)\n", tmp_path))
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
    assert events[-1][0] == "done" and events[-1][1]["prompt_version"] == "drawing_v1"


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
