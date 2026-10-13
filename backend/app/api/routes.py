"""路由總成：同一個 /api/v1 入口，依領域拆成 APIRouter（TASK-20261013-001，docs/adr/036）。

| 模組 | 範圍 |
|---|---|
| artworks | 畫作圖紙：公開畫作清單、以圖／以文搜尋、色彩、藝術家區域註記 |
| parts | 工廠圖紙：清單、單筆、原圖／模型、以圖／以文找圖紙 |
| inventory | 工廠資料庫：固定查詢、Text-to-SQL（唯讀） |
| cad | 工廠 3D 重建：既有 SSE 入口與結果相容讀取 |
| production | 工廠工單排程：工單、既有 SSE 求解入口、提前停止、結果 |
| jobs | 背景工作（3D 重建、排程）：建立 202、狀態、事件、取消、產物 |
| 共用 | auth、images、chat、agent、changes（含核准）、tools、system |

路由只處理輸入輸出與授權進入點，商業邏輯在 services/，資料存取在 repositories/；
共用安全政策在 services/security.py，不在各模組各寫一份。
"""

from fastapi import APIRouter

from app.api import schemas as S
from app.api.routers import (
    agent,
    artworks,
    auth,
    cad,
    changes,
    chat,
    images,
    inventory,
    jobs,
    parts,
    production,
    system,
    tools,
)

router = APIRouter(prefix="/api/v1", responses={"4XX": {"model": S.ErrorResponse}})

# 順序決定同一路徑樣式的比對優先序：固定路徑在前、含參數的在後（每個模組內部維持原本的順序）
for module in (
    auth,
    images,
    artworks,
    chat,
    parts,
    cad,
    tools,
    inventory,
    production,
    jobs,
    system,
    agent,
    changes,
):
    router.include_router(module.router)
