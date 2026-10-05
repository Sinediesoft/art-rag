"""記憶體管理的兩個記憶體池（docs/adr/021）。

有 NVIDIA 顯示卡時，Ollama 與 llama-server 的模型在 VRAM：系統記憶體滿了只釋放系統記憶體裡的模型，
VRAM 滿了只釋放 VRAM 裡的模型；沒有顯示卡時和原本一樣（test_schedule.py）。
"""

import subprocess
from types import SimpleNamespace

import pytest

from app.services import memory_guard

GPU_TOTAL = 16000


@pytest.fixture
def two_pools(monkeypatch):
    """CLIP、bge 在系統記憶體；Qwen3-VL、Ortho2CAD 在 VRAM。都已載入，釋放時記錄下來。"""
    released: list[str] = []
    loaded = {"clip": True, "bge": True, "qwen": True, "ortho2cad": True}
    pools = {"clip": "ram", "bge": "ram", "qwen": "gpu", "ortho2cad": "gpu"}
    sizes = {"clip": 750, "bge": 2200, "qwen": 7000, "ortho2cad": 6200}

    def make(key):
        def release():
            released.append(key)
            loaded[key] = False
            return "ok"

        return memory_guard.ManagedModel(
            key, key, sizes[key], "test", lambda: loaded[key], release, pool=pools[key]
        )

    state = {"ram": 50.0, "gpu": 50.0}
    monkeypatch.setattr(memory_guard, "_models", lambda: [make(k) for k in loaded])
    monkeypatch.setattr(memory_guard.time, "sleep", lambda s: None)
    monkeypatch.setattr(memory_guard, "has_gpu", lambda: True)
    monkeypatch.setattr(memory_guard, "memory_percent", lambda: state["ram"])
    monkeypatch.setattr(
        memory_guard,
        "gpu_memory",
        lambda: memory_guard.GpuMemory("RTX", int(state["gpu"] / 100 * GPU_TOTAL), GPU_TOTAL),
    )
    settings = SimpleNamespace(memory_guard=True, memory_high_pct=80.0, memory_gpu_high_pct=90.0)
    monkeypatch.setattr(memory_guard, "get_settings", lambda: settings)
    return memory_guard.MemoryGuard(), released, loaded, state


def test_full_system_ram_does_not_unload_vram_models(two_pools):
    """5070 Ti 主機實際碰到的情況：遊戲把系統記憶體吃到 98%，VRAM 還有空間——
    原本會每幾秒卸載一次 Qwen3-VL，現在只放系統記憶體裡的模型。"""
    g, released, _, state = two_pools
    state["ram"] = 98.0
    event = g.check("背景監控", set())
    assert sorted(released) == ["bge", "clip"]
    assert event["pool"] == "ram" and event["percent_before"] == 98.0
    assert event["gpu_percent_before"] == 50.0


def test_full_vram_unloads_only_vram_models_not_in_use(two_pools):
    g, released, _, state = two_pools
    state["gpu"] = 95.0
    g.enter("chat", {"bge", "qwen"})
    event = g.check("背景監控", set(g.current_models))
    assert released == ["ortho2cad"]  # 系統記憶體沒滿：CLIP 不動；Qwen3-VL 正在用
    assert event["pool"] == "gpu" and event["threshold"] == 90.0
    assert event["percent_before"] == 95.0 and "qwen" in event["kept"]


def test_both_below_threshold_does_nothing(two_pools):
    g, released, _, _ = two_pools
    assert g.check("背景監控", set()) is None and released == []


def test_anticipate_counts_model_into_its_own_pool(two_pools):
    """VRAM 60% 時進入 3D 重建：Ortho2CAD（6.2 GB／16 GB）載入後約 99%，先放 VRAM 裡的 Qwen3-VL；
    系統記憶體同時是 78%，但 Ortho2CAD 不在系統記憶體，不能拿它去推估系統記憶體。"""
    g, released, loaded, state = two_pools
    loaded["ortho2cad"] = False
    state["ram"], state["gpu"] = 78.0, 60.0
    event = g.check("進入「3D 重建」", {"ortho2cad"}, anticipate=True)
    assert released == ["qwen"]
    assert "預估載入 ortho2cad 後顯示記憶體約" in event["trigger"]


def test_force_releases_both_pools(two_pools):
    g, released, _, _ = two_pools
    event = g.check("手動釋放", set(), force=True)
    assert sorted(released) == ["bge", "clip", "ortho2cad", "qwen"] and event is not None


def test_status_reports_gpu_and_pools(two_pools):
    g, _, _, state = two_pools
    state["gpu"] = 40.0
    st = g.status()
    assert st["gpu"]["name"] == "RTX" and st["gpu"]["threshold"] == 90.0
    assert round(st["gpu"]["percent"]) == 40
    assert {m["key"]: m["pool"] for m in st["models"]}["qwen"] == "gpu"


def test_gpu_memory_none_without_nvidia_smi(monkeypatch):
    monkeypatch.setattr(memory_guard.shutil, "which", lambda name: None)
    assert memory_guard.gpu_memory() is None


def test_gpu_memory_parses_nvidia_smi(monkeypatch):
    monkeypatch.setattr(memory_guard.shutil, "which", lambda name: "nvidia-smi")
    out = "NVIDIA GeForce RTX 5070 Ti, 9578, 16303\n"
    monkeypatch.setattr(memory_guard.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=out))
    g = memory_guard.gpu_memory()
    assert g.name == "NVIDIA GeForce RTX 5070 Ti" and (g.used_mb, g.total_mb) == (9578, 16303)
    assert round(g.percent) == 59


def test_gpu_memory_none_when_query_fails(monkeypatch):
    monkeypatch.setattr(memory_guard.shutil, "which", lambda name: "nvidia-smi")

    def boom(*a, **k):
        raise subprocess.TimeoutExpired("nvidia-smi", 3)

    monkeypatch.setattr(memory_guard.subprocess, "run", boom)
    assert memory_guard.gpu_memory() is None
