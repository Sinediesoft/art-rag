"""離線包的模型收集（deploy/offline/bundle.py）：只挑需要的檔案、釘選版本、略過已經複製過的。"""

import importlib.util
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("bundle", REPO / "deploy" / "offline" / "bundle.py")
bundle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bundle)


def write(path: Path, data: bytes | str = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode() if isinstance(data, str) else data)
    return path


def fake_ollama(models: Path, tag: str, digests: list[str]) -> None:
    name, _, version = tag.partition(":")
    manifest = {"config": {"digest": digests[0]}, "layers": [{"digest": d} for d in digests[1:]]}
    write(models / bundle.OLLAMA_MANIFESTS / name / version, json.dumps(manifest))
    for d in digests:
        write(models / "blobs" / d.replace(":", "-"), d)


def test_ollama_model_files_only_lists_that_model(tmp_path):
    fake_ollama(tmp_path, "qwen3-vl:8b-instruct", ["sha256:a", "sha256:b", "sha256:c"])
    fake_ollama(tmp_path, "other:latest", ["sha256:z"])
    files = bundle.ollama_model_files(tmp_path, "qwen3-vl:8b-instruct")
    assert files[0] == bundle.OLLAMA_MANIFESTS / "qwen3-vl" / "8b-instruct"
    assert sorted(f.name for f in files[1:]) == ["sha256-a", "sha256-b", "sha256-c"]
    with pytest.raises(FileNotFoundError, match="ollama pull"):
        bundle.ollama_model_files(tmp_path, "qwen3-vl:32b")


def test_pinned_embeddings_reads_the_real_models_yaml():
    text = (REPO / "shared" / "models.yaml").read_text(encoding="utf-8")
    pinned = dict(bundle.pinned_embeddings(text))
    assert set(pinned) == {"OFA-Sys/chinese-clip-vit-base-patch16", "BAAI/bge-m3"}
    assert all(len(rev) == 40 for rev in pinned.values())


def test_hf_snapshot_takes_pinned_revision_and_no_exist_markers(tmp_path):
    base = tmp_path / "models--BAAI--bge-m3"
    write(base / "snapshots" / "rev1" / "pytorch_model.bin")
    write(base / "snapshots" / "rev1" / "config.json")
    write(base / "snapshots" / "rev0" / "old.bin")  # 舊版本不帶
    write(base / ".no_exist" / "rev1" / "model.safetensors", b"")
    files = {f.as_posix() for f in bundle.hf_snapshot_files(tmp_path, "BAAI/bge-m3", "rev1")}
    assert files == {
        "models--BAAI--bge-m3/snapshots/rev1/pytorch_model.bin",
        "models--BAAI--bge-m3/snapshots/rev1/config.json",
        "models--BAAI--bge-m3/.no_exist/rev1/model.safetensors",
    }
    with pytest.raises(FileNotFoundError, match="建一次索引"):
        bundle.hf_snapshot_files(tmp_path, "BAAI/bge-m3", "rev9")


def test_ollama_program_skips_rocm_tray_and_uninstaller(tmp_path):
    for rel in [
        "ollama.exe",
        "ollama app.exe",
        "unins000.exe",
        "lib/ollama/cuda_v12/ggml-cuda.dll",
        "lib/ollama/rocm_v7_1/ggml-hip.dll",
    ]:
        write(tmp_path / rel)
    files = {f.as_posix() for f in bundle.ollama_program_files(tmp_path)}
    assert files == {"ollama.exe", "lib/ollama/cuda_v12/ggml-cuda.dll"}


def test_main_copies_everything_and_rerun_skips(tmp_path, capsys):
    src, out = tmp_path / "src", tmp_path / "out"
    fake_ollama(src / "models", "qwen3-vl:4b-instruct", ["sha256:a", "sha256:b"])
    write(src / "program" / "ollama.exe")
    yaml = write(
        src / "models.yaml",
        "embeddings:\n  text:\n    name: BAAI/bge-m3\n    revision: rev1   # 釘選\n"
        "chunking:\n  name: x\n",
    )
    write(src / "hub" / "models--BAAI--bge-m3" / "snapshots" / "rev1" / "w.bin", "weights")
    args = [
        "--out", str(out), "--models-yaml", str(yaml),
        "--ollama-models", str(src / "models"), "--ollama-program", str(src / "program"),
        "--hf-hub", str(src / "hub"), "--model", "qwen3-vl:4b-instruct",
    ]  # fmt: skip
    assert bundle.main(args) == 0
    assert (out / "ollama" / "ollama.exe").is_file()
    assert (out / "ollama-models" / "blobs" / "sha256-b").read_text() == "sha256:b"
    assert (out / "hf" / "hub" / "models--BAAI--bge-m3" / "snapshots" / "rev1" / "w.bin").is_file()
    capsys.readouterr()
    assert bundle.main(args) == 0
    assert "已經在離線包裡" in capsys.readouterr().out


def test_main_stops_before_copying_when_something_is_missing(tmp_path, capsys):
    out = tmp_path / "out"
    write(tmp_path / "program" / "ollama.exe")
    yaml = write(tmp_path / "models.yaml", "embeddings:\n  text:\n    name: a/b\n    revision: r\n")
    args = [
        "--out", str(out), "--models-yaml", str(yaml),
        "--ollama-models", str(tmp_path / "models"), "--ollama-program", str(tmp_path / "program"),
        "--hf-hub", str(tmp_path / "hub"), "--model", "qwen3-vl:8b-instruct",
    ]  # fmt: skip
    assert bundle.main(args) == 1
    assert "ollama pull" in capsys.readouterr().out
    assert not out.exists()
