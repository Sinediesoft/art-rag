"""LoRA 不檢索對照組的訓練（docs/adr/043）：mlx-vlm 對 Qwen3-VL-4B-Instruct 做 QLoRA。

底模是語言層 4-bit（group 64）、視覺層保留原精度的 MLX 版（Makefile 的 base）；
r=16、alpha=32、dropout 0.05；只調語言層的 q/k/v/o/gate/up/down，視覺編碼器凍結；
batch 1、梯度累積 16、gradient checkpointing、lr 2e-4，只對回答算 loss。
每個 epoch 結束存一次 adapter（<iter>_adapters.safetensors），之後用 select_epoch.py
以 eval/qa.jsonl 的 dev 題挑 epoch。

結束時寫 train_report.json：訓練時間、MLX 回報的尖峰記憶體、adapter 大小、資料筆數、指令與 git SHA。

用法（repo 根目錄）：
    uv run --project pipelines/lora python pipelines/lora/train.py --data data/lora \\
        --out models/lora/adapters/run1 [--epochs 4 | --iters 20] [--image-size 672]
"""

import argparse
import json
import re
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / "models" / "lora" / "mlx" / "Qwen3-VL-4B-Instruct-4bit"
TARGETS = ["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"]


def count_rows(path: Path) -> int:
    return sum(1 for line in path.read_text("utf-8").splitlines() if line.strip())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, default=ROOT / "data" / "lora")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--model", type=Path, default=BASE)
    group = ap.add_mutually_exclusive_group()
    group.add_argument("--epochs", type=int, default=4)
    group.add_argument("--iters", type=int, help="只跑這麼多步（量速度、跑通流程用）")
    ap.add_argument("--image-size", type=int, default=0, help="記憶體不夠時把訓練圖縮到這個邊長")
    ap.add_argument("--max-seq-length", type=int, default=2048)
    args = ap.parse_args()

    n_train = count_rows(args.data / "train.jsonl")
    args.out.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        "-m",
        "mlx_vlm.train",
        "--model",
        str(args.model),
        "--data",
        str(args.data),
        "--train-type",
        "lora",
        "--lora-rank",
        "16",
        "--lora-alpha",
        "32",
        "--lora-dropout",
        "0.05",
        "--target-modules",
        *TARGETS,
        "--batch-size",
        "1",
        "--gradient-accumulation-steps",
        "16",
        "--grad-checkpoint",
        "--learning-rate",
        "2e-4",
        "--train-on-completions",
        "--max-seq-length",
        str(args.max_seq_length),
        # batch 1：一個 epoch 就是 n_train 步，每個 epoch 結束存檔、算一次驗證 loss
        "--steps-per-save",
        str(n_train if not args.iters else args.iters),
        "--steps-per-eval",
        str(n_train if not args.iters else args.iters),
        "--steps-per-report",
        "10",
        "--output-path",
        str(args.out / "adapters.safetensors"),
        "--seed",
        "20261021",
    ]
    cmd += ["--iters", str(args.iters)] if args.iters else ["--epochs", str(args.epochs)]
    if args.image_size:
        size = [args.image_size, args.image_size]
        cmd += ["--dataset-config", json.dumps({"image_resize_shape": size})]

    log_path = args.out / "train.log"
    t0 = time.time()
    peak, losses = 0.0, []
    with log_path.open("w", encoding="utf-8") as log:
        proc = subprocess.Popen(
            cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        )
        for line in proc.stdout:
            sys.stdout.write(line)
            log.write(line)
            if m := re.search(r"peak_memory=([\d.]+)", line):
                peak = max(peak, float(m.group(1)))
            if m := re.search(r"(train_loss|val_loss)=([\d.]+)", line):
                losses.append({m.group(1): float(m.group(2))})
        code = proc.wait()
    elapsed = time.time() - t0

    adapter = args.out / "adapters.safetensors"
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True
    ).stdout.strip()
    report = {
        "created_at": datetime.now(UTC).isoformat(),
        "git_sha": sha,
        "exit_code": code,
        "command": cmd,
        "train_rows": n_train,
        "valid_rows": count_rows(args.data / "valid.jsonl"),
        "elapsed_s": round(elapsed),
        "peak_memory_gb": peak,
        "adapter_bytes": adapter.stat().st_size if adapter.exists() else None,
        "checkpoints": sorted(p.name for p in args.out.glob("*_adapters.safetensors")),
        "losses": losses,
    }
    (args.out / "train_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), "utf-8"
    )
    print(f"訓練 {elapsed / 60:.1f} 分鐘、尖峰記憶體 {peak:.2f} GB，結果在 {args.out}")
    return code


if __name__ == "__main__":
    sys.exit(main())
