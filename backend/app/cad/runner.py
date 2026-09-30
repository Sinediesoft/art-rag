"""CadQuery 執行子行程（由 sandbox.run_cad 啟動，不要直接 import）。

stdin 收到工作 JSON → 以受限的內建函式執行程式碼 → 取出變數 solid →
（選擇性）依圖紙標註尺寸等比縮放 → 輸出 STL、STEP、回投影三視圖，與標準模型比 IoU。
結果寫到 out_dir/result.json；任何例外都寫成 {"ok": false, "error": ...}。
"""

import builtins
import json
import resource
import sys
import time
import traceback
from pathlib import Path

SAFE_BUILTINS = [
    "abs", "all", "any", "bool", "dict", "enumerate", "filter", "float", "int", "isinstance",
    "len", "list", "map", "max", "min", "pow", "print", "range", "reversed", "round", "set",
    "sorted", "str", "sum", "tuple", "zip", "True", "False", "None", "Exception", "ValueError",
    "ZeroDivisionError", "IndexError", "KeyError",
]  # fmt: skip
ALLOWED_MODULES = {"cadquery", "math"}


def _guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    if level != 0 or name.split(".")[0] not in ALLOWED_MODULES:
        raise ImportError(f"不允許 import {name}")
    return __import__(name, globals, locals, fromlist, level)


def _limit(cpu_s: int) -> None:
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_s, cpu_s + 5))
    resource.setrlimit(resource.RLIMIT_FSIZE, (200 * 1024 * 1024, 200 * 1024 * 1024))


def _to_shape(obj):
    import cadquery as cq

    if isinstance(obj, cq.Workplane):
        vals = [v for v in obj.vals() if isinstance(v, cq.Shape)]
        if not vals:
            raise ValueError("solid 裡沒有任何實體")
        return vals[0] if len(vals) == 1 else cq.Compound.makeCompound(vals)
    if isinstance(obj, cq.Shape):
        return obj
    raise ValueError(f"solid 的型別是 {type(obj).__name__}，不是 CadQuery 實體")


def _dims(shape) -> dict:
    bb = shape.BoundingBox()
    return {"width": bb.xlen, "depth": bb.ylen, "height": bb.zlen}


def main() -> None:
    job = json.loads(sys.stdin.read())
    out = Path(job["out_dir"])
    result: dict = {"ok": False, "error": None}
    t0 = time.perf_counter()
    try:
        _limit(job.get("cpu_limit_s", 60))
        import cadquery as cq

        from app.cad.drawing import render_ortho
        from app.cad.metrics import aligned_iou, bbox_iou

        code = Path(job["code_path"]).read_text(encoding="utf-8")
        safe = {n: getattr(builtins, n) for n in SAFE_BUILTINS}
        safe["__import__"] = _guarded_import
        ns: dict = {"__builtins__": safe, "__name__": "generated"}
        try:
            exec(compile(code, "generated.py", "exec"), ns)  # noqa: S102 — 已通過 AST 檢查、受限內建函式
        except Exception as e:
            tb = traceback.extract_tb(e.__traceback__)
            line = next((f.lineno for f in reversed(tb) if f.filename == "generated.py"), None)
            raise RuntimeError(
                f"程式碼執行錯誤{f'（第 {line} 行）' if line else ''}：{type(e).__name__}: {e}"
            ) from e
        obj = ns.get("solid", ns.get("result"))
        if obj is None:
            raise ValueError("程式碼沒有產生變數 solid")
        shape = _to_shape(obj)
        result["raw_dims"] = _dims(shape)
        result["valid"] = bool(shape.isValid())
        result["repaired"] = False
        if not result["valid"]:
            # 模型產生的實體常有細微瑕疵（面方向、縫隙）：先用 ShapeFix 修，修得好就用修過的
            # 匯出與算 IoU；實測未修復時 IoU 會從 0.73 掉到 0.12
            fixed = shape.fix()
            if fixed.isValid():
                shape, result["repaired"] = fixed, True
        result["exec_ms"] = round((time.perf_counter() - t0) * 1000)

        # 依圖紙標註尺寸等比縮放：最小平方法求單一比例 s（模型輸出為 DeepCAD 正規化尺度）
        scale = 1.0
        target = job.get("scale_to")
        if target:
            g = [result["raw_dims"][k] for k in ("width", "depth", "height")]
            t = [float(target[k]) for k in ("width", "depth", "height")]
            den = sum(x * x for x in g)
            if den > 0:
                scale = sum(x * y for x, y in zip(g, t, strict=True)) / den
                shape = shape.scale(scale)
        result["scale"] = scale
        result["dims"] = _dims(shape)
        result["volume"] = shape.Volume()
        result["faces"] = len(shape.Faces())

        size = max(result["dims"].values()) or 1.0
        cq.exporters.export(
            shape, str(out / "model.stl"), tolerance=size * 1e-3, angularTolerance=0.15
        )
        cq.exporters.export(shape, str(out / "model.step"))
        render_ortho(shape).save(out / "reproj.png")

        if job.get("gt_step"):
            gt = _to_shape(cq.importers.importStep(job["gt_step"]))
            result["iou"] = aligned_iou(shape, gt)
            result["iou_bbox"] = bbox_iou(shape, gt)
        result["ok"] = True
    except Exception as e:  # 錯誤訊息回給使用者看
        result["error"] = (
            str(e) if isinstance(e, (RuntimeError, ValueError)) else f"{type(e).__name__}: {e}"
        )
    result["total_ms"] = round((time.perf_counter() - t0) * 1000)
    (out / "result.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
