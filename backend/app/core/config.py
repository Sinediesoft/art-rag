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

    # 智慧助理的 System 1（意圖判斷）：TypeSafe Jev（雲端，只收代號化文字，docs/adr/011）。
    # 金鑰留空、JEV_ENABLED=false 或呼叫失敗時一律改走本地路由（關鍵字＋bge-m3），其他功能照常
    jev_enabled: bool = True
    jev_base_url: str = "https://api.typesafe.ai/v1"
    jev_model: str = "jev-1.13.0"  # 文件建議固定版本，換版要重跑 make eval-route
    jev_api_key: str = ""
    jev_timeout_s: float = 1.5

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
    # true／false 覆寫這台主機的設定（例如沒有 GPU 的電腦關掉）
    rearrange: str = ""

    upload_max_mb: int = 10
    upload_ttl_days: int = 7

    # 展示用：在 /admin 模擬主推論伺服器斷線，正式上線請關閉
    demo_controls: bool = True

    # 身分憑證（JWT，HS256，docs/adr/015 第 1 段）：留空＝每次啟動隨機產生
    # （後端重啟後舊憑證全部失效，前端自動改回訪客）；多台後端共用時要填同一組
    jwt_secret: str = ""
    jwt_ttl_min: int = 480

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
    def intake_dir(self) -> Path:
        """照片建檔的草稿（docs/adr/013）：照片、拉正後的圖紙、欄位；和上傳照片同一個保存期限。"""
        return self.data_dir / "intake"

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


class CompareSpec(BaseModel):
    """影像對位與比對（docs/adr/012），一個領域一份。不影響索引，改了不用重建。"""

    # none：只標位置；ink：拉正後比線條（圖紙）；tone：照片比照片，比形狀與顏色（畫作）
    diff: Literal["none", "ink", "tone"] = "none"
    tolerance_px: int = 3  # 線條差幾 px 以內算同一條（照片拉正後的誤差）
    faint_ink_c: int = 8  # 判「缺少」時照片線條的門檻（比周圍暗多少就算有線），比辨識用的 20 寬鬆
    refine_max_shift_px: int = 15  # 每格視圖各自微調位置（只平移、旋轉）：最多平移幾 px
    refine_max_linear: float = 0.03  # 微調最多旋轉多少（sin θ；0.03 約 1.7°）
    min_region_px: int = 30  # 一處差異至少要有這麼多像素，太小的當雜訊
    merge_px: int = 9  # 差異像素先膨脹這麼多再找連通區塊
    merge_gap_px: int = 24  # 區塊之間距離在這以內併成一處
    # 參考圖最外圈不比（ink：圖紙原始大小，照片裡的紙張邊緣拉正後落在這裡；
    # tone：工作大小，整幅掛牆拍時畫的邊緣混到牆面）
    edge_margin_px: int = 12
    max_changed_ratio: float = 0.25  # 差異超過這個比例 → 整體變了（ink：圖紙線條；tone：比對範圍）
    # 以下只給 tone（畫作：觀眾照片 vs 原圖、兩張照片互比）用
    work_long_edge: int = 512  # 在這個大小比：太大會被照片雜訊、筆觸的細微錯位干擾
    shape_threshold: float = 0.18  # (1 − SSIM) / 2 高於這個值算形狀不同
    color_threshold: float = 8.0  # 整體色彩拉齊之後的 Lab 色差（ΔE76）高於這個值算顏色不同
    min_region_frac: float = 0.001  # 一處差異至少占比對範圍的比例


class ImageCompareSpec(BaseModel):
    # 觀眾照片 vs 知識庫原圖：拍照的光線和數位原圖不同，顏色門檻比兩張照片互比高
    art: CompareSpec = CompareSpec(
        diff="tone", color_threshold=12.0, edge_margin_px=10, max_changed_ratio=0.3
    )
    mfg: CompareSpec = CompareSpec(diff="ink")
    # 兩張照片互比（畫作）
    pair: CompareSpec = CompareSpec(diff="tone", edge_margin_px=10, max_changed_ratio=0.3)


class IntakeFieldSpec(BaseModel):
    """照片建檔的一個欄位：從照片讀（read）或由人填；格式不對就標紅、不能收錄。"""

    label: str
    hint: str | None = None  # 給模型的位置說明（只有 read 的欄位用）；人填的欄位當輸入提示
    read: bool = True  # 從照片讀；false＝照片上沒有，由人填
    required: bool = True
    kind: Literal["text", "number", "enum", "url", "longtext"] = "text"
    pattern: str | None = None  # 編號格式（正規表示式，整串比對）
    min: float | None = None
    max: float | None = None
    group: str | None = None  # 表單分區（畫作的跳出表單）


class IntakeDomainSpec(BaseModel):
    id_prefix: str
    fields: dict[str, IntakeFieldSpec]


class IntakeSpec(BaseModel):
    """照片建檔（docs/adr/013）。不影響索引，改了不用重建。"""

    prompt_version: str = "intake_v1"
    max_tokens: int = 300
    # 模糊程度（analysis/page.blur_score，0 清楚～1 模糊）高於這個值請使用者重拍、不送模型；
    # 圖紙與畫作共用
    max_blur: float = 0.30
    # 知識庫圖紙的版面（800×800 三視圖＋170 px 標題欄）；找到的紙張長寬比差太多就請重拍
    page_size: list[int] = [800, 970]
    page_aspect_tol: float = 0.08
    mfg: IntakeDomainSpec | None = None
    art: IntakeDomainSpec | None = None


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
    image_compare: ImageCompareSpec = ImageCompareSpec()
    intake: IntakeSpec = IntakeSpec()


@lru_cache
def get_settings() -> Settings:
    return Settings()


@lru_cache
def get_models_config() -> ModelsConfig:
    path = get_settings().shared_dir / "models.yaml"
    return ModelsConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


@lru_cache
def get_agent_config() -> dict:
    """智慧助理的路由設定：意圖、門檻、關鍵字、範例句、別名（shared/agent.yaml）。"""
    path = get_settings().shared_dir / "agent.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@lru_cache
def get_access_config() -> dict:
    """展示帳號、角色權限、硬性上限與核准額度（shared/access.yaml）。"""
    path = get_settings().shared_dir / "access.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def kb_version() -> str:
    return (get_settings().kb_dir / "VERSION").read_text(encoding="utf-8").strip()
