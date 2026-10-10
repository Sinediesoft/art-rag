"""輸出後處理：OpenCC s2twp，避免模型混入簡體字（共用層 §三）；拿掉答完才補的拒答句。

模型已經用繁體輸出，直接整段 s2twp 會誤轉「一簡對多繁」的字，例如「范寬」→「範寬」、
「皇后」→「皇後」。所以只替換「簡體專用字」：在 OpenCC 字典中，候選繁體不包含自己的字。
"""

import re
from functools import lru_cache
from pathlib import Path

# prompt 第 3 條規定的拒答句（answer_v1–v4、drawing_v1–v2 同一句）
NO_DATA = "知識庫中沒有這方面的資料。"
_NO_DATA_RE = re.compile(r"[ \t]*知識庫中沒有這方面的資料[。.]?(?:[ \t]*\[\d+\])*[ \t]*\n?")
_CITED_RE = re.compile(r"\[(?:[1-9]\d*|畫面)\]")


@lru_cache
def _converter():
    from opencc import OpenCC

    return OpenCC("s2twp")


@lru_cache
def _char_sets() -> tuple[frozenset[str], frozenset[str]]:
    """(本身也是合法繁體字的簡體字, 簡體專用字)。前者如 范、后、里、干，保留原樣。"""
    import opencc

    path = Path(opencc.__file__).parent / "dictionary" / "STCharacters.txt"
    keep, simplified = set(), set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if "\t" not in line:
            continue
        src, targets = line.split("\t", 1)
        (keep if src in targets.split() else simplified).add(src)
    return frozenset(keep), frozenset(simplified)


def _ambiguous_chars() -> frozenset[str]:
    return _char_sets()[0]


def to_taiwan(text: str) -> str:
    # 沒有簡體專用字就原樣回傳：s2twp 的詞彙轉換會把繁體的「設備」改成「裝置」，
    # 客戶名稱「栩達設備」這類專有名詞就被改掉了
    if not text or not any(ch in _char_sets()[1] for ch in text):
        return text
    conv = _converter()
    converted = conv.convert(text)
    keep = _ambiguous_chars()
    if len(converted) == len(text):
        return "".join(o if o in keep else c for o, c in zip(text, converted, strict=True))
    # 詞彙轉換改變了長度時，改成逐字處理
    return "".join(ch if ch in keep else conv.convert(ch) for ch in text)


def drop_stray_no_data(answer: str) -> str:
    """已經引用資料回答了，又在後面補一句「知識庫中沒有這方面的資料。」：拿掉那句。
    小模型常把 prompt 第 3 條當成結尾套話（TASK-20261010-001：附 mfg-006 截圖問「立式軸承座」，
    三句都引用段落，最後一句卻說知識庫沒有資料）。沒有任何引用時是真的答不出來，整段保留。"""
    if NO_DATA[:-1] not in answer:
        return answer
    rest = _NO_DATA_RE.sub("", answer)
    if not _CITED_RE.search(rest):
        return answer
    return re.sub(r"\n{3,}", "\n\n", rest).rstrip()
