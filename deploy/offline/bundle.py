"""離線包（docs/adr/046）的模型與 Ollama 收集，由 deploy/offline/pack.ps1 呼叫。

只用標準函式庫，而且只挑需要的檔案（同一台常放著其他模型、舊版本）：
- Ollama 模型：指定模型的 manifest 和它引用的 blob
- Hugging Face：shared/models.yaml 釘選版本的 snapshot，加上 .no_exist 記號
  （離線時 transformers 靠它知道這個版本沒有 model.safetensors，直接讀 pytorch_model.bin）
- Ollama 程式：拿掉 AMD 顯卡用的 ROCm 函式庫（約 1 GB；離線包只支援 NVIDIA）與安裝程式、系統匣程式
目的地已經有、大小相同的檔案略過：中斷後重跑會接著做。
"""

import argparse
import json
import shutil
import sys
from pathlib import Path

OLLAMA_MANIFESTS = Path("manifests/registry.ollama.ai/library")
# 離線包直接跑 ollama serve，用不到系統匣程式與解除安裝程式
OLLAMA_PROGRAM_SKIP = ("ollama app.exe", "app.ico")


def ollama_model_files(models_dir: Path, tag: str) -> list[Path]:
    """相對 models_dir 的路徑：manifest、config 與各層 blob。"""
    name, _, version = tag.partition(":")
    manifest = OLLAMA_MANIFESTS / name / (version or "latest")
    path = models_dir / manifest
    if not path.is_file():
        raise FileNotFoundError(f"這台的 Ollama 沒有 {tag}：先 ollama pull {tag}")
    data = json.loads(path.read_text(encoding="utf-8"))
    digests = [data["config"]["digest"], *(layer["digest"] for layer in data["layers"])]
    return [manifest, *(Path("blobs") / d.replace(":", "-") for d in digests)]


def pinned_embeddings(models_yaml: str) -> list[tuple[str, str]]:
    """shared/models.yaml 的 embeddings 底下每個模型的 (name, revision)。

    打包時只有標準函式庫（沒有 PyYAML），這一段的格式簡單，逐行讀就好。
    """
    out: list[tuple[str, str]] = []
    in_section, name = False, None
    for line in models_yaml.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.startswith(" "):
            in_section = line.startswith("embeddings:")
            continue
        if not in_section:
            continue
        key, _, value = line.strip().partition(":")
        value = value.split("#")[0].strip()
        if key == "name":
            name = value
        elif key == "revision" and name:
            out.append((name, value))
            name = None
    return out


def hf_snapshot_files(hub_dir: Path, repo_id: str, revision: str) -> list[Path]:
    """相對 hub_dir 的路徑：釘選版本的 snapshot 與 .no_exist 記號。"""
    base = Path("models--" + repo_id.replace("/", "--"))
    snapshot = hub_dir / base / "snapshots" / revision
    if not snapshot.is_dir():
        raise FileNotFoundError(
            f"Hugging Face 快取沒有 {repo_id}@{revision[:8]}：先在這台建一次索引（會下載模型）"
        )
    files = [p.relative_to(hub_dir) for p in snapshot.rglob("*") if p.is_file()]
    no_exist = hub_dir / base / ".no_exist" / revision
    if no_exist.is_dir():
        files += [p.relative_to(hub_dir) for p in no_exist.rglob("*") if p.is_file()]
    return files


def ollama_program_files(program_dir: Path) -> list[Path]:
    """相對 program_dir 的路徑：ollama.exe 與 lib，不含 ROCm、系統匣與解除安裝程式。"""
    if not (program_dir / "ollama.exe").is_file():
        raise FileNotFoundError(f"{program_dir} 裡沒有 ollama.exe")
    files = []
    for p in program_dir.rglob("*"):
        rel = p.relative_to(program_dir)
        if not p.is_file() or rel.name.lower() in OLLAMA_PROGRAM_SKIP:
            continue
        if rel.name.lower().startswith("unins") or any(
            part.lower().startswith("rocm") for part in rel.parts
        ):
            continue
        files.append(rel)
    return files


def copy_files(src_root: Path, rel_paths: list[Path], dst_root: Path) -> int:
    """回傳這次實際複製的位元組數；目的地已有、大小相同的略過。"""
    copied = 0
    for rel in rel_paths:
        src, dst = src_root / rel, dst_root / rel
        size = src.stat().st_size
        if dst.is_file() and dst.stat().st_size == size:
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        # copyfile 會跟著符號連結複製真正的內容（開了開發人員模式的 Windows，HF 快取是連結）
        shutil.copyfile(src, dst)
        copied += size
    return copied


def total_size(root: Path, rel_paths: list[Path]) -> int:
    return sum((root / rel).stat().st_size for rel in rel_paths)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--out", required=True, help="離線包的 offline 資料夾")
    ap.add_argument("--models-yaml", required=True, help="要打包的那份 shared/models.yaml")
    ap.add_argument("--ollama-models", required=True, help="這台 Ollama 的模型資料夾")
    ap.add_argument("--ollama-program", required=True, help="這台 Ollama 的程式資料夾")
    ap.add_argument("--hf-hub", required=True, help="這台 Hugging Face 的 hub 快取資料夾")
    ap.add_argument("--model", action="append", required=True, help="Ollama 模型（可重複）")
    args = ap.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")

    out = Path(args.out)
    ollama_models, ollama_program, hf_hub = (
        Path(args.ollama_models),
        Path(args.ollama_program),
        Path(args.hf_hub),
    )
    try:
        # 先把要複製的清單全部列好：缺東西就在複製任何檔案之前停下來
        plan = [
            ("Ollama 程式", ollama_program, ollama_program_files(ollama_program), out / "ollama")
        ]
        model_files = sorted(
            {f for tag in args.model for f in ollama_model_files(ollama_models, tag)}
        )
        plan.append(
            (
                f"Ollama 模型（{'、'.join(args.model)}）",
                ollama_models,
                model_files,
                out / "ollama-models",
            )
        )
        for repo_id, revision in pinned_embeddings(
            Path(args.models_yaml).read_text(encoding="utf-8")
        ):
            files = hf_snapshot_files(hf_hub, repo_id, revision)
            plan.append((f"{repo_id}@{revision[:8]}", hf_hub, files, out / "hf" / "hub"))
    except FileNotFoundError as e:
        print(f"✗ {e}")
        return 1

    for label, src, files, dst in plan:
        size = total_size(src, files)
        print(f"… {label}：{len(files)} 個檔案，{size / 1e9:.2f} GB")
        copied = copy_files(src, files, dst)
        if copied < size:
            print(f"  （{(size - copied) / 1e9:.2f} GB 已經在離線包裡，略過）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
