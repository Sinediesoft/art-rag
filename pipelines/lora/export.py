"""LoRA 不檢索對照組的轉檔（docs/adr/043）：fuse＋de-quantize → HF 格式 → GGUF Q4_K_M。

底模是語言層 4-bit 的 MLX 版（視覺層保留原精度，見 Makefile 的 base）：
- art-lora：掛上 adapter → fuse 時 de-quantize → 其餘量化層 de-quantize
- art-base：不掛 adapter，直接 de-quantize；三組比較時權重經過相同的處理
語言層換回 HF 的權重名稱、設定檔與 tokenizer 用原版，交給 llama.cpp 的 convert_hf_to_gguf.py
轉成 bf16 GGUF（只含語言模型），再用 llama-quantize 轉成 Q4_K_M。
影像編碼器沿用 Qwen 官方 GGUF 的 mmproj，兩個模型共用。

用法（repo 根目錄）：
    uv run --project pipelines/lora python pipelines/lora/export.py --name art-lora \\
        --adapter models/lora/adapters/run1/chosen
    uv run --project pipelines/lora python pipelines/lora/export.py --name art-base --base-only
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import mlx.core as mx
from mlx.utils import tree_flatten

ROOT = Path(__file__).resolve().parents[2]
MODELS = ROOT / "models" / "lora"
BASE = MODELS / "mlx" / "Qwen3-VL-4B-Instruct-4bit"
HF = MODELS / "hf" / "Qwen3-VL-4B-Instruct"  # 只用設定檔、tokenizer 與權重名稱表
GGUF = MODELS / "gguf"
MMPROJ = GGUF / "mmproj-Qwen3VL-4B-Instruct-F16.gguf"
# 與 brew 安裝的 llama-server 同版（和 setup_ortho2cad.py 一致）
LLAMA_CPP_TAG = os.environ.get("LLAMA_CPP_TAG", "b11146")
TOOLS = MODELS / "llama.cpp"
SHARD_BYTES = 2 * 1024**3


def hf_name(mlx_name: str) -> str:
    """mlx-vlm 的 language_model 參數名稱 → HF 權重名稱（qwen3_vl.sanitize 的反向）。"""
    if mlx_name.startswith("model."):
        return "model.language_model." + mlx_name[len("model.") :]
    if mlx_name.startswith("lm_head."):
        return mlx_name
    raise ValueError(f"不認得的語言層參數：{mlx_name}")


def language_weights(adapter: Path | None) -> dict[str, mx.array]:
    import mlx.nn as nn
    from mlx_vlm.trainer.common.utils import set_module_by_name
    from mlx_vlm.trainer.peft.lora_layers import LoRALinear
    from mlx_vlm.trainer.peft.utils import apply_lora_layers, dequantize
    from mlx_vlm.utils import load_model

    model = load_model(BASE, lazy=False)
    if adapter is not None:
        apply_lora_layers(model, adapter)
        fused = [(n, m) for n, m in model.named_modules() if isinstance(m, LoRALinear)]
        if not fused:
            raise SystemExit(f"{adapter} 沒有任何 LoRA 層")
        for name, layer in fused:
            set_module_by_name(model, name, layer.fuse(dequantize=True))
        print(f"    fuse {len(fused)} 個 LoRA 層")
    dequantize(model)
    quantized = (nn.QuantizedLinear, nn.QuantizedEmbedding)
    leftover = [n for n, m in model.language_model.named_modules() if isinstance(m, quantized)]
    if leftover:
        raise SystemExit(f"還有量化層沒有 de-quantize：{leftover[:3]}")
    params = dict(tree_flatten(model.language_model.parameters()))
    return {hf_name(k): v.astype(mx.bfloat16) for k, v in params.items()}


def write_hf_dir(weights: dict[str, mx.array], dest: Path) -> None:
    """原版的設定檔與 tokenizer＋語言層權重，存成分片 safetensors（不含視覺層）。"""
    index = json.loads((HF / "model.safetensors.index.json").read_text())["weight_map"]
    expected = {k for k in index if k.startswith(("model.language_model.", "lm_head."))}
    if set(weights) != expected:
        missing, extra = expected - set(weights), set(weights) - expected
        raise SystemExit(f"語言層權重名稱對不上：少 {sorted(missing)[:3]}、多 {sorted(extra)[:3]}")
    dest.mkdir(parents=True, exist_ok=True)
    for f in HF.iterdir():
        if f.is_file() and f.suffix in {".json", ".txt", ".jinja"} and "index" not in f.name:
            shutil.copy2(f, dest / f.name)
    shards, current, size = [], {}, 0
    for k in sorted(weights):
        if size + weights[k].nbytes > SHARD_BYTES and current:
            shards.append(current)
            current, size = {}, 0
        current[k] = weights[k]
        size += weights[k].nbytes
    shards.append(current)
    weight_map = {}
    for i, shard in enumerate(shards, 1):
        name = f"model-{i:05d}-of-{len(shards):05d}.safetensors"
        mx.save_safetensors(str(dest / name), shard, metadata={"format": "pt"})
        weight_map |= dict.fromkeys(shard, name)
    total = sum(v.nbytes for v in weights.values())
    (dest / "model.safetensors.index.json").write_text(
        json.dumps({"metadata": {"total_size": total}, "weight_map": weight_map}, indent=2)
    )


def llama_cpp_tools() -> Path:
    if not (TOOLS / "convert_hf_to_gguf.py").exists():
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
                str(TOOLS),
            ],
            check=True,
        )
        subprocess.run(["git", "sparse-checkout", "set", "gguf-py", "conversion"], cwd=TOOLS)
        subprocess.run(["git", "checkout"], cwd=TOOLS, check=True)
    return TOOLS


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True, help="llama-server 的模型別名（art-lora、art-base）")
    target = ap.add_mutually_exclusive_group(required=True)
    target.add_argument("--adapter", type=Path, help="adapter 檔或含 adapters.safetensors 的資料夾")
    target.add_argument("--base-only", action="store_true", help="不掛 adapter（art-base）")
    ap.add_argument("--keep", action="store_true", help="保留 HF 格式與 bf16 GGUF 中間檔")
    args = ap.parse_args()

    if not MMPROJ.exists():
        raise SystemExit(f"找不到 {MMPROJ}，請先執行 make -C pipelines/lora base")
    GGUF.mkdir(parents=True, exist_ok=True)
    hf_dir = MODELS / "export" / args.name
    bf16 = GGUF / f"{args.name}-bf16.gguf"
    q4 = GGUF / f"{args.name}-q4_k_m.gguf"

    print(f"[1/3] {args.name}：{'de-quantize' if args.base_only else 'fuse＋de-quantize'}")
    write_hf_dir(language_weights(None if args.base_only else args.adapter), hf_dir)
    print("[2/3] 轉成 bf16 GGUF（只含語言模型）")
    tools = llama_cpp_tools()
    subprocess.run(
        [sys.executable, str(tools / "convert_hf_to_gguf.py"), str(hf_dir)]
        + ["--outtype", "bf16", "--outfile", str(bf16)],
        check=True,
        env={**os.environ, "PYTHONPATH": str(tools / "gguf-py")},
    )
    print("[3/3] 量化成 Q4_K_M")
    subprocess.run(["llama-quantize", str(bf16), str(q4), "Q4_K_M"], check=True)
    if not args.keep:
        bf16.unlink()
        shutil.rmtree(hf_dir)
    print(f"完成：{q4}（影像編碼器用 {MMPROJ.name}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
