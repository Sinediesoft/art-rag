"""下載 Ortho2CAD 並轉成 llama.cpp 可載入的 GGUF（make ortho2cad-setup）。

Ortho2CAD（arXiv 2607.08891）是以 Qwen3-VL-8B-Instruct 微調、把三視圖轉成 CadQuery 程式碼的 VLM。
論文作者沒有公開權重，這裡用 Hugging Face 上的第三方版本（版本以 commit 固定）：

- 語言模型：nitinrathi/Ortho2CAD-Qwen3VL-8B-Q4_K_M-GGUF（Q4_K_M，5.0 GB，只含文字部分）
- 影像編碼器：nishl19/Ortho2CAD-Qwen3VL-8B 的 model.visual.* 權重（約 1.2 GB）。
  訓練時 tune_mm_mlp=True（投影層有微調），不能拿原版 Qwen3-VL 的 mmproj 代替；
  所以只用 HTTP Range 下載這一段，再以 llama.cpp 的 convert_hf_to_gguf.py 轉成 mmproj。

用法：
    python pipelines/setup_ortho2cad.py            # 下載＋轉檔（可中斷後重跑，會續傳）
    python pipelines/setup_ortho2cad.py --only text|vision|mmproj
"""

import argparse
import json
import os
import shutil
import struct
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "models" / "ortho2cad"

GGUF_REPO = "nitinrathi/Ortho2CAD-Qwen3VL-8B-Q4_K_M-GGUF"
GGUF_REV = "2eb82f2819ea328f8877587314c1ceaaae861b2a"
GGUF_FILE = "ortho2cad-qwen3vl-8b-q4_k_m.gguf"

HF_REPO = "nishl19/Ortho2CAD-Qwen3VL-8B"
HF_REV = "3c1035c4557a7969726bba2eb5d103abd6c078b4"
VISION_SHARD = "model-00001-of-00004.safetensors"
VISION_PREFIX = "model.visual."
MMPROJ_FILE = "mmproj-ortho2cad-f16.gguf"

# llama.cpp 轉檔腳本：與 brew 安裝的 llama-server 同版（0.5.0，build 11146）
LLAMA_CPP_TAG = os.environ.get("LLAMA_CPP_TAG", "b11146")


def hf_url(repo: str, rev: str, name: str) -> str:
    return f"https://huggingface.co/{repo}/resolve/{rev}/{name}"


def download_range(url: str, dest: Path, start: int, end: int) -> None:
    """下載 [start, end) 位元組到 dest；中斷後重跑會從已下載的長度續傳。"""
    total = end - start
    part = dest.with_suffix(dest.suffix + ".part")
    have = part.stat().st_size if part.exists() else 0
    t0, last = time.time(), 0.0
    while have < total:
        headers = {"Range": f"bytes={start + have}-{end - 1}"}
        try:
            with (
                httpx.Client(follow_redirects=True, timeout=httpx.Timeout(60, connect=15)) as c,
                c.stream("GET", url, headers=headers) as r,
            ):
                if r.status_code != 206:
                    raise RuntimeError(f"伺服器不支援 Range（HTTP {r.status_code}）")
                with part.open("ab") as f:
                    for chunk in r.iter_bytes(1 << 20):
                        f.write(chunk)
                        have += len(chunk)
                        if time.time() - last > 5:
                            last = time.time()
                            mb = have / 1e6
                            print(
                                f"    {mb:,.0f} / {total / 1e6:,.0f} MB "
                                f"（{mb / max(time.time() - t0, 1e-3):.1f} MB/s）",
                                flush=True,
                            )
        except (httpx.HTTPError, RuntimeError) as e:
            print(f"    連線中斷（{e}），5 秒後續傳…", flush=True)
            time.sleep(5)
    part.rename(dest)


def fetch_text_model() -> Path:
    from huggingface_hub import hf_hub_download

    print(f"[1/3] 語言模型 {GGUF_REPO}/{GGUF_FILE}（約 5.0 GB）")
    path = hf_hub_download(GGUF_REPO, GGUF_FILE, revision=GGUF_REV, local_dir=OUT)
    print(f"    ✓ {path}")
    return Path(path)


def fetch_vision_weights() -> Path:
    """只下載 model.visual.* 權重，重新包成一個 safetensors 檔（位移改成從 0 起算）。"""
    hf_dir = OUT / "hf-visual"
    target = hf_dir / "model.safetensors"
    print(f"[2/3] 影像編碼器 {HF_REPO}（model.visual.*）")
    if target.exists():
        print(f"    ✓ 已存在 {target}")
        return hf_dir
    hf_dir.mkdir(parents=True, exist_ok=True)
    url = hf_url(HF_REPO, HF_REV, VISION_SHARD)
    with httpx.Client(follow_redirects=True, timeout=60) as c:
        n = struct.unpack("<Q", c.get(url, headers={"Range": "bytes=0-7"}).content)[0]
        header = json.loads(c.get(url, headers={"Range": f"bytes=8-{8 + n - 1}"}).content)
        for name in ("config.json", "preprocessor_config.json"):
            (hf_dir / name).write_bytes(c.get(hf_url(HF_REPO, HF_REV, name)).content)
    vis = {k: v for k, v in header.items() if k.startswith(VISION_PREFIX)}
    lo = min(v["data_offsets"][0] for v in vis.values())
    hi = max(v["data_offsets"][1] for v in vis.values())
    base = 8 + n
    print(f"    {len(vis)} 個張量，{(hi - lo) / 1e6:,.0f} MB")
    blob = hf_dir / "visual.bin"
    if not blob.exists():
        download_range(url, blob, base + lo, base + hi)

    new_header = {"__metadata__": {"format": "pt"}}
    for k, v in sorted(vis.items(), key=lambda kv: kv[1]["data_offsets"][0]):
        s, e = v["data_offsets"]
        new_header[k] = {**v, "data_offsets": [s - lo, e - lo]}
    raw = json.dumps(new_header, separators=(",", ":")).encode()
    raw += b" " * (-len(raw) % 8)  # 標頭長度對齊 8 位元組
    tmp = target.with_suffix(".tmp")
    with tmp.open("wb") as f, blob.open("rb") as src:
        f.write(struct.pack("<Q", len(raw)))
        f.write(raw)
        while chunk := src.read(1 << 24):
            f.write(chunk)
    tmp.rename(target)
    blob.unlink()
    print(f"    ✓ {target}")
    return hf_dir


def build_mmproj(hf_dir: Path) -> Path:
    """用 llama.cpp 的 convert_hf_to_gguf.py --mmproj 轉出影像投影檔。"""
    dest = OUT / MMPROJ_FILE
    print(f"[3/3] 轉成 {MMPROJ_FILE}")
    if dest.exists():
        print(f"    ✓ 已存在 {dest}")
        return dest
    tools = OUT / "llama.cpp"
    if not (tools / "convert_hf_to_gguf.py").exists():
        subprocess.run(
            [
                "git",
                "clone",
                "--depth=1",
                "--filter=blob:none",
                "--sparse",
                "--branch",
                LLAMA_CPP_TAG,
                "https://github.com/ggml-org/llama.cpp",
                str(tools),
            ],
            check=True,
        )
        subprocess.run(["git", "sparse-checkout", "set", "gguf-py", "conversion"], cwd=tools)
        subprocess.run(["git", "checkout"], cwd=tools, check=True)
    env = {**os.environ, "PYTHONPATH": str(tools / "gguf-py")}
    subprocess.run(
        [
            sys.executable,
            str(tools / "convert_hf_to_gguf.py"),
            str(hf_dir),
            "--mmproj",
            "--outtype",
            "f16",
            "--outfile",
            str(dest),
        ],
        check=True,
        env=env,
    )
    print(f"    ✓ {dest}")
    return dest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", choices=["text", "vision", "mmproj"])
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    if args.only in (None, "text"):
        fetch_text_model()
    if args.only in (None, "vision", "mmproj"):
        if args.only != "vision" and (OUT / MMPROJ_FILE).exists():
            print(f"[2–3/3] 已有 {MMPROJ_FILE}，略過影像編碼器下載與轉檔")
        else:
            hf_dir = fetch_vision_weights()
            if args.only != "vision":
                build_mmproj(hf_dir)
                # 轉好 mmproj 後中間檔就用不到了（1.15 GB）
                shutil.rmtree(hf_dir, ignore_errors=True)
    print(f"完成。啟動推論伺服器：make ortho2cad（模型在 {OUT}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
