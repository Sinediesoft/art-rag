"""輸出後處理：OpenCC s2twp，避免模型混入簡體字（共用層 §三）。

模型已經用繁體輸出，直接整段 s2twp 會誤轉「一簡對多繁」的字，例如「范寬」→「範寬」、
「皇后」→「皇後」。所以只替換「簡體專用字」：在 OpenCC 字典中，候選繁體不包含自己的字。
"""

from functools import lru_cache
from pathlib import Path


@lru_cache
def _converter():
    from opencc import OpenCC

    return OpenCC("s2twp")


@lru_cache
def _ambiguous_chars() -> frozenset[str]:
    """本身也是合法繁體字的簡體字（如 范、后、里、干），這些字保留原樣。"""
    import opencc

    path = Path(opencc.__file__).parent / "dictionary" / "STCharacters.txt"
    keep = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if "\t" not in line:
            continue
        src, targets = line.split("\t", 1)
        if src in targets.split():
            keep.add(src)
    return frozenset(keep)


def to_taiwan(text: str) -> str:
    if not text:
        return text
    conv = _converter()
    converted = conv.convert(text)
    keep = _ambiguous_chars()
    if len(converted) == len(text):
        return "".join(o if o in keep else c for o, c in zip(text, converted, strict=True))
    # 詞彙轉換改變了長度時，改成逐字處理
    return "".join(ch if ch in keep else conv.convert(ch) for ch in text)
