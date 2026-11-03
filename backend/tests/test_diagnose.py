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
        (
            "Network connectivity is disabled, but the requested data wasn't found in the cache",
            "offline-missing",
        ),
        ("Error code: Wsl/Service/CreateInstance/HCS_E_HYPERV_NOT_INSTALLED", "wsl-unavailable"),
        ("Error code: Wsl/Service/WSL_E_DISTRO_NOT_FOUND", "wsl-distro-missing"),
        (
            "error: Project virtual environment directory `D:\\LaiWen\\app\\backend\\.venv` cannot "
            "be used because it is not a valid Python environment (no Python executable was found)",
            "venv-broken",
        ),
        (
            'connection to server at "127.0.0.1", port 5432 failed: FATAL:  '
            'password authentication failed for user "artrag"',
            "db-port-taken",
        ),
        (
            "連接埠 15432 被這台電腦上的其他程式（postgres）佔用，萊問的資料庫沒辦法啟動",
            "db-port-taken",
        ),
        ("CUDA error: no kernel image is available for execution on the device", "llama-cuda"),
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


def test_offline_bundle_gets_offline_fixes(tmp_path, capsys, monkeypatch):
    # 離線包：app\ 旁邊有 offline\offline.json，沒有網路可以補下載，修法改成重新複製離線包
    app = tmp_path / "app"
    app.mkdir()
    (tmp_path / "offline").mkdir()
    (tmp_path / "offline" / "offline.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(diagnose, "REPO", app)
    log = write_log(
        app, "backend.log", "LocalEntryNotFoundError: Cannot find the requested files\n"
    )
    out = run(app, capsys, "--log", str(log))
    assert "重新複製整個資料夾" in out
    assert "HF_OFFLINE 暫時改成 false" not in out


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    # 離線包剛開始安裝：還沒寫 .env、包裡的 Ollama 也還沒起
    app = tmp_path / "app"
    app.mkdir()
    (tmp_path / "offline").mkdir()
    (tmp_path / "offline" / "offline.json").write_text(
        '{"ollama_port": 11499, "models": ["qwen3-vl:8b-instruct", "qwen3-vl:4b-instruct"]}',
        encoding="utf-8",
    )
    monkeypatch.setattr(diagnose, "REPO", app)
    return app


def test_broken_venv_in_bundle_says_delete_and_retry(bundle, capsys):
    log = write_log(
        bundle,
        "desktop.log",
        r"error: Project virtual environment directory `D:\LaiWen\app\backend\.venv` cannot be "
        "used because it is not a valid Python environment (no Python executable was found)\n",
    )
    out = run(bundle, capsys, "--log", str(log))
    assert r"Python 環境（backend\.venv）不完整" in out
    assert r"app\backend\.venv，再按萊問視窗的「重試」" in out


def test_wrong_password_is_port_taken_not_db_down(bundle, capsys):
    # 2026-10-30 學校電腦：連到別人的 PostgreSQL，「重新啟動服務」修不好
    log = write_log(
        bundle,
        "desktop.log",
        "沒有寫進資料庫：連不上資料庫 127.0.0.1:5432/artrag（connection failed: connection to "
        'server at "127.0.0.1", port 5432 failed: FATAL:  password authentication failed '
        'for user "artrag"）\n',
    )
    out = run(bundle, capsys, "--log", str(log))
    assert "連接埠被這台電腦上另外裝的程式" in out
    assert "PostgreSQL 容器沒起來" not in out
    assert "重新啟動服務" not in out


def test_bundle_prints_plain_words_before_each_cause(bundle, capsys):
    # 桌面程式靠「白話：」開頭的行決定先顯示什麼（desktop/splash.js 的 postDiagnosis）
    log = write_log(bundle, "desktop.log", "OSError: [Errno 28] No space left on device\n")
    lines = run(bundle, capsys, "--log", str(log)).splitlines()
    plain = next(i for i, line in enumerate(lines) if line.startswith("白話："))
    assert "硬碟空間不夠" in lines[plain]
    assert lines[plain + 1].startswith("✗ 磁碟空間不夠")


def test_plain_words_only_for_offline_bundle(repo, capsys):
    log = write_log(repo, "x.log", "OSError: [Errno 28] No space left on device\n")
    assert "白話：" not in run(repo, capsys, "--log", str(log))


def test_plain_words_avoid_jargon():
    for rule_id, text in diagnose.PLAIN.items():
        assert rule_id in {r[0] for r in diagnose.RULES}
        for word in ("WSL", "PostgreSQL", "Redis", ".env", "連接埠", "log"):
            assert word not in text, f"{rule_id} 的白話有術語 {word}"


def test_bundle_without_env_starts_its_ollama_then_stops_it(bundle, capsys, monkeypatch):
    log = write_log(bundle, "desktop.log", "ERROR: something unusual happened\n")
    events, sent = [], {}

    class Proc:
        def terminate(self):
            events.append("terminate")

        def wait(self, timeout=None):
            events.append("wait")

    monkeypatch.setattr(diagnose, "port_open", lambda url: False)
    monkeypatch.setattr(
        diagnose, "start_bundled_ollama", lambda url: events.append(("start", url)) or Proc()
    )

    def fake(base_url, model, api_key, text):
        sent.update(base_url=base_url, model=model, text=text)
        events.append("ask")
        return "原因：測試。\n修法：重試。"

    monkeypatch.setattr(diagnose, "ask_model", fake)
    out = run(bundle, capsys, "--log", str(log))
    assert events == [("start", "http://127.0.0.1:11499/v1"), "ask", "terminate", "wait"]
    assert sent["model"] == "qwen3-vl:8b-instruct"
    assert sent["text"].startswith("【環境】") and "沒有 Docker" in sent["text"]
    assert "暫時開了離線包的 Ollama" in out and "原因：測試" in out


def test_bundle_with_running_ollama_does_not_start_another(bundle, capsys, monkeypatch):
    log = write_log(bundle, "desktop.log", "ERROR: something unusual happened\n")
    monkeypatch.setattr(diagnose, "port_open", lambda url: True)
    monkeypatch.setattr(diagnose, "start_bundled_ollama", lambda url: pytest.fail("已經在跑"))
    monkeypatch.setattr(diagnose, "ask_model", lambda *a: "原因：測試。")
    out = run(bundle, capsys, "--log", str(log))
    assert "暫時開了" not in out and "原因：測試" in out
