"""
self_check.py：健康檢查（雙擊 self_check.bat 執行）

一次檢查：環境、資料、切段、索引、搜尋、關鍵字加分，以及「用真正的 MCP 連線呼叫每一個工具」。
這裡的 MCP 檢查是用程式模擬 Claude 桌面版連進來（跟 Claude 桌面版用同一種連線方式），
但它不能取代「在 Claude 桌面版裡真的看到工具並呼叫成功」那一步。
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import sys
from importlib.metadata import version

import search

RESULTS: list[tuple[bool, str]] = []


def check(ok: bool, label: str, detail: str = "") -> bool:
    ok = bool(ok)
    RESULTS.append((ok, label))
    mark = "✔" if ok else "✘"
    print(f"  {mark} {label}" + (f"：{detail}" if detail else ""))
    return ok


def section(title: str) -> None:
    print()
    print(f"【{title}】")


def check_environment() -> None:
    section("1 環境")
    check(sys.version_info[:2] == (3, 12), "Python 版本是 3.12", platform.python_version())
    for pkg in ("mcp", "fastembed", "numpy"):
        try:
            check(True, f"已安裝 {pkg}", version(pkg))
        except Exception:
            check(False, f"已安裝 {pkg}", "找不到，請重新雙擊 setup.bat")
    path = str(search.ROOT)
    check(path.isascii(), "專案路徑沒有中文或特殊字元", path)
    if search.FAKE_EMBED:
        print("  ⚠ 目前用的是「假向量」（雲端測試模式），分數不代表真實效果")


def check_data() -> dict:
    section("2 資料")
    artworks, problems = search.load_artworks()
    check(len(artworks) > 0, "讀得到作品資料", f"{len(artworks)} 件")
    check(not problems, "所有作品格式都通過基本檢查", "；".join(problems) if problems else "")
    return artworks


def check_chunking(artworks: dict) -> None:
    section("3 切段")
    check(search.split_text("短短一句。") == ["短短一句。"], "短段落不切")
    long_text = "這是一句測試用的句子，長度大約二十個字。" * 40
    pieces = search.split_text(long_text)
    check(
        len(pieces) > 1 and all(len(p) <= search.MAX_CHARS for p in pieces),
        "長段落會照句號切開，每段不超過上限",
        f"{len(long_text)} 字 → {len(pieces)} 段",
    )
    check("".join(pieces) == long_text, "切開後內容一個字都沒少")
    passages, skipped = search.make_passages(artworks)
    check(
        all(p.source_url and p.license for p in passages),
        "每一段都帶著出處和授權",
        f"{len(passages)} 段",
    )
    if skipped:
        print(f"  ・依規則不收的段落：{'；'.join(skipped)}")


def check_index_and_search() -> None:
    section("4 索引與搜尋")
    try:
        s = search.Searcher()
    except search.IndexNotReady as e:
        check(False, "索引已建立", str(e))
        return
    check(
        True,
        "索引已建立",
        f"{s.meta['n_passages']} 段，模型 {s.meta['model']}，建立於 {s.meta['built_at']}",
    )
    check(
        not s.stale,
        "索引是最新的（資料改過後有重建）",
        "資料改過了，請雙擊 build_index.bat" if s.stale else "",
    )
    try:
        result = s.search("谿山行旅圖的簽名是誰發現的？")
    except Exception as e:  # 模型沒下載、檔案壞掉等
        check(False, "搜尋可以執行", str(e))
        return
    top = result["results"][0] if result["results"] else None
    check(
        top is not None and top["artwork_id"] == "npm-000001",
        "問谿山行旅圖的簽名，第 1 名是谿山行旅圖的段落",
        f"第 1 名：{top['title']}｜{top['topic']}（分數 {top['score']}）" if top else "沒有結果",
    )
    # 關鍵字加分：問題和段落裡都有「范寬」，那段要剛好多 KEYWORD_BONUS 分
    with_bonus = {
        r["passage"].passage_id: r for r in s.score_all("范寬是怎麼畫山的？", use_bonus=True)
    }
    without = {
        r["passage"].passage_id: r for r in s.score_all("范寬是怎麼畫山的？", use_bonus=False)
    }
    hit = [pid for pid, r in with_bonus.items() if "范寬" in r["matched_keywords"]]
    ok = bool(hit) and all(
        abs(with_bonus[pid]["score"] - without[pid]["score"] - with_bonus[pid]["bonus"]) < 1e-6
        for pid in hit
    )
    check(ok, "關鍵字加分有作用（含「范寬」的段落有加分）", f"{len(hit)} 段加分")
    odd = s.search("請問台北 101 的電梯速度是多少？")
    print(
        f"  ・無關問題的最高分：{odd['best_score']}（門檻 {odd['threshold']}，"
        f"{'判定找不到 ✔' if not odd['found'] else '沒有判定為找不到，階段 4 要調門檻'}）"
    )


async def check_mcp() -> None:
    from mcp import Client
    from mcp.client.stdio import StdioServerParameters

    section("5 MCP 連線（模擬 Claude 桌面版，用同樣的標準輸入輸出方式連線）")
    env = {k: v for k, v in os.environ.items() if k.startswith("ARTMCP_")}
    env.update({"PYTHONUTF8": "1", "HF_HUB_OFFLINE": "1"})

    def params(script: str) -> StdioServerParameters:
        return StdioServerParameters(
            command=sys.executable, args=[str(search.ROOT / script)], env=env, cwd=str(search.ROOT)
        )

    def as_json(r) -> dict:
        """工具回傳的文字轉成資料；如果是錯誤訊息，就放在 error 欄位。"""
        text = r.content[0].text if r.content else ""
        if r.is_error:
            return {"error": text}
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"error": text}

    # 5-1 hello 測試工具
    try:
        async with Client(params("hello_server.py"), mode="legacy", read_timeout_seconds=60) as c:
            tools = [t.name for t in (await c.list_tools()).tools]
            check(tools == ["hello"], "hello_server：看得到 hello 工具", str(tools))
            r = await c.call_tool("hello", {"name": "Door"})
            check(
                not r.is_error and "連線是通的" in r.content[0].text,
                "hello_server：呼叫 hello 成功",
                r.content[0].text,
            )
    except Exception as e:
        check(False, "hello_server：連線成功", repr(e))

    # 5-2 正式 Server
    try:
        async with Client(params("server.py"), mode="legacy", read_timeout_seconds=120) as c:
            tools = sorted(t.name for t in (await c.list_tools()).tools)
            check(
                tools == ["get_artwork", "list_artworks", "search_artworks", "search_passages"],
                "server：看得到 4 個工具",
                str(tools),
            )

            r = await c.call_tool("list_artworks", {})
            data = as_json(r)
            check(
                data.get("count", 0) > 0,
                "list_artworks：列出作品",
                data.get("error") or f"{data['count']} 件",
            )

            r = await c.call_tool("get_artwork", {"artwork_id": "npm-000001"})
            data = as_json(r)
            check(
                data.get("credit_line") and data.get("descriptions"),
                "get_artwork：取得完整資料、授權與標示文字",
                data.get("error") or data["credit_line"],
            )

            r = await c.call_tool("get_artwork", {"artwork_id": "npm-00001"})
            check(
                r.is_error and "list_artworks" in r.content[0].text,
                "get_artwork：編號打錯時，回報錯誤並建議下一步",
                r.content[0].text[:60],
            )

            r = await c.call_tool("search_passages", {"query": "谿山行旅圖的簽名是誰發現的？"})
            data = as_json(r)
            first = data["results"][0] if data.get("results") else {}
            check(
                first.get("source_url"),
                "search_passages：回傳段落並附出處",
                data.get("error") or f"第 1 名 {first.get('title')}｜{first.get('topic')}",
            )

            r = await c.call_tool("search_passages", {"query": "   "})
            check(
                r.is_error and "不能是空的" in r.content[0].text,
                "search_passages：空白問題會被擋下",
            )

            r = await c.call_tool("search_artworks", {"query": "點描法"})
            data = as_json(r)
            check(
                "results" in data,
                "search_artworks：用描述找作品",
                data.get("error")
                or (f"第 1 名 {data['results'][0]['title']}" if data["results"] else "沒有結果"),
            )

            res = await c.read_resource("artworks://catalog")
            check("館藏清單" in res.contents[0].text, "資料 artworks://catalog：讀得到館藏清單")

            p = await c.get_prompt("tour_script", {"topic": "谿山行旅圖", "minutes": "3"})
            check(
                "search_passages" in p.messages[0].content.text, "範本 tour_script：取得導覽稿範本"
            )
    except Exception as e:
        check(False, "server：連線成功", repr(e))

    log_file = search.ROOT / "logs" / "tool_calls.jsonl"
    check(log_file.exists(), "每次工具呼叫都有記到 logs/tool_calls.jsonl")


def main() -> int:
    print("art-mcp 健康檢查")
    check_environment()
    artworks = check_data()
    check_chunking(artworks)
    check_index_and_search()
    asyncio.run(check_mcp())
    passed = sum(ok for ok, _ in RESULTS)
    print()
    print(f"結果：{passed}/{len(RESULTS)} 項通過")
    if passed < len(RESULTS):
        print("有沒通過的項目：請把這個視窗截圖給 Claude。")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
