"""輸出後處理：OpenCC s2twp，避免模型混入簡體字（共用層 §三）；拿掉答完才補的拒答句。

模型已經用繁體輸出，直接整段 s2twp 會誤轉「一簡對多繁」的字，例如「范寬」→「範寬」、
「皇后」→「皇後」。所以只替換「簡體專用字」：在 OpenCC 字典中，候選繁體不包含自己的字。
"""

import re
from functools import lru_cache
from pathlib import Path

# prompt 第 3 條規定的拒答句（answer_v1–v4、drawing_v1–v2 同一句）
NO_DATA = "知識庫中沒有這方面的資料。"
# 只認結尾那一句：前面是開頭、換行或句號／問號／驚嘆號
# （不是「包裝方式：」「由於」「「」這類接在句中的），後面只能是引用編號、空白到結尾
_TRAILING_NO_DATA_RE = re.compile(
    r"(?:^|(?<=[\n。！？!?]))[ \t]*知識庫中沒有這方面的資料[。.]?(?:[ \t]*\[\d+\])*\s*\Z"
)
_CITED_RE = re.compile(r"\[(?:[1-9]\d*|畫面)\]")
# 問題同時問了好幾件事：通用拒答句可能是在說其中一件沒資料，不能確定是多餘的。
# 換行也算（分行問兩件事；CRLF 同樣有 \n）；問號另外數整段（不看換行）
_MULTI_ASK_RE = re.compile(r"[和與及跟、，,；;／/\r\n]|以及|還有|或是|或者|並且|分別|各自")
# 拿掉拒答句後，前面那段要以句號／問號／驚嘆號或引用結尾；結尾是「包裝方式：」這類子標題時，
# 拒答句是在回答那個標題
_ENDS_SENTENCE_RE = re.compile(r"(?:[。！？!?.]|\[(?:\d+|畫面)\])\Z")


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


def drop_stray_no_data(answer: str, question: str = "") -> str:
    """已經引用資料回答了，又在最後補一句獨立的「知識庫中沒有這方面的資料。」：拿掉那句。
    小模型常把 prompt 第 3 條當成結尾套話（TASK-20261010-001：附 mfg-006 截圖問「立式軸承座」，
    三句都引用段落，最後一句卻說知識庫沒有資料）。

    不能確定是多餘套話時整段保留：沒有任何引用（真的答不出來）、不在最後或接在句中
    （「包裝方式：…」「由於…，因此…」、引號內）、前一行是子標題（「包裝方式：」換行再接拒答句）、
    問題同時問了好幾件事（連接詞、分行或兩個以上問號；可能在說其中一件沒資料）。
    只截掉結尾、不拼接前後片段，拿掉後前面那段原樣不動。"""
    if NO_DATA[:-1] not in answer or _asks_several(question):
        return answer
    m = _TRAILING_NO_DATA_RE.search(answer)
    if not m:
        return answer
    rest = answer[: m.start()].rstrip()
    if not _CITED_RE.search(rest) or not _ENDS_SENTENCE_RE.search(rest):
        return answer
    return rest


def _asks_several(question: str) -> bool:
    q = question.strip()
    return bool(_MULTI_ASK_RE.search(q)) or q.count("？") + q.count("?") >= 2
