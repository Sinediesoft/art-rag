"""執行模型產生的 CadQuery 程式碼：先做靜態檢查，再丟到隔離的子行程執行。

模型輸出的是 Python 程式碼，不能直接 exec。三層防護：
1. AST 白名單（這支）：只准 import cadquery／math；不准底線開頭的屬性、eval/open 等內建函式、
   CadQuery 的匯出入（exporters、importers、export*）與底層 OCP 物件，也不准 class／global／with
2. 子行程（runner.py）：只給安全的內建函式、限制 CPU 時間與輸出檔大小，不帶任何環境變數與金鑰
3. macOS 再包一層 sandbox-exec：禁止網路、只准寫入這次工作的暫存目錄
逾時直接砍掉子行程。
"""

import ast
import asyncio
import json
import os
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
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
) -> RunResult:
    """在子行程執行 CadQuery 程式碼，輸出 model.stl／.step、reproj.png、result.json 到 job_dir。

    trusted=True 只給知識庫自己的標準模型（kb/cad/*.py）用，仍走同一個子行程與限制。
    """
    if not trusted:
        try:
            check_code(code)
        except UnsafeCode as e:
            return RunResult(False, f"安全檢查未通過：{e}", {})
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "code.py").write_text(code, encoding="utf-8")
    job = {
        "code_path": str((job_dir / "code.py").resolve()),
        "out_dir": str(job_dir.resolve()),
        "scale_to": scale_to,
        "gt_step": str(gt_step.resolve()) if gt_step else None,
        "cpu_limit_s": int(timeout_s),
    }
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONPATH": str(BACKEND_DIR),
        "PYTHONDONTWRITEBYTECODE": "1",
        "HOME": str(job_dir.resolve()),
        "TMPDIR": str(job_dir.resolve()),
    }
    proc = await asyncio.create_subprocess_exec(
        *_sandbox_prefix(job_dir),
        sys.executable,
        "-m",
        "app.cad.runner",
        cwd=BACKEND_DIR,
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(
            proc.communicate(json.dumps(job).encode()), timeout=timeout_s + 15
        )
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return RunResult(False, f"執行逾時（>{timeout_s:.0f} 秒），已中止", {})
    result_path = job_dir / "result.json"
    if not result_path.exists():
        tail = stderr.decode("utf-8", "replace").strip().splitlines()[-3:]
        return RunResult(False, "執行失敗：" + (" ".join(tail) or f"exit {proc.returncode}"), {})
    data = json.loads(result_path.read_text(encoding="utf-8"))
    return RunResult(data.get("ok", False), data.get("error"), data)
