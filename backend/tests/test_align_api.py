"""影像對位與比對的 API（docs/adr/012）：GET /images/{image_id}/align、/align.png。"""

import io

import pytest
from PIL import Image

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


def test_painting_alignment_is_location_only(client, painting_crop):
    r = client.get(f"/api/v1/images/{painting_crop}/align", params={"target": "artwork:npm-000001"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["diff"] is None
    assert 0.3 < body["location"]["coverage"] < 0.8  # 模擬照裁掉 30%：拍到約一半
    assert body["reference_url"] == "/api/v1/artworks/npm-000001/image"
    assert client.get(body["overlay_url"]).status_code == 200
    again = client.get(
        f"/api/v1/images/{painting_crop}/align", params={"target": "artwork:npm-000001"}
    )
    assert again.json()["location"] == body["location"]


def test_wrong_target_is_align_failed(client, painting_crop):
    r = client.get(f"/api/v1/images/{painting_crop}/align", params={"target": "artwork:aic-27992"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "ALIGN_FAILED"
    r = client.get(
        f"/api/v1/images/{painting_crop}/align.png", params={"target": "artwork:aic-27992"}
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "ALIGN_FAILED"


@pytest.mark.parametrize("target", ["mfg-002", "image:img_x", "part:", "artwork:a b"])
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
