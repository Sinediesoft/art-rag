"""啟動失敗的故障診斷（deploy/diagnose.py）：規則表優先、log 編碼與雜訊、遮蔽祕密、模型只連本機。"""

import importlib.util
import os
import time
import urllib.error
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("diagnose", REPO / "deploy" / "diagnose.py")
diagnose = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diagnose)

# PowerShell 5.1 把 docker 寫到 stderr 的內容包成 NativeCommandError 的樣子
PS_WRAPPED = (
    "docker :  Container artrag-redis-1 Running\r\n"
    "At C:\\art-rag\\deploy\\windows\\start-services.ps1:90 char:5\r\n"
    "+     docker compose -f deploy/docker-compose.yml --env-file .env up -d ...\r\n"
    "+     ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~\r\n"
    "    + CategoryInfo          : NotSpecified: (:String) [], RemoteException\r\n"
    "    + FullyQualifiedErrorId : NativeCommandError\r\n"
)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / ".env.example").write_text(
        "HYBRID_API_KEY=ollama\nPOSTGRES_PASSWORD=artrag\nJWT_SECRET=\n", encoding="utf-8"
    )
    (tmp_path / ".env").write_text(
        "LLM_MODE=real\nHYBRID_BASE_URL=http://127.0.0.1:11434/v1\n"
        "HYBRID_MODEL=qwen3-vl:8b-instruct\nHYBRID_FALLBACK_MODEL=qwen3-vl:4b-instruct\n"
        "HYBRID_API_KEY=ollama\nPOSTGRES_PASSWORD=artrag\nJWT_SECRET=s3cr3t-jwt-value-0123\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(diagnose, "REPO", tmp_path)
    return tmp_path


def run(repo: Path, capsys, *args: str) -> str:
    assert diagnose.main([*args, "--env", str(repo / ".env")]) == 0
    return capsys.readouterr().out


def write_log(repo: Path, name: str, text: str, encoding: str = "utf-8") -> Path:
    path = repo / name
    path.write_bytes(text.encode(encoding))
    return path


def test_utf16_powershell_log_matches_rule_without_asking_model(repo, capsys, monkeypatch):
    log = write_log(
        repo,
        "db.log",
        PS_WRAPPED
        + 'error during connect: Get "http://%2F%2F.%2Fpipe%2FdockerDesktopLinuxEngine"\r\n',
        "utf-16",
    )
    monkeypatch.setattr(diagnose, "ask_model", lambda *a: pytest.fail("規則對到就不該問模型"))
    out = run(repo, capsys, "--log", str(log))
    assert "Docker Desktop 沒開" in out
    assert "db.log：error during connect" in out


def test_powershell_noise_alone_is_not_an_error(repo, capsys, monkeypatch):
    log = write_log(repo, "db.log", PS_WRAPPED, "utf-16")
    assert diagnose.clean(diagnose.decode(log.read_bytes())) == [
        "docker :  Container artrag-redis-1 Running"
    ]
    monkeypatch.setattr(diagnose, "ask_model", lambda *a: pytest.fail("沒有錯誤就不該問模型"))
    assert "沒有找到錯誤訊息" in run(repo, capsys, "--log", str(log))


def test_message_from_script_is_matched_too(repo, capsys):
    out = run(
        repo, capsys, "--message", "找不到 ollama，請先安裝 Ollama", "--log", str(repo / "none.log")
    )
    assert "Ollama 沒裝或沒開" in out


@pytest.mark.parametrize(
    ("line", "rule_id"),
    [
        ("RuntimeError: 連不上資料庫 postgresql://***@127.0.0.1:5432/artrag", "db-unreachable"),
        ("redis.exceptions.ConnectionError: Error 10061 connecting to 127.0.0.1:6379.", "redis"),
        ("[Errno 10048] error while attempting to bind on address", "port-in-use"),
        ("Bind for 0.0.0.0:5432 failed: port is already allocated", "port-in-use"),
        ("torch.OutOfMemoryError: CUDA out of memory. Tried to allocate 2.00 GiB", "out-of-memory"),
        (
            "huggingface_hub.errors.LocalEntryNotFoundError: Cannot find the requested files",
            "hf-offline",
        ),
        ('model "qwen3-vl:8b-instruct" not found, try pulling it first', "model-missing"),
        ("unshare: write failed /proc/self/uid_map: Operation not permitted", "wsl-cadquery"),
        ("uv : The term 'uv' is not recognized as the name of a cmdlet", "uv-missing"),
        ("Error: Unable to access jarfile scheduler/target/scheduler.jar", "scheduler"),
        ("OSError: [Errno 28] No space left on device", "disk-full"),
    ],
)
def test_each_rule_matches_its_example(line, rule_id):
    found = diagnose.match_rules("", {"x.log": [line]})
    assert found and found[0][0] == rule_id


def test_logs_older_than_this_run_are_skipped(repo):
    old = write_log(repo, "old.log", "Traceback: something failed\n")
    new = write_log(repo, "new.log", "Traceback: something else failed\n")
    start = time.time()
    os.utime(old, (start - 600, start - 600))
    assert list(diagnose.read_logs([old, new], start)) == ["new.log"]


def test_unmatched_error_goes_to_local_model_with_secrets_masked(repo, capsys, monkeypatch):
    log = write_log(
        repo,
        "backend.log",
        "INFO: started\n"
        "ERROR: boom with JWT_SECRET=s3cr3t-jwt-value-0123 at postgresql://artrag:hunter22@db/x\n"
        "ERROR: container artrag-db-1 said something odd\n",
    )
    sent = {}

    def fake(base_url, model, api_key, text):
        sent.update(base_url=base_url, model=model, text=text)
        return "原因：看不出來。\n修法：重跑一次。"

    monkeypatch.setattr(diagnose, "ask_model", fake)
    out = run(repo, capsys, "--log", str(log))
    assert "模型推測，未驗證" in out and "原因：看不出來" in out
    assert sent["model"] == "qwen3-vl:8b-instruct"
    assert "s3cr3t-jwt-value-0123" not in sent["text"] and "hunter22" not in sent["text"]
    assert "artrag-db-1" in sent["text"]  # 和 .env.example 一樣的值不遮
    assert "INFO: started" not in sent["text"]  # 只送像錯誤的行


def test_falls_back_to_second_model_then_to_raw_lines(repo, capsys, monkeypatch):
    log = write_log(repo, "backend.log", "ERROR: something unusual happened\n")
    tried = []

    def down(base_url, model, api_key, text):
        tried.append(model)
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(diagnose, "ask_model", down)
    out = run(repo, capsys, "--log", str(log))
    assert tried == ["qwen3-vl:8b-instruct", "qwen3-vl:4b-instruct"]
    assert "本地模型叫不到" in out and "ERROR: something unusual happened" in out


def test_non_local_model_url_never_receives_logs(repo, capsys, monkeypatch):
    env = repo / ".env"
    env.write_text(
        env.read_text(encoding="utf-8").replace(
            "http://127.0.0.1:11434/v1", "https://api.example.com/v1"
        ),
        encoding="utf-8",
    )
    log = write_log(repo, "backend.log", "ERROR: something unusual happened\n")
    monkeypatch.setattr(diagnose, "ask_model", lambda *a: pytest.fail("不是本機位址不該送出"))
    assert "log 不送出" in run(repo, capsys, "--log", str(log))


def test_mock_profile_and_no_llm_skip_the_model(repo, capsys, monkeypatch):
    log = write_log(repo, "backend.log", "ERROR: something unusual happened\n")
    monkeypatch.setattr(diagnose, "ask_model", lambda *a: pytest.fail("不該問模型"))
    assert "加了 --no-llm" in run(repo, capsys, "--log", str(log), "--no-llm")
    env = repo / ".env"
    env.write_text(
        env.read_text(encoding="utf-8").replace("LLM_MODE=real", "LLM_MODE=mock"), encoding="utf-8"
    )
    assert "mock 方案" in run(repo, capsys, "--log", str(log))


@pytest.mark.parametrize(
    ("url", "local"),
    [
        ("http://localhost:11434/v1", True),
        ("http://127.0.0.1:8080/v1", True),
        ("http://192.168.1.20:11434/v1", True),
        ("http://100.101.102.103:11434/v1", True),
        ("http://gpu-box.ts.net:11434/v1", True),
        ("https://api.example.com/v1", False),
        ("http://8.8.8.8/v1", False),
    ],
)
def test_is_local_url(url, local):
    assert diagnose.is_local_url(url) is local
