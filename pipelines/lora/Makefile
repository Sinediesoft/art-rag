# LoRA 不檢索對照組（docs/adr/043）：在 Apple Silicon 的 Mac 上訓練與轉檔，不進 CI。
# 用法：make -C pipelines/lora <目標>。指令都在 repo 根目錄執行，模型與資料放 models/lora、data/lora（不進 Git）。
#
# 完整流程（訓練前關掉 Ollama、Ortho2CAD 與後端；Air 沒有風扇，要接電源）：
#   make -C pipelines/lora setup base      # 建環境、下載底模（4-bit MLX）與官方 mmproj
#   make -C pipelines/lora export-base serve   # 另開終端機；make lora-data 的 retain 題答案可用 art-base
#   make lora-data                         # repo 根目錄，產生 data/lora/（之後由組員 D 抽查 review.jsonl）
#   make -C pipelines/lora speed           # 先跑 20 步量速度，估總時間
#   make -C pipelines/lora train select export-lora info
#   make -C pipelines/lora serve           # 三組評估都接這個 :8081
#   make eval-lora                         # repo 根目錄，環境變數見 README「LoRA 不檢索對照組」
#
# 跑通流程（10 筆資料：訓練 → fuse → GGUF → llama-server 回答）：make -C pipelines/lora smoke

ROOT := $(abspath ../..)
PY := cd "$(ROOT)" && uv run --project pipelines/lora python
MODELS := $(ROOT)/models/lora
RUN ?= run1
EPOCHS ?= 4
PORT ?= 8081

# 底模：mlx-community 以 mlx-vlm 量化的版本（語言層 affine 4-bit、group 64，視覺層保留原精度）
MLX_REPO := mlx-community/Qwen3-VL-4B-Instruct-4bit
MLX_REV := 2fd8dacbdb8f1e54b8c005f081ec5bf79c56376b
# 影像編碼器：Qwen 官方 GGUF 的 mmproj；設定檔、tokenizer 與權重名稱表取自官方原版
GGUF_REPO := Qwen/Qwen3-VL-4B-Instruct-GGUF
GGUF_REV := 1cd86afb9a95c410a6038ab3b40d8b578c892266
HF_REPO := Qwen/Qwen3-VL-4B-Instruct
HF_REV := ebb281ec70b05090aa6165b016eac8ec08e71b17
MLX_FILES := added_tokens.json chat_template.jinja chat_template.json config.json generation_config.json \
	merges.txt model.safetensors model.safetensors.index.json preprocessor_config.json \
	special_tokens_map.json tokenizer.json tokenizer_config.json video_preprocessor_config.json vocab.json
HF_FILES := chat_template.json config.json generation_config.json merges.txt model.safetensors.index.json \
	preprocessor_config.json tokenizer.json tokenizer_config.json video_preprocessor_config.json vocab.json

# 可續傳的下載：網路慢或中斷時重跑即可
fetch = mkdir -p $(dir $(2)) && test -f $(2) || { until curl -sSL -C - --speed-limit 20000 --speed-time 60 \
	-o $(2).part https://huggingface.co/$(1); do sleep 5; done; mv $(2).part $(2); }

.PHONY: setup base export-base export-lora speed train select info serve smoke

setup:
	cd "$(ROOT)/pipelines/lora" && uv sync

base:
	@$(foreach f,$(MLX_FILES),$(call fetch,$(MLX_REPO)/resolve/$(MLX_REV)/$(f),$(MODELS)/mlx/Qwen3-VL-4B-Instruct-4bit/$(f));)
	@$(foreach f,$(HF_FILES),$(call fetch,$(HF_REPO)/resolve/$(HF_REV)/$(f),$(MODELS)/hf/Qwen3-VL-4B-Instruct/$(f));)
	@$(call fetch,$(GGUF_REPO)/resolve/$(GGUF_REV)/mmproj-Qwen3VL-4B-Instruct-F16.gguf,$(MODELS)/gguf/mmproj-Qwen3VL-4B-Instruct-F16.gguf)
	@echo "底模與 mmproj 就緒：$(MODELS)"

export-base:
	$(PY) pipelines/lora/export.py --name art-base --base-only

speed:
	$(PY) pipelines/lora/train.py --out models/lora/adapters/speed --iters 20

train:
	$(PY) pipelines/lora/train.py --out models/lora/adapters/$(RUN) --epochs $(EPOCHS)

select:
	$(PY) pipelines/lora/select_epoch.py --adapters models/lora/adapters/$(RUN)

export-lora:
	$(PY) pipelines/lora/export.py --name art-lora --adapter models/lora/adapters/$(RUN)/chosen

# 真實模型證據（docs/adr/043）：三個 GGUF 的 SHA-256、llama-server 版本、目前的 commit
info:
	cd "$(ROOT)" && { echo "git $$(git rev-parse HEAD)"; llama-server --version 2>&1 | grep -E "^version|built with"; \
		shasum -a 256 models/lora/gguf/art-base-q4_k_m.gguf models/lora/gguf/art-lora-q4_k_m.gguf \
		models/lora/gguf/mmproj-Qwen3VL-4B-Instruct-F16.gguf; } | tee models/lora/model_info.txt

serve:
	cd "$(ROOT)" && llama-server --models-preset deploy/llama-lora.ini --models-max 2 \
		--host 127.0.0.1 --port $(PORT) --no-webui

# 10 筆資料走完整條流程，最後用 llama-server 回答一題（另開的 :8091，連 keep_alive 一起送）
smoke:
	cd "$(ROOT)" && uv run --project backend python pipelines/make_lora_data.py --limit 10 --out data/lora-smoke
	$(PY) pipelines/lora/train.py --data data/lora-smoke --out models/lora/adapters/smoke --iters 20
	$(PY) pipelines/lora/export.py --name art-lora-smoke --adapter models/lora/adapters/smoke
	cd "$(ROOT)" && { llama-server -m models/lora/gguf/art-lora-smoke-q4_k_m.gguf \
		--mmproj models/lora/gguf/mmproj-Qwen3VL-4B-Instruct-F16.gguf -c 8192 -fa on \
		--host 127.0.0.1 --port 8091 --alias art-lora-smoke --no-webui > models/lora/smoke-server.log 2>&1 & \
		pid=$$!; until curl -sf http://127.0.0.1:8091/health >/dev/null; do sleep 2; done; \
		uv run --project pipelines/lora python pipelines/lora/ask.py --url http://127.0.0.1:8091/v1 \
			--model art-lora-smoke --file data/lora-smoke/select.jsonl --keep-alive 60m; \
		status=$$?; kill $$pid; exit $$status; }
