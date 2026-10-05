"""3D 重建評估拆兩半的前半（docs/5070ti-host.md）。

產生知識庫 6 張圖紙的 CadQuery 程式碼，存成 data/cad_codes/<label>/mfg-00x.py。
輸入和 cad_service 相同：
知識庫圖紙（layout kb）→ model_input 前處理 → models.yaml 的 cad.prompt、temperature、max_tokens；
Ortho2CAD 縮到 cad.max_pixels，未微調的對照組縮到 800×800 並加 cad.baseline_suffix
（和 cad_service 的 strategy != ortho2cad 相同）。

為什麼要拆：Windows 限制不了子行程，不能執行模型產生的程式碼（app/cad/sandbox.py），
但 GPU 推論在 Windows 最快；所以在 Windows 產生程式碼，再到 WSL／Linux 用 eval/cad_score.py
執行並算 IoU。macOS／Linux 直接用 make eval-cad 即可。

用法（後端不用開，推論伺服器要開）：
  python eval/cad_generate.py ortho2cad    # llama-server :8081（make ortho2cad）
  python eval/cad_generate.py qwen3-vl:8b-instruct --baseline   # Ollama :11434，未微調對照組
"""

import argparse
import base64
import io
import json
import sys
import time
from pathlib import Path

import httpx
import yaml
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.cad.preprocess import model_input  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("model", help="ortho2cad（llama-server 的 alias）或 Ollama 模型名稱")
    ap.add_argument(
        "--baseline", action="store_true", help="未微調對照組：800×800＋baseline_suffix"
    )
    ap.add_argument("--base-url", default=None, help="預設 ortho2cad → :8081，其他 → Ollama :11434")
    ap.add_argument("--label", default=None, help="輸出資料夾名稱，預設用模型名稱")
    args = ap.parse_args()

    cfg = yaml.safe_load((ROOT / "shared/models.yaml").read_text(encoding="utf-8"))["cad"]
    base = args.base_url or (
        "http://127.0.0.1:8081/v1" if args.model == "ortho2cad" else "http://127.0.0.1:11434/v1"
    )
    max_pixels = 800 * 800 if args.baseline else int(cfg["max_pixels"])
    prompt = str(cfg["prompt"]) + (str(cfg["baseline_suffix"]) if args.baseline else "")
    out = ROOT / "data/cad_codes" / (args.label or args.model.replace(":", "_"))
    out.mkdir(parents=True, exist_ok=True)

    if args.model == "ortho2cad":
        # 這支腳本直接打 llama-server、不經過後端，後端的記憶體管理不知道 Ortho2CAD 正在用；
        # VRAM 超過門檻時會把載入中的 Ortho2CAD 卸載（ADR 021），請求就回 500。
        # 所以先請 Ollama 卸載它的模型，和後端進入 3D 重建流程時一樣讓出 VRAM（ADR 006）
        try:
            ps = httpx.get("http://127.0.0.1:11434/api/ps", timeout=3).json().get("models", [])
            for m in ps:
                httpx.post(
                    "http://127.0.0.1:11434/api/generate",
                    json={"model": m["name"], "keep_alive": 0},
                    timeout=30,
                )
            if ps:
                print(f"  先卸載 Ollama 的 {len(ps)} 個模型，讓出 VRAM", flush=True)
        except httpx.HTTPError:
            pass

    rows = []
    for png in sorted((ROOT / "kb/drawings").glob("mfg-*.png")):
        img = model_input(Image.open(png).convert("RGB"), "kb", max_pixels)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode()
        body = {
            "model": args.model,
            "temperature": float(cfg["temperature"]),
            "max_tokens": int(cfg["max_tokens"]),
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
        }
        t0 = time.perf_counter()
        r = httpx.post(f"{base}/chat/completions", json=body, timeout=900)
        r.raise_for_status()
        j = r.json()
        (out / f"{png.stem}.py").write_text(j["choices"][0]["message"]["content"], encoding="utf-8")
        rows.append(
            {
                "part": png.stem,
                "output_tokens": j["usage"]["completion_tokens"],
                "seconds": round(time.perf_counter() - t0, 1),
            }
        )
        print(
            f"  {png.stem}：{rows[-1]['output_tokens']} token，{rows[-1]['seconds']} 秒", flush=True
        )
    meta = {"model": args.model, "baseline": args.baseline, "max_pixels": max_pixels, "rows": rows}
    (out / "generate.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(f"→ {out.relative_to(ROOT)}（接著在 WSL／Linux 跑 eval/cad_score.py）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
