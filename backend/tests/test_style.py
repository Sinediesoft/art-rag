"""畫作卡推測（docs/adr/018）：把關、風格大類加總、看不太出來的標示與 API。

conftest 用 mock embedding：文字與照片向量是雜湊產生的固定向量，所以分類結果沒有意義，
這裡只測流程與規則；真模型的準確度由 make eval-style 量。
"""

import json
from pathlib import Path

import numpy as np

from app.analysis import style
from app.core.config import REPO_ROOT, StyleGuessSpec, StyleHeadSpec, get_models_config

PAINTING = Path(__file__).resolve().parents[2] / "eval" / "photos" / "unknown" / "unknown-01.jpg"


def _prompt_vec(text: str) -> np.ndarray:
    return style._label_vec([text], ["{}"])


def test_painting_gate():
    """照片向量貼近「一幅畫」→ 推測；貼近「一張螢幕截圖」→ 不推測、不回任何欄位。"""
    spec = get_models_config().style_guess
    yes = style.guess(_prompt_vec(spec.painting_prompts[0]))
    assert yes["is_painting"] and yes["painting_score"] >= spec.painting_min
    assert [f["key"] for f in yes["fields"]] == ["style", "genre", "media"]
    no = style.guess(_prompt_vec(spec.other_prompts[1]))
    assert not no["is_painting"] and no["fields"] == []
    assert "不像畫作" in no["summary"]


def test_style_group_follows_top_label_not_biggest_sum():
    """細分第一名是 d（小類 Small）；Big 有 3 個流派、加總比較高，但不能靠流派多贏 →
    風格回報 Small，機率是 Small 的加總；細分候選照機率排。"""
    spec = StyleGuessSpec(temperature=1, min_confidence=0.2)
    vec = np.array([0.4, 0.4, 0.4, 0.6])
    groups = ["Big", "Big", "Big", "Small"]
    f = style._field(
        "style",
        ["a", "b", "c", "d"],
        np.eye(4),
        groups,
        {"Big": "某世紀", "Small": "另一世紀"},
        vec,
        spec,
    )
    top = f["candidates"][0]
    assert top["name"] == "d" and top["group"] == "Small"
    assert f["name"] == "Small" and f["period"] == "另一世紀"
    assert abs(f["prob"] - top["prob"]) < 1e-3 and f["prob"] < 0.5  # Big 加總其實比較高


def test_style_group_prob_sums_its_members():
    spec = StyleGuessSpec(temperature=1, min_confidence=0.4)
    vec = np.array([0.5, 0.45, 0.4])
    f = style._field(
        "style", ["a", "b", "c"], np.eye(3), ["G1", "G1", "G2"], {"G1": "x", "G2": "y"}, vec, spec
    )
    probs = {c["name"]: c["prob"] for c in f["candidates"]}
    assert f["name"] == "G1" and abs(f["prob"] - (probs["a"] + probs["b"])) < 1e-3


def test_low_probability_is_marked_uncertain():
    spec = StyleGuessSpec(temperature=1, min_confidence=0.4)
    f = style._field("media", ["油畫", "水彩畫", "素描"], np.eye(3), None, None, np.zeros(3), spec)
    assert f["prob"] < 0.4 and f["uncertain"] and f["period"] is None
    assert "group" not in f["candidates"][0]


def test_summary_skips_uncertain_fields():
    fields = {
        "style": {
            "name": "印象派一脈",
            "period": "19 世紀後期",
            "uncertain": False,
            "candidates": [{"name": "新印象派", "group": "印象派一脈"}],
        },
        "genre": {"name": "風景畫", "uncertain": True},
        "media": {"name": "油畫", "uncertain": False},
    }
    text = style.summarize(fields)
    assert text == "推測是19 世紀後期的印象派一脈（細分最接近新印象派），媒材像油畫。"
    fields["style"]["uncertain"] = True
    fields["media"]["uncertain"] = True
    assert style.summarize(fields) == "風格與題材看不太出來，媒材看不太出來。"


def _fake_head(labels: list[str], w: np.ndarray, b: list[float], thr: list[float]) -> dict:
    d = w.shape[1]
    return {
        "labels": labels,
        "W": w,
        "b": np.array(b),
        "thresholds": np.array(thr),
        "mu": np.zeros(d),
        "sd": np.ones(d),
    }


def test_head_field_lists_every_label_above_its_threshold():
    """多標籤：過各自門檻的都列出（機率高的當 name、其餘放 also）；機率各自獨立，加總不必是 1。"""
    h = _fake_head(["油畫", "蛋彩畫", "水彩畫"], np.eye(3) * 10, [0, 0, 0], [0.9, 0.7, 0.5])
    f = style._head_field("media", h, np.array([0.5, 0.3, -1.0]))
    assert f["source"] == "head" and not f["uncertain"]
    assert f["name"] == "油畫" and f["also"] == ["蛋彩畫"]  # 水彩畫 sigmoid(-10)≈0 沒過門檻
    assert [c["name"] for c in f["candidates"]] == ["油畫", "蛋彩畫", "水彩畫"]
    assert sum(c["prob"] for c in f["candidates"]) > 1
    assert style.summarize({"media": f}) == "風格與題材看不太出來，媒材像油畫、蛋彩畫。"


def test_head_field_uncertain_when_nothing_passes():
    """全部沒過門檻 → 看不太出來，name 仍取機率最高的（畫面上顯示「看不太出來」）。"""
    h = _fake_head(["油畫", "蛋彩畫"], np.eye(2), [0, 0], [0.99, 0.99])
    f = style._head_field("media", h, np.array([0.2, 0.1]))
    assert f["uncertain"] and f["name"] == "油畫" and f["also"] == []


def _save_head(path: Path, clip: str) -> None:
    labels = list(get_models_config().style_guess.media.labels)
    n = len(labels)
    np.savez(
        path,
        meta=np.array(json.dumps({"version": "style-head-v1", "clip": clip})),
        media_W=np.zeros((n, 512), dtype=np.float32),
        media_b=np.linspace(2, -2, n).astype(np.float32),  # 第一個標籤機率最高（sigmoid(2)≈0.88）
        media_thresholds=np.full(n, 0.5, dtype=np.float32),
        media_mu=np.zeros(512, dtype=np.float32),
        media_sd=np.ones(512, dtype=np.float32),
        media_labels=np.array(labels),
    )


def test_guess_uses_head_only_for_listed_fields(tmp_path):
    spec = get_models_config().style_guess
    clip = get_models_config().embeddings["image"]
    path = tmp_path / "head.npz"
    _save_head(path, f"{clip.name}@{clip.revision}")
    spec = spec.model_copy(update={"head": StyleHeadSpec(path=str(path), fields=["media"])})
    r = style.guess(_prompt_vec(spec.painting_prompts[0]), spec)
    fields = {f["key"]: f for f in r["fields"]}
    assert r["method"] == f"{spec.method}+style-head-v1(media)"
    assert fields["media"]["source"] == "head" and fields["genre"]["source"] == "zero_shot"
    assert fields["media"]["name"] == list(spec.media.labels)[0]
    assert any("分類頭" in n for n in r["notes"])


def test_guess_falls_back_to_zero_shot_when_head_unusable(tmp_path):
    """分類頭檔不在、或訓練時的 Chinese-CLIP 和現在不同（向量空間不一樣）→ 照舊零樣本，不報錯。"""
    base = get_models_config().style_guess
    vec = _prompt_vec(base.painting_prompts[0])
    wrong = tmp_path / "other-clip.npz"
    _save_head(wrong, "some/other-clip@deadbeef")
    for p in (wrong, tmp_path / "missing.npz"):
        spec = base.model_copy(update={"head": StyleHeadSpec(path=str(p), fields=["media"])})
        r = style.guess(vec, spec)
        assert r["method"] == base.method
        assert {f["source"] for f in r["fields"]} == {"zero_shot"}


def test_every_style_label_belongs_to_one_group():
    """細分流派名稱不能在兩個大類重複：大類加總靠名稱對回所屬大類。"""
    groups = get_models_config().style_guess.style.groups
    names = [n for g in groups for n in g.labels]
    assert len(names) == len(set(names))
    assert all(g.period for g in groups)


def test_photo_style_api(client):
    r = client.post(
        "/api/v1/images", files={"file": (PAINTING.name, PAINTING.read_bytes(), "image/jpeg")}
    )
    image_id = r.json()["image_id"]
    body = client.get(f"/api/v1/images/{image_id}/style").json()
    # models.yaml 開了媒材的線性分類頭（shared/style_head_v1.npz）
    assert body["method"] == "clip-zeroshot-v1+style-head-v1(media)"
    assert body["summary"] and body["notes"]
    assert 0 <= body["painting_score"] <= 1
    assert body["is_painting"] == bool(body["fields"])
    for f in body["fields"]:
        assert 1 <= len(f["candidates"]) <= 3
        assert (f["period"] is not None) == (f["key"] == "style")
        assert f["source"] == ("head" if f["key"] == "media" else "zero_shot")
    again = client.get(f"/api/v1/images/{image_id}/style").json()
    assert again["fields"] == body["fields"]  # 快取或重算，結果都一樣


def test_photo_style_404(client):
    r = client.get("/api/v1/images/img_doesnotexist0/style")
    assert r.status_code == 404 and r.json()["error"]["code"] == "IMAGE_NOT_FOUND"


def test_eval_runs_skips_style_runs(client):
    """GET /eval/runs 讀真的 eval/runs/：*-style.json 格式不同，沒排除會讓回應驗證失敗（500）。"""
    assert any((REPO_ROOT / "eval" / "runs").glob("*-style.json"))
    assert client.get("/api/v1/eval/runs").status_code == 200
