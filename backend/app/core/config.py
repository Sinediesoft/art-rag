"""所有設定集中在這裡（共用層 §四）；其他程式不直接讀環境變數。"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    # real：呼叫真的模型；mock：embedding 與生成都回固定結果（CI、沒有模型的電腦）
    llm_mode: Literal["real", "mock"] = "real"
    embed_mode: Literal["real", "mock"] = "real"
    embed_device: str = "cpu"  # 查詢 embedding 一律 CPU，各平台結果一致

    # 各策略一組 OpenAI 相容設定；model 留空就用 shared/models.yaml 的 default_model
    hybrid_base_url: str = "http://localhost:11434/v1"
    hybrid_model: str = ""
    hybrid_api_key: str = "ollama"

    # 本地備援模型：主推論伺服器失敗時改走這裡（同主機較小的模型，或 Mac 備用機）；
    # base_url 留空＝與 hybrid 同一台。全程不改走雲端（共用層 §四）
    hybrid_fallback_base_url: str = ""
    hybrid_fallback_model: str = ""
    hybrid_fallback_api_key: str = "ollama"

    # 雲端 API 只當對照組（api_nokb／api_kb）；預設關閉，正式服務不呼叫任何雲端 API
    allow_cloud: bool = False
    api_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai/"
    api_model: str = ""
    api_key: str = ""

    # 工廠圖紙 3D 重建：Ortho2CAD（llama.cpp 的 llama-server，OpenAI 相容介面；make ortho2cad 啟動）
    ortho2cad_base_url: str = "http://localhost:8081/v1"
    ortho2cad_model: str = ""
    ortho2cad_api_key: str = "none"

    lora_enabled: bool = False
    lora_base_url: str = "http://localhost:11434/v1"
    lora_model: str = ""
    lora_api_key: str = "ollama"

    # 逾時與備援（共用層 §四）
    connect_timeout_s: float = 5.0
    generate_timeout_s: float = 60.0
    retries: int = 1

    # 模型下載完成後設 true：embedding 模型只讀本機快取，不再連 Hugging Face（斷網可用）
    hf_offline: bool = False

    upload_max_mb: int = 10
    upload_ttl_days: int = 7

    # 展示用：在 /admin 模擬主推論伺服器斷線，正式上線請關閉
    demo_controls: bool = True

    data_dir: Path = REPO_ROOT / "data"

    @property
    def kb_dir(self) -> Path:
        return REPO_ROOT / "kb"

    @property
    def shared_dir(self) -> Path:
        return REPO_ROOT / "shared"

    @property
    def index_dir(self) -> Path:
        return self.data_dir / "index"

    @property
    def cad_jobs_dir(self) -> Path:
        return self.data_dir / "cad"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def frontend_dist(self) -> Path:
        return REPO_ROOT / "frontend" / "dist"


class EmbeddingSpec(BaseModel):
    name: str
    revision: str
    dim: int
    distance: str
    normalize: bool
    dtype: str
    max_length: int | None = None


class StrategySpec(BaseModel):
    label: str
    default_model: str
    cost_per_1k_input_twd: float
    cost_per_1k_output_twd: float


class ModelsConfig(BaseModel):
    embeddings: dict[str, EmbeddingSpec]
    chunking: dict[str, int]
    retrieval: dict[str, float]
    global_fill_keywords: list[str] = []
    prompt: dict[str, str]
    generation: dict[str, float]
    strategies: dict[str, StrategySpec]
    # 工廠圖紙：以圖搜圖紙的門檻、Ortho2CAD 的 prompt 與影像大小
    drawing_retrieval: dict[str, float] = {}
    cad: dict[str, str | float] = {}


@lru_cache
def get_settings() -> Settings:
    return Settings()


@lru_cache
def get_models_config() -> ModelsConfig:
    path = get_settings().shared_dir / "models.yaml"
    return ModelsConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def kb_version() -> str:
    return (get_settings().kb_dir / "VERSION").read_text(encoding="utf-8").strip()
