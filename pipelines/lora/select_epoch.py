"""用 eval/qa.jsonl 的 dev 題挑 epoch（docs/adr/043）。

dev 只用在這裡，test 只在 make eval-lora 跑一次。

data/lora/select.jsonl 是 make_lora_data 產生的 dev 題（prompt 和推論時相同、沒有答案）。
每個 epoch 的 adapter 都在 MLX 上以 temperature 0 回答，用關鍵字判對錯（和 run_eval 一樣：
每一組關鍵字至少出現一個）。答對最多的 epoch 勝出，同分取較早的（訓練越久越容易忘掉別的能力）；
勝出的 adapter 複製到 <adapters>/chosen/，給 export.py 用。

用法（repo 根目錄）：
    uv run --project pipelines/lora python pipelines/lora/select_epoch.py \\
        --adapters models/lora/adapters/run1
"""

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / "models" / "lora" / "mlx" / "Qwen3-VL-4B-Instruct-4bit"


def answer(model, processor, row: dict, data: Path) -> str:
    from mlx_vlm import generate
    from mlx_vlm.prompt_utils import apply_chat_template

    images = [str(data / p) for p in row["images"]]
    prompt = apply_chat_template(
        processor, model.config, row["messages"], add_generation_prompt=True, num_images=len(images)
    )
    out = generate(model, processor, prompt, image=images or None, max_tokens=400, temperature=0.0)
    return getattr(out, "text", out)


def correct(text: str, keywords: list[list[str]]) -> bool:
    return all(any(k in text for k in group) for group in keywords)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--adapters", type=Path, required=True)
    ap.add_argument("--data", type=Path, default=ROOT / "data" / "lora")
    ap.add_argument("--model", type=Path, default=BASE)
    args = ap.parse_args()

    from mlx_vlm import load

    rows = [json.loads(x) for x in (args.data / "select.jsonl").read_text("utf-8").splitlines()]
    checkpoints = sorted(args.adapters.glob("*_adapters.safetensors"))
    if not checkpoints:
        raise SystemExit(f"{args.adapters} 沒有 *_adapters.safetensors")
    results = []
    for epoch, ckpt in enumerate(checkpoints, 1):
        model, processor = load(str(args.model), adapter_path=str(ckpt))
        answers = [answer(model, processor, r, args.data) for r in rows]
        ok = [correct(a, r["keywords"]) for a, r in zip(answers, rows, strict=True)]
        results.append(
            {
                "epoch": epoch,
                "checkpoint": ckpt.name,
                "dev_correct": sum(ok),
                "dev_n": len(rows),
                "answers": [
                    {"id": r["id"], "ok": o, "answer": a}
                    for r, o, a in zip(rows, ok, answers, strict=True)
                ],
            }
        )
        print(f"epoch {epoch}（{ckpt.name}）：dev {sum(ok)}／{len(rows)}")
        del model

    best = max(results, key=lambda r: (r["dev_correct"], -r["epoch"]))
    chosen = args.adapters / "chosen"
    chosen.mkdir(exist_ok=True)
    shutil.copy2(args.adapters / best["checkpoint"], chosen / "adapters.safetensors")
    shutil.copy2(args.adapters / "adapter_config.json", chosen / "adapter_config.json")
    report = {"chosen_epoch": best["epoch"], "chosen": best["checkpoint"], "epochs": results}
    (args.adapters / "select_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), "utf-8"
    )
    print(f"選 epoch {best['epoch']}（dev {best['dev_correct']}／{best['dev_n']}）→ {chosen}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
