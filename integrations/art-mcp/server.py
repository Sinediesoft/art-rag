"""
server.py：你的 MCP Server（櫃台）

對照《00_專案學習地圖.md》第 4 節 B 段：
  B1  Claude 讀這裡每個工具的「說明」（服務項目表），決定要用哪一個
  B2  Claude 透過 MCP（統一格式的點單）把請求送進來
  B3  交給 search.py 找資料
  B4  把結果＋出處＋授權傳回去，並在 logs/tool_calls.jsonl 記一行（登記簿）

提供的內容：
  工具 search_artworks   用描述找作品
  工具 get_artwork       一件作品的完整資料（含授權與標示文字）
  工具 search_passages   最相關的 3 段原文與出處（回答由 AI 寫）
  工具 list_artworks     列出全部作品
  資料 artworks://catalog   館藏清單
  範本 tour_script          導覽稿

重要：這個程式透過「標準輸入輸出」和 Claude 溝通，所以絕對不能用 print()，
     要記錄東西請用 log（寫到 logs/server.log）。

工具說明（每個 @mcp.tool 底下的 description）：10/26 已換成 Door 改寫的版本（階段 5 步驟 2）。
「# Door：」開頭的註解是 Door 寫的（階段 5 步驟 1）。
"""

# Door：① 匯入套件與外部模組（包括我階段 4 的 search.py），沒有這步程式就無法正常運行。
from __future__ import annotations

import difflib
import json
import logging
import time
from contextlib import contextmanager
from datetime import datetime

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

import search

# ─────────────────────────────────────────────
# 紀錄：伺服器狀況寫進 logs/server.log；每次工具呼叫寫進 logs/tool_calls.jsonl
# ─────────────────────────────────────────────
# Door：② 建立 logs/ 資料夾，設定伺服器日誌 server.log，記錄時間、報錯、運作狀態。
LOG_DIR = search.ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)
logging.basicConfig(
    filename=LOG_DIR / "server.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    encoding="utf-8",
)
log = logging.getLogger("art-rag")
TOOL_LOG = LOG_DIR / "tool_calls.jsonl"


# Door：③ Claude 呼叫任何工具時，立刻記下工具、參數、時間、花多久、成功與否，寫進 logs/tool_calls.jsonl。
@contextmanager
def log_call(tool: str, args: dict):
    """包住每次工具呼叫：記下時間、工具、參數、花多久、成功與否、回傳幾筆。"""
    record = {"time": datetime.now().isoformat(timespec="seconds"), "tool": tool, "args": args}
    t0 = time.perf_counter()
    try:
        yield record
        record["ok"] = True
    except Exception as e:
        record["ok"] = False
        record["error"] = str(e)
        raise
    finally:
        record["ms"] = round((time.perf_counter() - t0) * 1000)
        with TOOL_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        log.info("tool=%s ok=%s ms=%s", tool, record.get("ok"), record["ms"])


# ─────────────────────────────────────────────
# 資料：作品資料直接讀 data/（不需要模型）；搜尋第一次用到時才載入
# ─────────────────────────────────────────────
# Door：④ 作品清單 ARTWORKS 很輕，一啟動就讀；向量搜尋引擎 Searcher 很重，等第一次搜尋才載入（懶載入），免得啟動太久讓 Claude 以為故障。
ARTWORKS, PROBLEMS = search.load_artworks()
for p in PROBLEMS:
    log.warning("資料問題：%s", p)
log.info("啟動：讀到 %d 件作品", len(ARTWORKS))

_searcher: search.Searcher | None = None


def get_searcher() -> search.Searcher:
    global _searcher
    if _searcher is None:
        try:
            _searcher = search.Searcher()
        except search.IndexNotReady as e:
            raise ToolError(f"{e}。建好之後再問一次就可以了。") from e
    return _searcher


# Door：⑤ 資料改了卻沒重建索引，搜尋結果會是舊的；發現這種情況就在結果裡加警告，提醒去點 build_index.bat。
def stale_note(s: search.Searcher) -> dict:
    """資料改過卻沒重建索引時，在結果裡提醒。"""
    if s.stale:
        return {
            "warning": "data/ 的資料有更新，但索引還是舊的：請雙擊 build_index.bat 重建，再完全結束並重開 Claude。"
        }
    return {}


# Door：⑥ 檢查使用者輸入的問題，空白或超過 500 字就直接駁回報錯。
def check_query(query: str) -> str:
    query = (query or "").strip()
    if not query:
        raise ToolError("query 不能是空的：請放入要查的文字，例如「谿山行旅圖的落款在哪裡」。")
    if len(query) > 500:
        raise ToolError("query 太長了（超過 500 字）：請只放要查的重點。")
    return query


# ─────────────────────────────────────────────
# MCP Server 本體
# ─────────────────────────────────────────────
# Door：⑦ 正式建立 MCP 櫃台，名字叫 art-rag，給它最高指導原則：先查資料、附出處網址與授權、不准瞎掰。
mcp = MCPServer(
    name="art-rag",
    instructions=(
        "這是藝術作品知識庫（含臺灣民間藝術），資料來自博物館開放授權典藏。"
        "回答作品相關問題時，請先用工具查資料，並在回答中附上工具回傳的出處網址與授權；"
        "工具說找不到時，請直接告訴使用者知識庫沒有這方面的資料，不要自行編造。"
    ),
)


# Door：⑧ 工具一：用描述的語意找作品，找出最多 5 件相關的「作品」。
@mcp.tool(
    description=(
        "【適用情境】當使用者透過一段描述或題材、地點等內容來尋找作品，但你不知道作品名稱或編號時使用（以作品為單位，最多回傳 5 件）。\n"
        "【禁忌情境與替代方案】\n"
        "1. 如果你已經知道確切的作品編號，請不要使用此工具，請改用 get_artwork；找到作品後想進一步看此作品的完整資料，也請用 get_artwork。\n"
        "2. 如果是要查詢文章內文的深層細節、技法或故事，請改用 search_passages。\n"
        "3. 如果只是想瀏覽全部的館藏清單，請改用 list_artworks。"
    )
)
def search_artworks(query: str, limit: int = 5) -> dict:
    with log_call("search_artworks", {"query": query, "limit": limit}) as rec:
        query = check_query(query)
        limit = max(1, min(int(limit), 10))
        s = get_searcher()
        result = s.search_artworks(query, limit=limit)
        rec["n_results"] = len(result["results"])
        if not result["found"]:
            result["message"] = (
                "知識庫裡沒有和這個描述夠相關的作品。可以用 list_artworks 看看收錄了哪些作品。"
            )
        return {**result, **stale_note(s)}


# Door：⑨ 工具二：用作品編號取得作品詳細資料；編號打錯會用 difflib 猜「你是不是要找…？」
@mcp.tool(
    description=(
        "【適用情境】當你已經擁有精確的作品編號（例如 npm-000001），需要取得該作品的完整詳細資料（名稱、作者、年代、材質、尺寸、典藏單位、每一段介紹與其出處網址，以及授權與標示文字；引用或使用圖片時要照標示文字寫）時使用。\n"
        "【禁忌情境與替代方案】\n"
        "1. 如果你不知道作品編號，請不要直接猜測呼叫，務必先使用 list_artworks 查閱目錄，或透過 search_artworks 找出正確編號。\n"
        "2. 如果是要搜尋段落文字或回答細節問題，請改用 search_passages。"
    )
)
def get_artwork(artwork_id: str) -> dict:
    with log_call("get_artwork", {"artwork_id": artwork_id}) as rec:
        art_id = (artwork_id or "").strip().lower()
        record = ARTWORKS.get(art_id)
        if record is None:
            close = difflib.get_close_matches(art_id, list(ARTWORKS), n=3, cutoff=0.5)
            hint = f"你是不是要找：{', '.join(close)}？" if close else ""
            raise ToolError(
                f"找不到編號「{artwork_id}」的作品。{hint}請先用 list_artworks 查正確的編號。"
            )
        image = record["image"]
        descriptions = []
        for d in record["descriptions"]:
            reason = search.should_skip(d)
            if reason:
                continue  # 未審核、沒有出處的段落不提供（和搜尋的規則一致）
            descriptions.append(
                {
                    "topic": d.get("topic", ""),
                    "text": d["text"],
                    "source_url": d["source_url"],
                    "license": d["license"],
                }
            )
        rec["n_results"] = 1
        return {
            "artwork_id": record["id"],
            "title": record["title"],
            "artist": record["artist"],
            "date_text": record["date_text"],
            "medium": record.get("medium", ""),
            "dimensions": record.get("dimensions", ""),
            "collection": record["collection"],
            "source_url": record["source_url"],
            "image_license": image["license"],
            "image_source_url": image.get("source_url", ""),
            "credit_line": image.get("attribution")
            or f"{record['collection']}，{image['license']}",
            "style_tags": record.get("style_tags", []),
            "descriptions": descriptions,
        }


# Door：⑩ 工具三：找出最相關的 3 張原文卡片；這個工具只負責找卡，Claude 自己閱讀理解後再回答使用者。
@mcp.tool(
    description=(
        "【適用情境】依問題找出作品資料庫（卡片庫）中最相關的原文段落（預設 3 段），當需要回答作品細節、技法、用途、題材故事、匠師或畫家生平等需要引用原文的問題時使用；請根據回傳的段落理解思考怎麼告訴使用者後回答，並附上每段的出處。\n"
        "【硬性規範】\n"
        "1. query 撰寫規則：查詢的 query 務必帶有具體的作品名稱或人名（例如「谿山行旅圖的落款」「陳天乞的生平」），切勿只打單一抽象的短關鍵字（如「落款」「技法」），否則容易因資訊不足而漏搜。\n"
        "2. found 為 false 時代表知識庫沒有相關資料，請直接說明，絕不編造。必須嚴格只根據回傳的段落內容進行回答，並在回答中附上每段的出處網址與授權；若回傳的段落中找不到相關答案，必須直接對使用者說資料裡沒寫，絕對不可自行編造。\n"
        "【禁忌情境與替代方案】\n"
        "3. 如果只想知道館內收錄了哪些作品清單，請改用 list_artworks。\n"
        "4. 如果是要找單一作品的完整規格（材質、尺寸等），請改用 get_artwork。"
    )
)
def search_passages(query: str, top_k: int = 3) -> dict:
    with log_call("search_passages", {"query": query, "top_k": top_k}) as rec:
        query = check_query(query)
        top_k = max(1, min(int(top_k), 5))
        s = get_searcher()
        result = s.search(query, top_k=top_k)
        rec["n_results"] = len(result["results"])
        if not result["found"]:
            result["message"] = (
                "知識庫裡沒有和這個問題夠相關的段落。請直接告訴使用者查不到，或用 list_artworks 看看收錄了哪些作品。"
            )
        return {**result, **stale_note(s)}


# Door：⑪ 工具四：列出所有作品的編號、名稱、作者、年代、典藏單位；需要知道有哪些作品或編號時用。
@mcp.tool(
    description=(
        "【適用情境】當使用者詢問有哪些作品、有收錄哪些館藏，或當你需要作品編號但手頭上完全沒有線索時使用，用以列出全館所有作品的編號、名稱、作者、年代與典藏單位。\n"
        "【禁忌情境與替代方案】\n"
        "1. 如果使用者已經提供具體的內容描述或搜尋條件，請改用 search_artworks。\n"
        "2. 如果是要查詢作品的詳細內文、技法或故事，請改用 search_passages。\n"
        "3. 如果已經知道特定編號想查詳細身家，請直接用 get_artwork。"
    )
)
def list_artworks() -> dict:
    with log_call("list_artworks", {}) as rec:
        items = [
            {
                "artwork_id": r["id"],
                "title": r["title"]["zh"],
                "artist": r["artist"]["zh"],
                "date_text": r["date_text"],
                "collection": r["collection"],
            }
            for r in ARTWORKS.values()
        ]
        rec["n_results"] = len(items)
        return {"count": len(items), "artworks": items}


# Door：⑫ 館藏清單（資料 artworks://catalog）不是 Claude 點單用的工具，而是貼在大廳佈告欄的公告牌，由使用者自己附進對話；程式把所有館藏排成 Markdown 表格。
@mcp.resource(
    "artworks://catalog",
    name="館藏清單",
    mime_type="text/markdown",
    description="知識庫收錄的全部作品清單（Markdown 表格）。",
)
def catalog() -> str:
    lines = [
        "# 館藏清單",
        "",
        "| 編號 | 名稱 | 作者 | 年代 | 典藏單位 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for r in ARTWORKS.values():
        lines.append(
            f"| {r['id']} | {r['title']['zh']} | {r['artist']['zh']} | {r['date_text']} | {r['collection']} |"
        )
    return "\n".join(lines)


# Door：⑬ 導覽稿範本 tour_script 由使用者自己選來用（Claude 不會自動套用）；填主題和分鐘數，就展開成「只用查到的資料、每段附出處、語氣親切」的寫稿指令。
@mcp.prompt(
    name="tour_script",
    title="導覽稿",
    description="產生一份附出處的導覽稿。topic 填主題或作品，minutes 填講幾分鐘。",
)
def tour_script(topic: str, minutes: str = "5") -> str:
    return (
        f"請幫我寫一份大約 {minutes} 分鐘的導覽稿，主題是「{topic}」。\n"
        "寫作前請先用 search_artworks 找出相關作品，再用 search_passages 或 get_artwork 取得原文。\n"
        "要求：\n"
        "1. 只使用工具查到的內容，查不到的部分直接說明，不要自行補充或編造。\n"
        "2. 每一段結尾用括號附上出處（典藏單位與出處網址）。\n"
        "3. 語氣親切、口語，適合現場解說；最後附上「資料授權」小節，列出每件作品的授權與標示文字。"
    )


# Door：⑭ 發動機開關：Claude 桌面版啟動這個檔案時，把「開始等待連線」寫進 server.log（不能用 print），再用 mcp.run() 透過 stdio 和 Claude 雙向溝通。
if __name__ == "__main__":
    log.info("MCP Server 開始等待 Claude 連線（stdio）")
    mcp.run()
