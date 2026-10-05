# 畫語 ArtRAG — 常用指令（Windows 請在 WSL2 內執行）
PY      := uv run --project backend python
MODEL   ?= qwen3-vl:4b-instruct
# Ortho2CAD（工廠圖紙 → CadQuery）：llama.cpp 的 llama-server，make ortho2cad-setup 下載到 models/
O2C_DIR  := models/ortho2cad
O2C_PORT ?= 8081
# 生產排程：Timefold Solver 排程服務（Java 21），make scheduler-setup 建置到 scheduler/target/
SCHED_PORT ?= 8082

# 資料庫（PostgreSQL 17 + pgvector）跑在 Docker；帳號密碼讀 .env 的 POSTGRES_*
COMPOSE := docker compose -f deploy/docker-compose.yml --env-file .env

.PHONY: help setup index index-db check-kb db-up db-stop db-psql db-import-sqlite dev dev-backend dev-frontend demo demo-all build test lint openapi eval eval-cloud eval-cad eval-router eval-color eval-style eval-style-met style-head eval-versions eval-met-photos eval-align eval-intake eval-rearrange qa-multi demo-add demo-reset ci drawings ortho2cad ortho2cad-setup eval-sql eval-route eval-guard inventory demo-test scheduler scheduler-setup

help:
	@echo "make setup       安裝後端（uv）與前端（npm）套件，建立 .env"
	@echo "make db-up       啟動資料庫容器（PostgreSQL 17 + pgvector），等到可以連線才結束"
	@echo "make db-stop     停止資料庫容器（資料保留在 Docker volume）"
	@echo "make db-psql     進資料庫下 SQL"
	@echo "make db-import-sqlite  把 SQLite（data/artrag.sqlite3）裡的舊紀錄搬進資料庫（預跑的 3D 重建結果才看得到）"
	@echo "make index       驗證知識庫並重建向量索引（新增畫作後執行；.env 設了 DATABASE_URL 會一併寫進資料庫）"
	@echo "make index-db    不重算向量，把現有的 data/index/ 寫進資料庫"
	@echo "make demo        建置前端並啟動展示伺服器 http://localhost:8000"
	@echo "make dev         開發模式：後端 8000（熱重載）＋前端 5173"
	@echo "make eval        對執行中的後端跑評估，結果存 eval/runs/"
	@echo "make eval-cloud  連同雲端對照組（A1 無檢索、A2 有檢索）一起評估；需 ALLOW_CLOUD=true"
	@echo "make eval-router 領域路由評估：所有評估照片送 /search/any，看畫作／圖紙判斷與辨識是否正確"
	@echo "make eval-rearrange 檢索段落篩選（MIRA）開關對照：正確率、引用、平均段數與延遲；有標正解段落的題（跨段落、比較）另外看正解段落留下幾段"
	@echo "make qa-multi    用本地模型（Ollama）從知識庫段落出「要兩段以上才答得完整」的評估題（要先 make index）：候選題寫到 data/qa_gen/，人工看過再複製進 eval/qa.jsonl"
	@echo "make eval-intake 照片建檔評估（不用開後端、不用索引，要有 Ollama）：標題欄每個欄位讀對、留空、被規則擋下、錯了卻通過驗證的比例，糊照擋下率"
	@echo "make eval-color  色彩分析評估（不用開後端，要先 make index）：結果是否固定、照片與原圖的色差、色彩段落的檢索"
	@echo "make eval-style  畫作卡推測評估（不用開後端、不用索引）：風格大類、細分流派、題材、媒材的正確率，「像畫作」把關擋下圖紙照片"
	@echo "make eval-style-met 畫作卡推測＋大都會評估集（555 件，第一次會從 Met 開放 API 抓圖到 data/met_eval/，約 56 MB）：畫作 340 幅、雕塑陶瓷器物與老照片 215 件，門檻掃描"
	@echo "make style-head  重訓畫作卡的媒材分類頭（大都會館藏 2,010 幅，第一次會抓圖約 215 MB）：輸出 shared/style_head_v1.npz 與和零樣本的比較；換 Chinese-CLIP 後要重跑"
	@echo "make eval-versions 以圖搜圖「同系列、不同版本」評估（不用開後端、不用索引；第一次會從 Wikimedia Commons 抓 46 張圖到 data/version_eval/，連模擬照約 50 MB）：畫家的別版、習作、同系列有沒有被認成知識庫的畫，同一幅畫的實拍照認不認得出來"
	@echo "make eval-met-photos 以圖搜圖「觀眾實拍照」評估（不用開後端、不用索引；第一次會抓 The Met Dataset 的照片與標註約 40 MB、館藏圖與 Flickr 原圖（存下來約 60 MB）到 data/，沒跑過 eval-style-met／style-head 的還要抓干擾項約 270 MB；算 2,467 幅的 CLIP 向量約 15 分鐘（之後有快取）、ORB 比對約 50 分鐘）：真實手機照認不認得出來、沒收錄的畫與器物會不會被認錯、知識庫變大時第一階段找不找得到"
	@echo "make eval-align 影像對位與比對評估（不用開後端、不用索引）：畫作位置框誤差、畫作找不同、圖紙找不同、兩張照片互比的偵出率與假差異"
	@echo "make demo-add    展示用：加入第 4、5 筆畫作（早春圖、睡蓮）與第 7 張圖紙（治具定位板，含庫存與途程）並重建索引"
	@echo "make demo-reset  展示用：移除上述展示資料、清掉開立的工單、排程結果、智慧助理的異動與核准單，並重建索引"
	@echo "make demo-test   展示前測試：查圖紙 → 開立工單 → 生產排程 → Text-to-SQL 查排程，逐步顯示記憶體與釋放的模型"
	@echo "--- 工廠機械加工圖（Ortho2CAD）---"
	@echo "make ortho2cad-setup  下載 Ortho2CAD 並轉成 GGUF（約 6.2 GB；需先 brew install llama.cpp）"
	@echo "make ortho2cad   啟動 Ortho2CAD 推論伺服器 http://localhost:$(O2C_PORT)（另開一個終端機）"
	@echo "make demo-all    同時啟動 Ortho2CAD、排程服務與展示伺服器"
	@echo "make drawings    由 kb/cad/*.py 標準模型產生 kb/drawings/*.png（改了標準模型後執行）"
	@echo "make eval-cad    圖紙辨識＋3D 重建評估（需後端與 Ortho2CAD 在執行），結果存 eval/runs/*-cad.json"
	@echo "--- 工廠庫存（Text-to-SQL）---"
	@echo "make inventory   由 kb/inventory/*.json 重建庫存資料庫 data/inventory.sqlite3（後端也會自動重建）"
	@echo "make eval-sql    Text-to-SQL 評估（需後端在執行），結果存 eval/runs/*-sql.json"
	@echo "--- 生產排程（Timefold Solver）---"
	@echo "make scheduler-setup  建置排程服務（需 Java 21：brew install openjdk@21；Maven 會自動下載）"
	@echo "make scheduler   啟動排程服務 http://localhost:$(SCHED_PORT)（另開一個終端機；沒啟動時排程頁改用簡易排程）"
	@echo "--- 智慧助理（System 1：Jev／本地路由）---"
	@echo "make eval-route  智慧助理本地分流的意圖正確率＋第 2 段誤擋、誤短路（不是 eval-router 的領域路由；需後端在執行；沒金鑰只跑地端規則）"
	@echo "make eval-guard  七段權限控管第 2 段：Jev Choice 與地端規則對 eval/guard_qa.jsonl 的攔截率、誤擋率、閒聊短路率（需後端在執行）"
	@echo "make ci          CI 會跑的檢查：知識庫、lint、型別、單元測試、openapi 同步"

# --compile-bytecode：CadQuery 在 sandbox-exec 裡不能寫 .pyc，沒預先編譯時每次 import 要 14 秒，
# make index 同時跑 6 個標準模型會超過 60 秒逾時
setup:
	cd backend && uv sync --compile-bytecode
	cd frontend && npm ci
	@test -f .env || cp .env.example .env
	@command -v ollama >/dev/null && ollama pull $(MODEL) || echo "（未安裝 Ollama：可在 .env 設 LLM_MODE=mock）"

index:
	$(PY) pipelines/build_index.py

index-db:
	$(PY) pipelines/build_index.py --db-only

# ---- 資料庫（docs/adr/009）----
db-up:
	$(COMPOSE) up -d --wait db

db-stop:
	$(COMPOSE) stop db

db-psql:
	$(COMPOSE) exec db sh -c 'psql -U "$$POSTGRES_USER" -d "$$POSTGRES_DB"'

db-import-sqlite:
	$(PY) pipelines/import_sqlite_logs.py

check-kb:
	$(PY) pipelines/build_index.py --check
	$(PY) pipelines/build_inventory.py --check

inventory:
	$(PY) pipelines/build_inventory.py

dev:
	$(MAKE) -j2 dev-backend dev-frontend

dev-backend:
	cd backend && uv run uvicorn app.main:app --reload --port 8000

dev-frontend:
	cd frontend && npm run dev

build:
	cd frontend && npm run build

demo: build
	cd backend && uv run uvicorn app.main:app --host 0.0.0.0 --port 8000

demo-all:
	$(MAKE) -j3 ortho2cad scheduler demo

# ---- 工廠機械加工圖：Ortho2CAD（Qwen3-VL-8B 微調，三視圖 → CadQuery）----
ortho2cad-setup:
	@command -v llama-server >/dev/null || (echo "請先安裝 llama.cpp：brew install llama.cpp" && exit 1)
	$(PY) pipelines/setup_ortho2cad.py

# router 模式：模型參數在 deploy/llama-router.ini（圖片 token 上限、context、KV cache 型別），
# 第一次 3D 重建時自動載入；記憶體超過門檻時後端可呼叫 /models/unload 卸載，下次用到再自動載入
ortho2cad:
	@test -f $(O2C_DIR)/mmproj-ortho2cad-f16.gguf || (echo "找不到模型，請先執行 make ortho2cad-setup" && exit 1)
	llama-server --models-preset deploy/llama-router.ini --models-max 1 \
		--host 127.0.0.1 --port $(O2C_PORT) --no-webui

# ---- 生產排程：Timefold Solver（Java 21）----
scheduler-setup:
	$(PY) pipelines/setup_scheduler.py

# SerialGC＋heap 上限 768 MB：排程問題很小，閒置時 GC 會把 heap 還給作業系統
scheduler:
	@test -f scheduler/target/scheduler.jar || (echo "找不到排程服務，請先執行 make scheduler-setup" && exit 1)
	"$$(cat scheduler/.java-home)/bin/java" -XX:+UseSerialGC -Xms32m -Xmx768m \
		-XX:MinHeapFreeRatio=10 -XX:MaxHeapFreeRatio=30 -jar scheduler/target/scheduler.jar $(SCHED_PORT)

drawings:
	$(PY) pipelines/make_drawings.py
	$(PY) pipelines/make_drawings.py --dir kb_staging

eval-cad:
	$(PY) eval/run_cad_eval.py --baseline

eval-sql:
	$(PY) eval/run_sql_eval.py

eval-route:
	$(PY) eval/run_route_eval.py

eval-guard:
	$(PY) eval/run_guard_eval.py

test:
	cd backend && uv run pytest -q

lint:
	cd backend && uv run ruff check .. && uv run ruff format --check ..
	cd frontend && npm run typecheck

openapi:
	cd backend && uv run python -m app.export_openapi
	cd frontend && npm run gen:api

eval:
	$(PY) eval/run_eval.py --strategies hybrid,hybrid_norag

eval-cloud:
	$(PY) eval/run_eval.py --strategies hybrid,hybrid_norag,api_nokb,api_kb

eval-router:
	$(PY) eval/run_router_eval.py

# 色彩分析（docs/adr/010）：在程序內執行，不用開後端
eval-color:
	$(PY) eval/run_color_eval.py

# 畫作卡推測（docs/adr/018）：在程序內執行，不用開後端、不用索引；正解在 eval/style_truth.json
eval-style:
	$(PY) eval/run_style_eval.py

# 加跑大都會評估集（eval/met_set.json，Q-2026-10-04-04）：缺的圖從 Met 開放 API（CC0）補抓到 data/met_eval/
# 要換一批或改正解的對應規則：$(PY) eval/make_met_set.py（重抽）／--relabel（只重算正解）
eval-style-met:
	$(PY) eval/run_style_eval.py --met

# 畫作卡推測的線性分類頭（docs/adr/018「線性分類頭」）：eval/met_train.json（大都會館藏 2,010 幅，和評估集不重疊）
# → shared/style_head_v1.npz＋eval/runs/*-style-head.json（和零樣本的比較）；缺的圖從 Met 補抓。換 Chinese-CLIP 要重跑
style-head:
	$(PY) pipelines/train_style_head.py

# 以圖搜圖「同系列、不同版本」（docs/adr/002，Q-2026-10-04-05 第 2 項）：清單與正解在 eval/version_set.json，
# 缺的圖從 Wikimedia Commons 補抓到 data/version_eval/（不 commit）。改 image_threshold、verify_min_inliers 時要跑
eval-versions:
	$(PY) eval/run_version_eval.py

# 以圖搜圖「觀眾實拍照」（docs/adr/002，Q-2026-10-04-06）：The Met Dataset 的 Met queries（觀眾在大都會拍的照片）。
# 清單與正解在 eval/met_photo_set.json（重做：$(PY) eval/make_met_photo_set.py）；照片、館藏圖、向量都在 data/（不 commit）。
# 改 image_threshold、verify_min_inliers、verify_top_n，或知識庫變大時要跑
eval-met-photos:
	$(PY) eval/run_met_photo_eval.py

# 影像對位與比對（docs/adr/012）：在程序內執行，不用開後端；照片由 eval/make_align_photos.py 產生（已附在 repo）
eval-align:
	$(PY) eval/run_align_eval.py

# 照片建檔（docs/adr/013）：在程序內執行，不用開後端；要有本地生成端（Ollama）。--read-rejected 連被擋下的照片也讀
eval-intake:
	$(PY) eval/run_intake_eval.py

# 檢索段落篩選（MIRA 的 Rearrange）開關對照：同一批題目各跑一次，比正確率、引用、段數與延遲
eval-rearrange:
	$(PY) eval/run_eval.py --strategies hybrid_plain,hybrid_rearrange

# 自動出評估題（Kaggle LLM Science Exam 的出題方式，Q-2026-10-05-05）：同一幅畫的兩段（跨段落題）、
# 兩幅畫同一類的段落（比較題），程式先檢查關鍵字，合格的仍要人工看過。只出還沒收進 qa.jsonl 的段落組合
qa-multi:
	$(PY) eval/make_qa_multi.py

# ---- 展示：現場新增畫作（只加 JSON 與圖片、執行一個指令，不改程式）----
demo-add:
	cp kb_staging/artworks/*.json kb/artworks/
	cp kb_staging/images/*.jpg kb/images/
	cp kb_staging/parts/*.json kb/parts/
	cp kb_staging/cad/*.py kb/cad/
	cp kb_staging/drawings/*.png kb/drawings/
	cp kb_staging/inventory/items/*.json kb/inventory/items/
	cp kb_staging/production/routings/*.json kb/production/routings/
	$(PY) pipelines/bump_version.py
	$(PY) pipelines/build_index.py

demo-reset:
	cd kb_staging/artworks && for f in *.json; do rm -f ../../kb/artworks/$$f ../../kb/images/$${f%.json}.jpg; done
	cd kb_staging/parts && for f in *.json; do rm -f ../../kb/parts/$$f ../../kb/cad/$${f%.json}.py ../../kb/drawings/$${f%.json}.png ../../kb/inventory/items/$$f ../../kb/production/routings/$$f; done
	$(PY) pipelines/reset_production.py
	$(PY) pipelines/bump_version.py
	$(PY) pipelines/build_index.py

# 展示前測試（需後端在執行；排程服務、Ortho2CAD 沒啟動也能跑，會標示略過）
demo-test:
	$(PY) eval/run_demo_test.py

ci: check-kb lint test
	cd backend && uv run python -m app.export_openapi --check
	cd frontend && npm run gen:api && git diff --exit-code src/api/schema.d.ts
