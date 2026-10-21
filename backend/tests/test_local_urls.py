"""本機服務的預設位址寫 127.0.0.1，不寫 localhost。

Windows 解析 localhost 會先試 IPv6 ::1，而 Ollama、llama-server、排程服務都只聽 127.0.0.1：
每個請求多等 0.2～0.5 秒，/status 一次探測好幾個服務就累積成 5 秒以上，
前端每次載入都卡在「正在確認身分」（5070 Ti 主機實測 4968 ms → 872 ms）。
"""

import re

import pytest

from app.core.config import REPO_ROOT, Settings

LOCAL_SERVICE_URLS = (
    "hybrid_base_url",
    "ortho2cad_base_url",
    "scheduler_base_url",
    "lora_base_url",
)


@pytest.mark.parametrize("field", LOCAL_SERVICE_URLS)
def test_local_service_defaults_use_loopback_ip(field):
    default = Settings.model_fields[field].default
    assert default.startswith("http://127.0.0.1:"), f"{field} 預設是 {default}"


def test_env_example_has_no_localhost_url():
    # 新主機照 .env.example 複製出 .env，範例裡的 localhost 會原封不動帶過去；註解不算
    lines = (REPO_ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    settings = [line for line in lines if re.match(r"^[A-Z0-9_]+=", line)]
    assert [line for line in settings if "localhost" in line] == []
    for field in LOCAL_SERVICE_URLS:
        prefix = f"{field.upper()}=http://127.0.0.1:"
        assert any(line.startswith(prefix) for line in settings), field
