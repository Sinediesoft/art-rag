"""影像對位與比對的 API（docs/adr/012）：GET /images/{image_id}/align、/align.png。"""

import io

import pytest
from PIL import Image, ImageDraw

from app.core.config import REPO_ROOT


def _upload(client, path) -> str:
    r = client.post("/api/v1/images", files={"file": (path.name, path.read_bytes(), "image/jpeg")})
    assert r.status_code == 200, r.text
    return r.json()["image_id"]


@pytest.fixture(scope="module")
def drawing_photo(client):
    return _upload(client, REPO_ROOT / "eval/drawing_photos/known/mfg-002__tilt.jpg")


@pytest.fixture(scope="module")
def painting_crop(client):
    return _upload(client, REPO_ROOT / "eval/photos/known/npm-000001__crop.jpg")


def test_drawing_alignment_has_diff_and_overlay(client, drawing_photo):
    r = client.get(f"/api/v1/images/{drawing_photo}/align", params={"target": "part:mfg-002"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["target"] == {"kind": "part", "id": "mfg-002"}
    assert body["inliers"] >= 25
    assert len(body["location"]["polygon"]) == 4 and body["location"]["coverage"] > 0.9
    assert body["diff"]["method"] == "ink" and body["diff"]["status"] in ("same", "changed")
    assert body["reference_url"].startswith("/api/v1/parts/mfg-002/drawing")
    png = client.get(body["overlay_url"])
    assert png.status_code == 200 and png.headers["content-type"] == "image/png"
    assert Image.open(io.BytesIO(png.content)).size == (800, 970)


def test_painting_alignment_has_location_and_tone_diff(client, painting_crop):
    r = client.get(f"/api/v1/images/{painting_crop}/align", params={"target": "artwork:npm-000001"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["diff"] == {"method": "tone", "status": "same", "regions": [], "changed_ratio": 0.0}
    assert 0.3 < body["location"]["coverage"] < 0.8  # 模擬照裁掉 30%：拍到約一半
    assert body["reference_url"] == "/api/v1/artworks/npm-000001/image"
    assert client.get(body["overlay_url"]).status_code == 200
    again = client.get(
        f"/api/v1/images/{painting_crop}/align", params={"target": "artwork:npm-000001"}
    )
    assert again.json()["location"] == body["location"]


def test_strokes_drawn_on_a_painting_photo_are_boxed(client):
    """在畫作照片上加幾筆再上傳：和知識庫原圖比，只框出那幾筆。"""
    photo = Image.open(REPO_ROOT / "eval/align_photos/art/npm-000001__crop50-1.jpg").convert("RGB")
    d = ImageDraw.Draw(photo)
    for pts in (
        [(70, 625), (100, 610), (95, 650), (120, 640)],
        [(130, 615), (165, 630), (150, 655), (180, 650)],
        [(195, 605), (225, 625), (210, 660)],
    ):
        d.line(pts, fill=(15, 15, 15), width=3, joint="curve")
    buf = io.BytesIO()
    photo.save(buf, "JPEG", quality=90)
    r = client.post("/api/v1/images", files={"file": ("strokes.jpg", buf.getvalue(), "image/jpeg")})
    image_id = r.json()["image_id"]
    body = client.get(
        f"/api/v1/images/{image_id}/align", params={"target": "artwork:npm-000001"}
    ).json()
    assert body["diff"]["status"] == "changed", body
    [g] = body["diff"]["regions"]
    assert g["kind"] in ("shape", "both")
    x0, y0, x1, y1 = g["bbox"]  # 筆畫在原圖約 x 0.16–0.30、y 0.60–0.64
    assert 0.1 < x0 < 0.2 and 0.25 < x1 < 0.35 and 0.55 < y0 < 0.62 and 0.62 < y1 < 0.7


def test_wrong_target_is_align_failed(client, painting_crop):
    r = client.get(f"/api/v1/images/{painting_crop}/align", params={"target": "artwork:aic-27992"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "ALIGN_FAILED"
    r = client.get(
        f"/api/v1/images/{painting_crop}/align.png", params={"target": "artwork:aic-27992"}
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "ALIGN_FAILED"


@pytest.mark.parametrize("target", ["mfg-002", "photo:img_x", "part:", "artwork:a b"])
def test_bad_target_format_is_validation_error(client, drawing_photo, target):
    r = client.get(f"/api/v1/images/{drawing_photo}/align", params={"target": target})
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_eval_runs_skips_align_runs(client):
    """GET /eval/runs 讀真的 eval/runs/：*-align.json 格式不同，沒排除會讓回應驗證失敗（500）。"""
    assert any((REPO_ROOT / "eval" / "runs").glob("*-align.json"))
    assert client.get("/api/v1/eval/runs").status_code == 200


def test_unknown_ids_are_404(client, drawing_photo):
    r = client.get(f"/api/v1/images/{drawing_photo}/align", params={"target": "part:nope-1"})
    assert r.status_code == 404 and r.json()["error"]["code"] == "PART_NOT_FOUND"
    r = client.get(f"/api/v1/images/{drawing_photo}/align", params={"target": "artwork:nope-1"})
    assert r.status_code == 404 and r.json()["error"]["code"] == "ARTWORK_NOT_FOUND"
    r = client.get("/api/v1/images/img_doesnotexist0/align", params={"target": "part:mfg-002"})
    assert r.status_code == 404 and r.json()["error"]["code"] == "IMAGE_NOT_FOUND"


# ---------------------------------------------------------------- 兩張照片互比（畫作）
@pytest.fixture(scope="module")
def painting_dim(client):
    return _upload(client, REPO_ROOT / "eval/photos/known/npm-000001__dim.jpg")


def test_photo_pair_compares_shape_and_color(client, painting_crop, painting_dim):
    """同一幅畫的兩張照片：B（裁切）對齊到 A（整幅、偏暗）比形狀與顏色。"""
    r = client.get(
        f"/api/v1/images/{painting_crop}/align", params={"target": f"image:{painting_dim}"}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["target"] == {"kind": "image", "id": painting_dim}
    assert body["diff"]["method"] == "tone"
    assert body["diff"]["status"] in ("same", "changed")
    assert all(g["kind"] in ("shape", "color", "both") for g in body["diff"]["regions"])
    assert body["reference_url"] == f"/api/v1/images/{painting_dim}"
    png = client.get(body["overlay_url"])
    assert png.status_code == 200 and png.headers["content-type"] == "image/png"


def test_photo_against_itself_has_no_differences(client, painting_dim):
    r = client.get(
        f"/api/v1/images/{painting_dim}/align", params={"target": f"image:{painting_dim}"}
    )
    body = r.json()
    assert body["diff"]["status"] == "same" and body["location"]["coverage"] > 0.95


def test_photo_pair_of_different_paintings_is_align_failed(client, painting_dim, drawing_photo):
    r = client.get(
        f"/api/v1/images/{drawing_photo}/align", params={"target": f"image:{painting_dim}"}
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "ALIGN_FAILED"


def test_photo_pair_unknown_photo_is_404(client, painting_dim):
    r = client.get(
        f"/api/v1/images/{painting_dim}/align", params={"target": "image:img_doesnotexist0"}
    )
    assert r.status_code == 404 and r.json()["error"]["code"] == "IMAGE_NOT_FOUND"
