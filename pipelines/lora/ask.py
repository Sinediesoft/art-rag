"""對 llama-server 問一題（docs/adr/043）：跑通流程、確認 keep_alive、抽查推論用。

題目取自 data/lora/ 的 jsonl（select.jsonl 的 dev 題或訓練樣本），換回推論時的格式
（{"type": "image"} → base64 image_url）後送到 OpenAI 相容的 /chat/completions，temperature 0。
--keep-alive 會在請求裡多送 keep_alive，和後端 lora、hybrid 策略在 MODEL_KEEP_ALIVE 有設定時一樣，
用來確認 llama-server 不會因為這個欄位出錯。

用法：python pipelines/lora/ask.py --model art-lora [--file data/lora/select.jsonl] [--row 0]
      [--url http://127.0.0.1:8081/v1] [--keep-alive 60m]
"""

import argparse
import base64
import json
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]


def inference_messages(row: dict, data: Path) -> list[dict]:
    images = iter(row["images"])
    user = dict(row["messages"][1])
    user["content"] = [
        {
            "type": "image_url",
            "image_url": {
                "url": "data:image/jpeg;base64,"
                + base64.b64encode((data / next(images)).read_bytes()).decode()
            },
        }
        if part["type"] == "image"
        else part
        for part in user["content"]
    ]
    return [row["messages"][0], user]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8081/v1")
    ap.add_argument("--model", required=True)
    ap.add_argument("--file", type=Path, default=ROOT / "data" / "lora" / "select.jsonl")
    ap.add_argument("--row", type=int, default=0)
    ap.add_argument("--keep-alive", default="")
    args = ap.parse_args()

    row = json.loads(args.file.read_text("utf-8").splitlines()[args.row])
    body = {
        "model": args.model,
        "messages": inference_messages(row, args.file.parent),
        "temperature": 0,
        "max_tokens": 800,
    }
    if args.keep_alive:
        body["keep_alive"] = args.keep_alive
    r = httpx.post(args.url.rstrip("/") + "/chat/completions", json=body, timeout=600)
    print(f"HTTP {r.status_code}")
    r.raise_for_status()
    print(f"問：{row['question']}\n答：{r.json()['choices'][0]['message']['content']}")
    if row.get("answer"):
        print(f"訓練答案：{row['answer']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
