"""執行模型產生的 CadQuery 程式碼：先做靜態檢查，再丟到隔離的子行程執行。

模型輸出的是 Python 程式碼，不能直接 exec。三層防護：
1. AST 白名單（這支）：只准 import cadquery／math；不准底線開頭的屬性、eval/open 等內建函式、
   CadQuery 的匯出入（exporters、importers、export*）與底層 OCP 物件，也不准 class／global／with
2. 子行程（runner.py）：只給安全的內建函式、限制 CPU 時間與輸出檔大小，不帶任何環境變數與金鑰；
   限制不了的平台（Windows 沒有 resource 模組）只執行知識庫自己的標準模型——
   有設 WSL（WslTarget，docs/adr/027）時，模型產生的程式碼改到 WSL2 的 Linux 裡執行，照樣限制
3. macOS 再包一層 sandbox-exec：禁止網路、只准寫入這次工作的暫存目錄；
   WSL 包一層 unshare -rn（沒有網路）與 timeout（時間到一定結束）
逾時直接砍掉子行程。
"""

import ast
import asyncio
import importlib.util
import json
import os
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path, PurePath, PureWindowsPath

BACKEND_DIR = Path(__file__).resolve().parents[2]
# 子行程能不能限制 CPU 時間與輸出檔大小：Windows 沒有 resource 模組
LIMITS_AVAILABLE = importlib.util.find_spec("resource") is not None
ALLOWED_MODULES = {"cadquery", "math"}
FORBIDDEN_NAMES = {
    "__import__", "eval", "exec", "compile", "open", "input", "globals", "locals", "vars",
    "getattr", "setattr", "delattr", "hasattr", "breakpoint", "exit", "quit", "help", "dir",
    "memoryview", "super", "type", "object", "classmethod", "staticmethod", "property", "id",
}  # fmt: skip
FORBIDDEN_ATTRS = {
    "exporters", "importers", "occ_impl", "cqgi", "vis", "wrapped", "save", "show",
    "show_object", "toOCC", "format", "format_map", "system", "popen",
}  # fmt: skip
FORBIDDEN_NODES = (
    ast.Global, ast.Nonlocal, ast.With, ast.AsyncWith, ast.AsyncFunctionDef, ast.Await,
    ast.ClassDef, ast.Yield, ast.YieldFrom,
)  # fmt: skip
MAX_CODE_CHARS = 30_000


class UnsafeCode(Exception):
    pass


def extract_code(text: str) -> str:
    """模型回答 → 程式碼：有 ``` 區塊就取最長的一段，否則整段當程式碼。"""
    blocks = re.findall(r"```(?:python|py)?\s*\n(.*?)```", text, flags=re.S)
    if blocks:
        return max(blocks, key=len).strip()
    return text.replace("```python", "").replace("```", "").strip()


def _bad_attr(name: str) -> bool:
    return name.startswith("_") or name in FORBIDDEN_ATTRS or name.startswith(("export", "import"))


def check_code(code: str) -> None:
    """不安全就丟 UnsafeCode（訊息會顯示給使用者）。"""
    if len(code) > MAX_CODE_CHARS:
        raise UnsafeCode(f"程式碼超過 {MAX_CODE_CHARS} 字元")
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        raise UnsafeCode(f"語法錯誤（第 {e.lineno} 行）：{e.msg}") from e
    for node in ast.walk(tree):
        if isinstance(node, FORBIDDEN_NODES):
            raise UnsafeCode(f"不允許的語法：{type(node).__name__}")
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] not in ALLOWED_MODULES or any(
                    _bad_attr(p) for p in a.name.split(".")[1:]
                ):
                    raise UnsafeCode(f"不允許 import {a.name}")
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if node.level or mod.split(".")[0] not in ALLOWED_MODULES:
                raise UnsafeCode(f"不允許 from {mod} import")
            if any(_bad_attr(p) for p in mod.split(".")[1:]) or any(
                a.name == "*" or _bad_attr(a.name) for a in node.names
            ):
                raise UnsafeCode(
                    f"不允許 from {mod} import {', '.join(a.name for a in node.names)}"
                )
        elif isinstance(node, ast.Attribute) and _bad_attr(node.attr):
            raise UnsafeCode(f"不允許存取屬性 .{node.attr}")
        elif isinstance(node, ast.Name) and (
            node.id in FORBIDDEN_NAMES or node.id.startswith("__")
        ):
            raise UnsafeCode(f"不允許使用 {node.id}")


@dataclass
class RunResult:
    ok: bool
    error: str | None
    data: dict


@dataclass(frozen=True)
class WslTarget:
    """Windows 主機在 WSL2 執行模型產生的程式碼（docs/adr/027）。

    來自 .env 的 CAD_WSL_PYTHON／CAD_WSL_DISTRO（services/cad_service.wsl_target）。
    """

    python: str  # WSL 裡裝好 CadQuery 的 Python，例如 /home/me/artrag-cad/bin/python
    distro: str = ""  # 留空＝wsl.exe 的預設發行版


def wsl_path(p: PurePath) -> str:
    """Windows 路徑 → WSL 路徑（預設掛載點 /mnt/<磁碟機>）：C:/Users/me → /mnt/c/Users/me。"""
    w = PureWindowsPath(p)
    if len(w.drive) != 2 or w.drive[1] != ":":
        raise ValueError(f"WSL 只能轉換磁碟機路徑（例如 C:/…）：{p}")
    return f"/mnt/{w.drive[0].lower()}{w.as_posix()[2:]}"


def _wsl_argv(wsl: WslTarget, job_dir: Path, timeout_s: float) -> list[str]:
    """wsl.exe → timeout（時間到一定結束）→ unshare -rn（沒有網路）
    → env -i（不帶任何環境變數）→ runner。

    Linux 裡有 resource 模組，CPU 時間與輸出檔大小照常由 runner 限制。
    """
    work, backend = wsl_path(job_dir.resolve()), wsl_path(BACKEND_DIR)
    return [
        "wsl.exe",
        *(["-d", wsl.distro] if wsl.distro else []),
        "--cd",
        backend,
        "--exec",
        "/usr/bin/timeout",
        "-k",
        "5",
        str(int(timeout_s) + 10),
        "/usr/bin/unshare",
        "-rn",
        "/usr/bin/env",
        "-i",
        "PATH=/usr/bin:/bin",
        f"PYTHONPATH={backend}",
        "PYTHONDONTWRITEBYTECODE=1",
        f"HOME={work}",
        f"TMPDIR={work}",
        wsl.python,
        "-m",
        "app.cad.runner",
    ]


def _decode(b: bytes) -> str:
    """wsl.exe 自己的錯誤訊息是 UTF-16LE，Linux 裡的程式是 UTF-8。"""
    if b.count(0) > len(b) // 4:
        return b.decode("utf-16-le", "replace")
    return b.decode("utf-8", "replace")


def _sandbox_prefix(job_dir: Path) -> list[str]:
    """macOS：sandbox-exec 禁網路、只准寫入工作目錄；其他平台不包（靠 AST＋子行程限制）。"""
    exe = shutil.which("sandbox-exec")
    if sys.platform != "darwin" or not exe:
        return []
    profile = (
        "(version 1)(allow default)(deny network*)(deny file-write*)"
        f'(allow file-write* (subpath "{job_dir.resolve()}") (literal "/dev/null")'
        ' (regex #"^/dev/tty") (regex #"^/private/var/folders/"))'
    )
    return [exe, "-p", profile]


async def run_cad(
    code: str,
    job_dir: Path,
    *,
    scale_to: dict | None = None,
    gt_step: Path | None = None,
    timeout_s: float = 60,
    trusted: bool = False,
    wsl: WslTarget | None = None,
) -> RunResult:
    """在子行程執行 CadQuery 程式碼，輸出 model.stl／.step、reproj.png、result.json 到 job_dir。

    trusted=True 只給知識庫自己的標準模型（kb/cad/*.py）用，仍走同一個子行程。沒有 resource 模組的
    平台（Windows）上，子行程沒有 CPU 時間與檔案大小限制，只剩這裡的逾時（timeout_s + 15 秒）。
    wsl：沒有 resource 模組時，模型產生的程式碼改到 WSL2 執行；有 resource 模組的平台忽略。
    """
    in_wsl = not trusted and not LIMITS_AVAILABLE and wsl is not None
    if not trusted:
        if not LIMITS_AVAILABLE and wsl is None:
            return RunResult(
                False,
                "這台電腦（Windows）限制不了子行程的 CPU 時間與檔案大小，不執行模型產生的程式碼；"
                "3D 重建請在 macOS、Linux 或 WSL 執行，或在 .env 設 CAD_WSL_PYTHON（docs/adr/027）",
                {},
            )
        try:
            check_code(code)
        except UnsafeCode as e:
            return RunResult(False, f"安全檢查未通過：{e}", {})
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "code.py").write_text(code, encoding="utf-8")
    path = wsl_path if in_wsl else str
    job = {
        "code_path": path((job_dir / "code.py").resolve()),
        "out_dir": path(job_dir.resolve()),
        "scale_to": scale_to,
        "gt_step": path(gt_step.resolve()) if gt_step else None,
        "cpu_limit_s": int(timeout_s),
        "trusted": trusted,
    }
    if in_wsl:
        # Linux 這邊的環境變數由 env -i 清掉；拿掉 WSLENV，Windows 的變數不會被轉進去
        argv = _wsl_argv(wsl, job_dir, timeout_s)
        env = {k: v for k, v in os.environ.items() if k.upper() != "WSLENV"}
        wall_s = timeout_s + 30  # WSL 沒在執行時要先開機（幾秒）
    else:
        argv = [*_sandbox_prefix(job_dir), sys.executable, "-m", "app.cad.runner"]
        env = _native_env(job_dir)
        wall_s = timeout_s + 15
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=BACKEND_DIR,
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(
            proc.communicate(json.dumps(job).encode()), timeout=wall_s
        )
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return RunResult(False, f"執行逾時（>{timeout_s:.0f} 秒），已中止", {})
    result_path = job_dir / "result.json"
    if not result_path.exists():
        # CPU 硬限制的 SIGKILL（直接執行是 -9；經過 wsl.exe 回 9，timeout -k 補砍是 128+9），
        # 或 timeout 指令到時間（124）
        if proc.returncode in (-9, 9, 124, 137):
            return RunResult(False, f"執行逾時（>{timeout_s:.0f} 秒），已中止", {})
        tail = _decode(stderr).strip().splitlines()[-3:]
        return RunResult(False, "執行失敗：" + (" ".join(tail) or f"exit {proc.returncode}"), {})
    data = json.loads(result_path.read_text(encoding="utf-8"))
    return RunResult(data.get("ok", False), data.get("error"), data)


def _native_env(job_dir: Path) -> dict[str, str]:
    """在這台電腦直接執行時給子行程的環境變數：不帶任何金鑰。"""
    work = str(job_dir.resolve())
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONPATH": str(BACKEND_DIR),
        "PYTHONDONTWRITEBYTECODE": "1",
        "HOME": work,
        "TMPDIR": work,
    }
    if sys.platform == "win32":
        # Windows 的 Python 用 USERPROFILE 找家目錄（不看 HOME），少了會報
        # 「Could not determine home directory」；暫存檔比照 TMPDIR 留在工作目錄
        env |= {"USERPROFILE": work, "TEMP": work, "TMP": work}
    return env
