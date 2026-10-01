# ADR 001：demo 階段用本地檔案索引，正式版換 PostgreSQL + pgvector

- 日期：2026-09-29
- 狀態：暫行（正式部署前由 B 替換）→ 2026-10-01 由 ADR 009 取代：`.env` 設了 `DATABASE_URL` 就用
  PostgreSQL + pgvector（Docker）；本文的檔案模式保留給沒有 Docker 的電腦（`DATABASE_URL` 留空）

## 背景
企劃書選定 PostgreSQL 17 + pgvector。demo 主機（MacBook Air M5）尚未安裝 Docker，需要先展示完整流程。

## 決定
向量索引存在 `data/index/`（numpy + JSON + manifest），使用紀錄與回饋存在 SQLite。
存取全部封裝在 `backend/app/repositories/`，對外介面（`search_images`、`search_chunks`、`get_artwork`、
`add_chat_log`…）照正式版設計。

## 影響
- 換成 pgvector 時只改 `repositories/`，`services/`、`rag/` 與 API 不動。
- 3–5 筆資料的暴力搜尋 < 1 ms，效能不是問題。
- manifest 一致性檢查、`make index` 重建流程與正式版相同。
