"""輸出後處理：OpenCC s2twp，避免模型混入簡體字（共用層 §三）；拿掉答完才補的拒答句。

模型已經用繁體輸出，直接整段 s2twp 會誤轉「一簡對多繁」的字，例如「范寬」→「範寬」、
「皇后」→「皇後」。所以只替換「簡體專用字」：在 OpenCC 字典中，候選繁體不包含自己的字。
"""

import re
import unicodedata
from collections.abc import Iterable
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
# 一整句的拒答句（開頭、換行或句號／問號／驚嘆號之後，到句號為止；不是接在句中的）
_NO_DATA_SENTENCE_RE = re.compile(
    r"(?:^|(?<=[\n。！？!?]))[ \t]*知識庫中沒有這方面的資料[。.]?(?:[ \t]*\[\d+\])*[ \t]*\n?"
)
# 比對「問題是不是純粹指名這份文件」時忽略的空白與標點（兩邊都先 NFKC，全形標點已轉半形）
_IGNORED_RE = re.compile(r"[\s,.?!:;、。，．？！：；「」『』〈〉《》()（）]+")
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


def _squash(text: str) -> str:
    return _IGNORED_RE.sub("", unicodedata.normalize("NFKC", text)).casefold()


def drop_stray_no_data(answer: str, question: str = "", bare: Iterable[str] = ()) -> str:
    """已經引用資料回答了，又在最後補一句獨立的「知識庫中沒有這方面的資料。」：拿掉那句。
    小模型常把 prompt 第 3 條當成結尾套話（TASK-20261010-001：附 mfg-006 截圖問「立式軸承座」，
    三句都引用段落，最後一句卻說知識庫沒有資料）。

    只在確認問題沒有任何具體要問的事時才拿：question 必須是 bare 其中之一（呼叫端給的「純粹指名這份
    文件」的字串：名稱、料號、圖號，或只附照片時的預設概覽問句；忽略空白、標點、大小寫與全半形）。
    問題裡多了任何字（另一個問題、省略問號的第二問句…）就無法證明拒答句是多餘的，整段保留；
    bare 預設是空的，所以沒有可確認的指名時永遠不拿。
    即使問題是純粹指名，這些情況也保留：沒有任何引用（真的答不出來）、不在最後或接在句中
    （「包裝方式：…」「由於…，因此…」、引號內）、前一行是子標題（「包裝方式：」換行再接拒答句）。
    只截掉結尾、不拼接前後片段，拿掉後前面那段原樣不動。"""
    if NO_DATA[:-1] not in answer:
        return answer
    asked = _squash(question)
    if not asked or asked not in {_squash(b) for b in bare}:
        return answer
    m = _TRAILING_NO_DATA_RE.search(answer)
    if not m:
        return answer
    rest = answer[: m.start()].rstrip()
    if not _CITED_RE.search(rest) or not _ENDS_SENTENCE_RE.search(rest):
        return answer
    return rest


def drop_contradicting_no_data(answer: str) -> str:
    """說了「知識庫中沒有這方面的資料。」，後面卻又引用資料回答：拿掉那句。
    錯誤前提題（「董其昌是怎麼在 1958 年找到簽名的？」）常見：
    先說沒有資料，接著用 [1] 更正成李霖燦，
    前後矛盾、讀者會以為沒找到。
    只拿「後面還有引用（[n] 或 [畫面]）」的那句：放在最後的拒答句可能是在說「其餘的部分查不到」，
    交給 drop_stray_no_data 的保守規則；沒有任何引用的拒答（真的答不出來）不動。"""
    if NO_DATA[:-1] not in answer:
        return answer
    out, last = [], 0
    for m in _NO_DATA_SENTENCE_RE.finditer(answer):
        if _CITED_RE.search(answer[m.end() :]):
            out.append(answer[last : m.start()])
            last = m.end()
    if not out:
        return answer
    out.append(answer[last:])
    return "".join(out).strip()


# 年份與「年＋月」：日期是資料才有的事實，模型算不出來；年齡、面積這類計算結果不在這裡檢查。
# 後面接單位的（1200 mm、2026 件）不是年份
_YEAR_MONTH_RE = re.compile(r"(?<![\d.])(1\d{3}|20\d{2})\s*年\s*(\d{1,2})\s*月")
_YEAR_RE = re.compile(
    r"(?<![\d.])(1\d{3}|20\d{2})(?![\d.]|\s*(?:公|平方|立方|件|元|克|台|個|mm|cm|kg|g\b|%))"
)
_SENTENCE_RE = re.compile(r"[^。！？!?\n]*(?:[。！？!?]+|\n|$)")


def _dates(text: str) -> set[str]:
    squashed = re.sub(r"\s+", "", text)
    found = {f"{y}年{int(m)}月" for y, m in _YEAR_MONTH_RE.findall(squashed)}
    return found | set(_YEAR_RE.findall(squashed))


def unsupported_dates(answer: str, support: str) -> list[str]:
    """回答裡出現、但參考資料／作品資料／問題都沒有的年份或「年＋月」（依出現順序、不重複）。
    梵谷「1853 年出生」、「〈星夜〉1889 年 6 月」這類模型自己補的日期，標了出處讀者也會相信。"""
    known = _dates(support)
    out: list[str] = []
    for d in sorted(_dates(answer), key=lambda d: re.sub(r"\s+", "", answer).find(d)):
        # 「1889年6月」不在資料裡，但年份 1889 在：只回報年月；年份本身不在才回報年份
        if d not in known and d not in out:
            out.append(d)
    return out


def drop_sentences_with(answer: str, terms: Iterable[str]) -> str:
    """拿掉含有這些年份／年月的句子（比對時忽略空白）。"""
    terms = list(terms)
    kept = [
        s
        for s in _SENTENCE_RE.findall(answer)
        if s.strip() and not any(t in re.sub(r"\s+", "", s) for t in terms)
    ]
    return "".join(kept).strip()
