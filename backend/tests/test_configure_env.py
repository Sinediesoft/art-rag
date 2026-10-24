"""一鍵安裝的 .env 設定（deploy/configure_env.py）：只改還是預設值的設定，使用者改過的保留。"""

import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("configure_env", REPO / "deploy" / "configure_env.py")
configure_env = importlib.util.module_from_spec(spec)
spec.loader.exec_module(configure_env)

EXAMPLE = (
    "LLM_MODE=real\nHYBRID_MODEL=\nDATABASE_URL=postgresql://a@127.0.0.1/x\n# 註解\nSEND_IMAGE=\n"
)


def run(tmp_path: Path, env: str | None, *args: str) -> str:
    path = tmp_path / ".env"
    if env is not None:
        path.write_text(env, encoding="utf-8")
    example = tmp_path / ".env.example"
    example.write_text(EXAMPLE, encoding="utf-8")
    original = configure_env.REPO
    configure_env.REPO = tmp_path
    try:
        assert configure_env.main([*args, "--env", str(path)]) == 0
    finally:
        configure_env.REPO = original
    return path.read_text(encoding="utf-8")


def test_new_env_is_copied_from_example_and_gets_the_profile(tmp_path):
    out = run(tmp_path, None, "--profile", "full")
    values = configure_env.parse_env(out)
    assert values["HYBRID_MODEL"] == "qwen3-vl:8b-instruct"
    assert values["HYBRID_FALLBACK_MODEL"] == "qwen3-vl:4b-instruct"  # 範本沒有的 key 加在最後
    assert "# 註解" in out  # 註解保留


def test_user_changed_values_are_kept(tmp_path):
    env = EXAMPLE.replace("HYBRID_MODEL=", "HYBRID_MODEL=my-own-model").replace(
        "DATABASE_URL=postgresql://a@127.0.0.1/x", "DATABASE_URL=postgresql://me@10.0.0.2/db"
    )
    out = configure_env.parse_env(run(tmp_path, env, "--profile", "light", "--no-docker"))
    assert out["HYBRID_MODEL"] == "my-own-model"
    assert out["DATABASE_URL"] == "postgresql://me@10.0.0.2/db"


def test_no_docker_small_gpu_and_mock(tmp_path):
    out = configure_env.parse_env(
        run(tmp_path, EXAMPLE, "--profile", "light", "--no-docker", "--small-gpu")
    )
    assert out["DATABASE_URL"] == "" and out["SEND_IMAGE"] == "false"
    out = configure_env.parse_env(run(tmp_path, EXAMPLE, "--profile", "mock"))
    assert out["LLM_MODE"] == "mock" and out["EMBED_MODE"] == "mock"


def test_explicit_set_always_applies_and_dry_run_writes_nothing(tmp_path):
    out = configure_env.parse_env(
        run(tmp_path, "LLM_MODE=mock\n", "--profile", "full", "--set", "LLM_MODE=real")
    )
    assert out["LLM_MODE"] == "real"
    before = "HYBRID_MODEL=\n"
    assert run(tmp_path, before, "--profile", "full", "--dry-run") == before
