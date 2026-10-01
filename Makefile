# 畫語 ArtRAG — 常用指令（Windows 請在 WSL2 內執行）
PY      := uv run --project backend python
MODEL   ?= qwen3-vl:4b-instruct
# Ortho2CAD（工廠圖紙 → CadQuery）：llama.cpp 的 llama-server，make ortho2cad-setup 下載到 models/
O2C_DIR  := models/ortho2cad
O2C_PORT ?= 8081
# 生產排程：Timefold Solver 排程服務（Java 21），make scheduler-setup 建置到 scheduler/target/
SCHED_PORT ?= 8082

.PHONY: help setup index check-kb dev dev-backend dev-frontend demo demo-all build test lint openapi eval eval-cloud eval-cad eval-sql inventory demo-add demo-reset demo-test ci drawings ortho2cad ortho2cad-setup scheduler scheduler-setup

help:
	@echo "make setup       安裝後端（uv）與前端（npm）套件，建立 .env"
	@echo "make index       驗證知識庫並重建向量索引（新增畫作後執行）"
	@echo "make demo        建置前端並啟動展示伺服器 http://localhost:8000"
	@echo "make dev         開發模式：後端 8000（熱重載）＋前端 5173"
	@echo "make eval        對執行中的後端跑評估，結果存 eval/runs/"
	@echo "make eval-cloud  連同雲端對照組（A1 無檢索、A2 有檢索）一起評估；需 ALLOW_CLOUD=true"
	@echo "make demo-add    展示用：加入第 4、5 筆畫作（早春圖、睡蓮）與第 7 張圖紙（治具定位板，含庫存與途程）並重建索引"
	@echo "make demo-reset  展示用：移除上述展示資料、清掉圖紙頁開立的工單與排程結果，並重建索引"
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
