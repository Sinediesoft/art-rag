"""智慧助理（統一入口）：System 1 判斷意圖與信心，System 2 交給既有的本地模組執行（docs/adr/007）。

- entities：本機前處理，找出零件、倉庫、客戶、畫作、單號，送 Jev 前換成代號
- jev：TypeSafe Jev（雲端 System 1，只收代號化文字）
- local_router：本地路由（關鍵字＋bge-m3），Jev 沒金鑰或失敗時的備援
- gate：信心閘門，依動作風險分流
- extract：修改資料的參數抽取（規則＋本地 Qwen3-VL）
"""
