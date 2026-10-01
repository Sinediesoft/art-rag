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

    # 生產排程：Timefold Solver 排程服務（make scheduler 啟動，只聽本機）；
    # 連不上時改用簡易排程（交期優先派工）。mock＝一律用簡易排程（CI、沒有 Java 的電腦）
    scheduler_base_url: str = "http://localhost:8082"
    scheduler_mode: Literal["real", "mock"] = "real"

    # 記憶體管理：系統記憶體使用率超過門檻時，釋放目前流程用不到的模型（services/memory_guard.py）
    memory_guard: bool = True
    memory_high_pct: float = 80.0
    memory_check_interval_s: float = 5.0

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

    # 檢索段落篩選（MIRA 的 Rearrange）：留空＝shared/models.yaml 的 rearrange.enabled；
    # true／false 覆寫這台主機的設定（例如只在 5070 Ti 主機打開）
    rearrange: str = ""

    upload_max_mb: int = 10
    upload_ttl_days: int = 7

    # 展示用：在 /admin 模擬主推論伺服器斷線，正式上線請關閉
    demo_controls: bool = True

    data_dir: Path = REPO_ROOT / "data"

    # PostgreSQL + pgvector（Docker，見 deploy/docker-compose.yml 與 docs/adr/009）：
    # 畫作、段落、向量、manifest 與使用紀錄都存這裡。
    # 留空＝檔案索引（data/index/）＋SQLite，給沒有 Docker 的電腦
    database_url: str = ""

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


class RearrangeSpec(BaseModel):
    """檢索段落篩選（MIRA 的 Rearrange，見 docs/adr/008）"""

    enabled: bool = False
    prompt_version: str = "rearrange_v1"
    max_candidates: int = 5
    timeout_s: float = 30
    max_tokens: int = 16


class ColorAnalysisSpec(BaseModel):
    """畫作色彩分析（docs/adr/010）；改了要重建索引（manifest 比對）"""

    method: str = "lab-kmeans-v1"
    n_colors: int = 6
    fit_long_edge: int = 256  # k-means 分群用的圖（約 6 萬像素）
    map_long_edge: int = 512  # 算占比、畫色塊分布圖用的圖
    kmeans_max_iter: int = 30
    seed: int = 0
    neutral_chroma: float = 10  # C* 小於這個值算中性色（接近黑白灰）
    warm_hue_deg: list[float] = [340, 110]  # 色相角落在這段（可跨 0°）為暖色，其餘有彩色為冷色
    lightness_bands: list[float] = [30, 60]  # L*：暗調／中間調／亮調的分界
    chroma_bands: list[float] = [10, 25]  # C*：低／中／高彩度的分界


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
    # 工廠庫存 Text-to-SQL：生成上限、修正次數、回傳列數與執行逾時
    text2sql: dict[str, float] = {}
    # 生產排程（Timefold）：求解秒數、無改善提前結束秒數、進度更新間隔
    scheduling: dict[str, float] = {}
    # 領域路由：照片先判斷是畫作還是工廠圖紙（MMed-RAG 的領域辨識，見 docs/adr/007）
    router: dict[str, float] = {}
    rearrange: RearrangeSpec = RearrangeSpec()
    color_analysis: ColorAnalysisSpec = ColorAnalysisSpec()


@lru_cache
def get_settings() -> Settings:
    return Settings()


@lru_cache
def get_models_config() -> ModelsConfig:
    path = get_settings().shared_dir / "models.yaml"
    return ModelsConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def kb_version() -> str:
    return (get_settings().kb_dir / "VERSION").read_text(encoding="utf-8").strip()
