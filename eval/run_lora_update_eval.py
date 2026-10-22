"""知識更新評估（make eval-lora 的第三步，docs/adr/043）：改知識庫裡的 5 個事實，看誰跟得上。

在暫存目錄複製一份專題（backend、shared、kb、pipelines），把 FACTS 的舊說法換成新說法，
用副本重建索引、在另一個埠啟動後端，再對每個策略問這 5 題：
- updated：回答有新說法、沒有舊說法（hybrid 重建索引後應該做到）
- stale：回答還是舊說法（知識在權重裡的 lora_norag 預期如此）

副本沒有 .env（不複製、不連結原檔，複製完還會確認）：設定只來自呼叫端的環境變數。
make eval-lora 帶入 Makefile 的 LORA_EVAL_ENV，和 make eval-lora-backend 同一組；
另外清空 DATABASE_URL、JOBS_DATABASE_URL、REDIS_URL 並關掉記憶體管理：
不寫正式資料庫、不碰背景工作佇列、不卸載其他程式的模型。

用法：python eval/run_lora_update_eval.py [--strategies hybrid,hybrid_norag,lora_norag]
      [--port 8011] [--keep]
"""

import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx

EVAL = Path(__file__).resolve().parent
ROOT = EVAL.parent
sys.path.insert(0, str(EVAL))
from run_eval import STRATEGY_BODY, run_chat  # noqa: E402

# (畫作, 問題, 舊說法, 新說法)：舊說法在該畫作 JSON 裡全部換成新說法
FACTS = [
    ("npm-000001", "山石用了什麼皴法？", "雨點皴", "斧劈皴"),
    ("npm-000001", "作者的簽名是哪一年被發現的？", "1958", "1966"),
    ("aic-27992", "這幅畫首次在第幾屆印象派畫展展出？", "第八屆", "第五屆"),
    ("met-436535", "大都會藝術博物館是哪一年購得這幅畫的？", "1993", "1987"),
    ("met-436535", "梵谷在信中把絲柏的線條比喻成什麼？", "埃及方尖碑", "希臘神殿的石柱"),
]
COPY = ("backend/app", "backend/pyproject.toml", "shared", "kb", "pipelines")


def make_copy(dest: Path) -> None:
    for rel in COPY:
        src = ROOT / rel
        if src.is_dir():
            shutil.copytree(src, dest / rel, ignore=shutil.ignore_patterns("__pycache__"))
        else:
            (dest / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest / rel)
    leaked = sorted(
        str(p.relative_to(dest)) for p in dest.rglob(".env*") if p.name != ".env.example"
    )
    if leaked:
        raise SystemExit(f"副本裡不應該有 .env：{leaked}")
    for aid, _, old, new in FACTS:
        path = dest / "kb" / "artworks" / f"{aid}.json"
        text = path.read_text("utf-8")
        if old not in text:
            raise SystemExit(f"{aid} 找不到要改的事實「{old}」，FACTS 需要更新")
        path.write_text(text.replace(old, new), "utf-8")


def score(answer: str, old: str, new: str) -> dict:
    return {"updated": new in answer and old not in answer, "stale": old in answer}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategies", default="hybrid,hybrid_norag,lora_norag")
    ap.add_argument("--port", type=int, default=8011)
    ap.add_argument("--keep", action="store_true", help="保留暫存副本（除錯用）")
    args = ap.parse_args()

    python = ROOT / "backend" / ".venv" / "bin" / "python"
    tmp = Path(tempfile.mkdtemp(prefix="artrag-kbupdate-"))
    env = {
        **os.environ,
        "DATA_DIR": str(tmp / "data"),
        "DATABASE_URL": "",
        "JOBS_DATABASE_URL": "",
        "REDIS_URL": "",
        "MEMORY_GUARD": "false",
        "EVAL_CONTROLS": "true",
        "DEMO_CONTROLS": "true",
    }
    server = None
    try:
        make_copy(tmp)
        print(f"知識庫副本：{tmp}（改了 {len(FACTS)} 個事實），重建索引中…")
        subprocess.run([str(python), "pipelines/build_index.py"], cwd=tmp, env=env, check=True)
        server = subprocess.Popen(
            [str(python), "-m", "uvicorn", "app.main:app", "--port", str(args.port)],
            cwd=tmp / "backend",
            env=env,
        )
        base = f"http://127.0.0.1:{args.port}"
        client = httpx.Client(base_url=base, timeout=60)
        for _ in range(240):
            try:
                if client.get("/api/v1/health").status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if server.poll() is not None:
                raise SystemExit("副本後端啟動失敗")
            time.sleep(1)
        else:
            raise SystemExit("副本後端 240 秒內沒有就緒")
        client.get("/api/v1/auth/accounts").raise_for_status()
        client.post("/api/v1/auth/switch", json={"account_id": "manager"}).raise_for_status()
        # 副本後端要讀到改過的知識庫（不是原本專題的 app 與 kb）
        for aid, _, old, new in FACTS:
            text = client.get(f"/api/v1/artworks/{aid}").text
            if new not in text or old in text:
                raise SystemExit(f"副本後端讀到的 {aid} 不是改過的版本（{old} → {new}）")

        rows, by_strategy, broken = [], {}, []
        for strat in args.strategies.split(","):
            results = []
            for aid, question, old, new in FACTS:
                body = {
                    "question": question,
                    "artwork_id": aid,
                    "allow_fallback": False,
                    **STRATEGY_BODY[strat],
                }
                res = run_chat(client, body)
                done = res["done"] or {}
                fallback = bool(done.get("fallback"))
                error = (res["error"] or {}).get("code", "")
                verdict = score(res["answer"], old, new) if not (error or fallback) else {}
                row = {
                    "strategy": strat,
                    "artwork_id": aid,
                    "question": question,
                    "old": old,
                    "new": new,
                    "model": done.get("model", ""),
                    "fallback": fallback,
                    "error": error,
                    "updated": verdict.get("updated", False),
                    "stale": verdict.get("stale", False),
                    "answer": res["answer"].replace("\n", " "),
                }
                rows.append(row)
                results.append(row)
                if strat.startswith("lora") and (fallback or error):
                    broken.append(f"{strat} {question}")
                mark = "新" if row["updated"] else "舊" if row["stale"] else "？"
                print(f"  [{strat}] {mark} {question} {row['answer'][:60]}")
            by_strategy[strat] = {
                "n": len(results),
                "updated": sum(r["updated"] for r in results) / len(results),
                "stale": sum(r["stale"] for r in results) / len(results),
                "fallbacks": sum(r["fallback"] for r in results),
                "errors": sum(1 for r in results if r["error"]),
                "models": sorted({r["model"] for r in results if r["model"]}),
            }

        run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:4]
        runs = EVAL / "runs"
        runs.mkdir(exist_ok=True)
        with (runs / f"{run_id}-kbupdate.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        summary = {
            "run_id": run_id,
            "created_at": datetime.now(UTC).isoformat(),
            "facts": [{"artwork_id": a, "question": q, "old": o, "new": n} for a, q, o, n in FACTS],
            "by_strategy": by_strategy,
        }
        (runs / f"{run_id}-kbupdate.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(by_strategy, ensure_ascii=False, indent=2))
        print(f"結果已寫入 eval/runs/{run_id}-kbupdate.*")
        if broken:
            print("LoRA 有題目不是由 LoRA 回答：" + "、".join(broken))
            return 3
        return 0
    finally:
        if server is not None:
            server.terminate()
            server.wait(timeout=30)
        if args.keep:
            print(f"保留副本：{tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
