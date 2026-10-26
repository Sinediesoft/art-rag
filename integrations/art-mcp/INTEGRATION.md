# INTEGRATION.md — art-mcp 與 ArtRAG 小組系統的整合說明

> **給人看（三行）**：這個合併請求加了 20 件臺灣民間藝術資料、Door 的 MCP 模組、一個說明頁。合併後網站多一個側欄連結，其他功能不變。後續要做什麼，請把這份檔案交給你的 AI。
>
> **以下給 AI 助理讀。** 請先讀完整份再動手；遇到「不要做」的項目，先問 Door 或組長。

## 0. Context

- Repo: `Sinediesoft/art-rag`, base branch `dev`. This PR comes from branch `data/taiwan-folk-art` (author: Door).
- `integrations/art-mcp/` is a self-contained **MCP server** (Python, `mcp==2.3.0`, stdio transport) that lets AI assistants (Claude Desktop) query the art knowledge base. It is **not** imported by `backend/` and is **not** part of the web app runtime.
- Data is shared: when `integrations/art-mcp/data/artworks/` does not exist (it is gitignored by the root `.gitignore` rule `data/`), `search.py` reads `<repo>/kb/artworks/` (see `_OWN_DATA` / `_GROUP_KB` in `search.py`). Override with env `ARTMCP_DATA_DIR`.

## 1. What this PR changes (complete list)

| Path | Change | Runtime impact |
| --- | --- | --- |
| `kb/artworks/{nmth-*,tcmb-*}.json` (20) + `kb/images/*.jpg` (20) | New artworks (12 版印 from 國立臺灣歷史博物館 CC BY 4.0, 8 交趾陶/剪黏 from 國家文化記憶庫 CC BY 3.0 TW+) | Validated by `make check-kb`. Not in the search index until `make index` is re-run (see §3). |
| `kb/VERSION` | `2026.10.2` → `2026.10.3` (kb content changed; same convention as ADR 013/029) | `/health` reports the new version; index built from old kb will report mismatch until re-indexed. |
| `integrations/art-mcp/**` | MCP server, search, eval data & results, Windows .bat scripts, docs | None for backend/frontend. Has its own `.gitignore` (`.venv/ models/ index/ logs/`), `.gitattributes` (`*.bat eol=crlf`), and `ruff.toml` (extends root config, only relaxes E501/E731/B905/SIM115/E741). |
| `frontend/public/mcp.html` | New static page (no API calls, no auth), same pattern as `developer-workbench.html` | Served as `/mcp.html`. |
| `frontend/src/components/shell/Sidebar.tsx` | Adds `MCP_URL = "/mcp.html"` and an "AI 助理接口（MCP）↗" link (expanded + collapsed sidebar), visible to **all** roles, opens in new tab | Only UI addition. |

Verified before PR (in a clean clone of `dev` + this branch): `ruff check ..` and `ruff format --check ..` (ruff 0.16.9, from `backend/`) pass; `npm run typecheck`, `npx vitest run` (13 files / 252 tests), `npm run build` pass. **Not run locally**: `make test` (backend, needs torch/Postgres/Redis) and `make check-kb` — rely on GitHub CI for these. The 20 records previously passed `validate_kb()` on dev `04b5704`.

## 2. Integration layers (agreed plan)

| Layer | Meaning | Status after merge | Who |
| --- | --- | --- | --- |
| 1 Data | 20 artworks in `kb/` → web search, artwork pages, Q&A work for them | Data merged; **needs `make index`** on the demo host | teammate with the demo host |
| 2 MCP → backend ("connect mode") | MCP forwards queries to backend `/api/v1` instead of its own index | **Not implemented** in this PR (stand-alone mode only) | future, Door |
| 3 Start with system | MCP launched with the stack | **Not needed** for stdio (Claude Desktop launches it). Only relevant if layer 2 uses a network transport | future |
| 4 Frontend | `/mcp.html` + sidebar link | Done in this PR | — |
| 5 Web chat uses MCP | backend chat calls MCP tools | **Out of scope** (would touch the 7-step permission checks) | do not do |

## 3. After merge — required steps (demo host)

1. `make check-kb` → expect 23 artworks pass, 0 errors.
2. `make index` (runs `pipelines/build_index.py`; rebuilds `data/index/` with the 20 new artworks; if the stack uses Postgres, also `make index-db`).
3. Restart backend. Open the web app → search "交趾陶" or "團鶴版印彩紙" → new artworks appear.
4. Optional regression check: `make eval` before/after to confirm existing numbers did not change (new artworks only add items).

## 4. Running the MCP server (Windows, stand-alone mode)

From `integrations/art-mcp/` (path must be ASCII-only, e.g. `C:\dev\art-rag\integrations\art-mcp`):

1. `setup.bat` → creates `.venv` (Python 3.12 via uv) and installs `requirements.lock.txt`.
2. `build_index.bat` → downloads `BAAI/bge-small-zh-v1.5` into `models/` and builds `index/` from `kb/artworks/` (23 artworks, ~97 chunks).
3. `self_check.bat` → expect `27/27`.
4. Merge into Claude Desktop config `mcpServers` (Store build path: `%LOCALAPPDATA%\Claude-Data\claude_*\claude_desktop_config.json`; **merge, never overwrite** — the file contains other keys):
   ```json
   "art-rag": {
     "command": "C:\\dev\\art-rag\\integrations\\art-mcp\\.venv\\Scripts\\python.exe",
     "args": ["C:\\dev\\art-rag\\integrations\\art-mcp\\server.py"],
     "env": { "PYTHONUTF8": "1", "HF_HUB_OFFLINE": "1" }
   }
   ```
5. Fully quit Claude Desktop (system tray), reopen. Verify: `logs/server.log` has a new `啟動` line.

Linux/cloud test: `ARTMCP_FAKE_EMBED=1 ARTMCP_MIN_SCORE=0.15 python build_index.py && python self_check.py` (fake vectors, logic only; drop `pywin32` from the lock file).

## 5. Contracts (do not break)

- Tool names and parameters: `search_passages(query, top_k=3)`, `search_artworks(query, limit=5)`, `get_artwork(artwork_id)`, `list_artworks()`; resource `artworks://catalog`; prompt `tour_script(topic, minutes)`. Eval results in `eval/results/` depend on these names.
- Tool descriptions in `server.py` were authored by Door (stage 5); changing them changes AI tool selection — re-run the eval if changed.
- **Never `print()` to stdout in `server.py`/`search.py`** (stdio transport); use `logging` (writes `logs/server.log`).
- Artwork JSON must keep `source_url`, `license`, `attribution` per description; tools return them for citation.
- `kb/artworks` schema is owned by the group (`validate_kb`); the MCP only reads it.

## 6. Do not do without asking Door / 組長

- Do not move MCP code into `backend/` or call it from the web chat (layer 5).
- Do not edit tool descriptions, search thresholds (`MIN_SCORE=0.60`, `KEYWORD_BONUS`, `NEAR_DUP=0.97`) or `eval/` data — they are measured results.
- Do not commit `.venv/`, `models/`, `index/`, `logs/` (contain local paths / large files).

## 7. Known limitations (from stage-6 failure analysis)

- Long passages dilute person names: query "江清露的生平" scores 0.565 (< 0.60 threshold) though `tcmb-292510` mentions him; adding the temple name scores 0.869. Fix idea: smaller chunks, or bypass threshold on exact name match.
- Narrative questions ("我去褒忠祠拜拜看到…") sometimes skip tools. Fix idea: add temple/onsite scenarios to `search_passages` description.
- Only stand-alone mode; connect mode (layer 2) pending demo host networking (Tailscale port 8000, or run MCP on host).

## 8. Where to read more

- `README.md` (this folder): goals, architecture, full tech list, evaluation, failure analysis.
- `eval/README_評估怎麼做.md`, `eval/results/score_summary.md`, `eval/results/report.html`.
- `docs/資料抓取紀錄.md`: data provenance.
