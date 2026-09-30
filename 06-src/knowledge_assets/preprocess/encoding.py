"""Calibre 预处理：编码错误（mojibake）的探测与还原。

背景
----
增量 metadata.db 中部分记录来自「GBK/CP936 字节流被按 UTF-8 解码」的错误导入：

    原始 GBK 字节 --按 UTF-8 解码(errors='replace')--> 存入 DB 的字符串

按原始字节能否凑成合法 UTF-8 序列，结果分成两类：

1. **恰好构成合法序列** —— 字节仍可无损还原::

       b'\\xd2\\xa3' --UTF-8--> 'ң'(U+04A3) --latin-1--> b'\\xd2\\xa3' --gb18030--> '遥'

2. **构不成合法序列** —— 被替换成 U+FFFD，**原始字节已永久丢失，不可还原**::

       b'\\xb2\\xbb' --UTF-8--> '\\ufffd\\ufffd' --??--> 无从恢复

本模块只对第 1 类做自动还原（:func:`restore_text`）；
第 2 类仅给出「部分还原提示」（:func:`partial_hint`）供人工判断，绝不猜测。

判定原则
--------
**宁可漏判，不可误判。** 判定基于「编码探针」而非字符黑名单：

- 正常中文/日文标题（含假名、装饰符号、罗马数字）无法被 latin-1/cp1252 编码，
  探针自然失败，因此不会被误判 —— 这是本模块相对字符黑名单的核心改进；
- 只有「能还原出合法中文」的字符串才被判定为乱码。
"""

from __future__ import annotations

from dataclasses import dataclass

REPLACEMENT_CHAR = "�"

#: 部分还原提示中代表「原始字节已丢失」的占位符
HINT_FILLER = "◻"

#: 恢复链路：(把文本编码回字节所用的编码, 再用它解码)
#: 文本是「原始字节被前一编码解码」的产物，故先用同一编码编回字节
_RESTORE_CHAINS: tuple[tuple[str, str], ...] = (
    ("latin-1", "gb18030"),
    ("cp1250", "gb18030"),
    ("cp1252", "gb18030"),
    ("latin-1", "big5"),
)

#: 正常标题会用到、不应被视为乱码证据的字符区段
_ORDINARY_RANGES: tuple[tuple[int, int], ...] = (
    (0x0020, 0x007E),  # ASCII 可打印
    (0x2010, 0x203B),  # 常用标点 —— … ‘’“”
    (0x2044, 0x205E),  # ⁄ ⁅
    (0x2160, 0x2183),  # 罗马数字 Ⅰ Ⅱ
    (0x2600, 0x27BF),  # 杂项符号 ★ ☆ ✓
    (0x3000, 0x303F),  # CJK 标点 、。《》〇
    (0x3040, 0x30FF),  # 日文平假名/片假名
    (0x4E00, 0x9FFF),  # CJK 基本区
    (0xFF01, 0xFF5E),  # 全角 ASCII
    (0xFF5F, 0xFF60),
    (0xFFE0, 0xFFE6),
)

#: 还原结果必须达到的中文占比阈值
_NATIVE_CJK_RATIO = 0.5

#: 判定「明显是乱码产物」的字符区段（用于部分还原时的提示，不用于判定）
_EXTENDED_LATIN_MAX = 0x024F


@dataclass(frozen=True)
class RestoreResult:
    """一次成功的无损还原。"""

    chain: tuple[str, str]
    restored: str
    original: str

    @property
    def chain_label(self) -> str:
        return f"{self.chain[0]}->{self.chain[1]}"


def is_cjk(char: str) -> bool:
    """是否为 CJK 统一表意文字（基本区）。"""
    return 0x4E00 <= ord(char) <= 0x9FFF


def cjk_ratio(text: str) -> float:
    """文本中 CJK 字符的占比。"""
    if not text:
        return 0.0
    return sum(1 for char in text if is_cjk(char)) / len(text)


def is_ordinary_char(char: str) -> bool:
    """该字符是否属于「正常标题会用到的字符」。"""
    if char == REPLACEMENT_CHAR:
        return False
    code = ord(char)
    return any(low <= code <= high for low, high in _ORDINARY_RANGES)


def is_replacement_damaged(text: str) -> bool:
    """是否已含 U+FFFD —— 原始字节丢失，不可还原。"""
    return REPLACEMENT_CHAR in (text or "")


def looks_like_native_text(text: str) -> bool:
    """是否已经是正常中文文本（无需、也不应再做还原尝试）。"""
    if not text or is_replacement_damaged(text):
        return False
    if cjk_ratio(text) < _NATIVE_CJK_RATIO:
        return False
    # 还原产物常见的 CJK 兼容区/扩展区字符，出现在「正常文本」里说明判断有误
    return not any(0x3400 <= ord(char) <= 0x4DBF or 0xF900 <= ord(char) <= 0xFAFF for char in text)


def _decode_candidates(raw_bytes: bytes) -> tuple[str, ...]:
    """用各条恢复链路的真实编码解码字节，返回候选结果。"""
    candidates: list[str] = []
    for _, real_encoding in _RESTORE_CHAINS:
        try:
            candidates.append(raw_bytes.decode(real_encoding))
        except (UnicodeDecodeError, LookupError):
            continue
    return tuple(candidates)


def _is_plausible_restore(original: str, candidate: str) -> bool:
    """还原结果是否可信：必须变成中文，且中文占比要提升。"""
    if not candidate or candidate == original:
        return False
    if is_replacement_damaged(candidate):
        return False
    if cjk_ratio(candidate) < _NATIVE_CJK_RATIO:
        return False
    # 还原必须「净增」中文字符，避免把本就正常的多语言文本改坏
    return sum(1 for char in candidate if is_cjk(char)) > sum(
        1 for char in original if is_cjk(char)
    )


def restore_text(text: str) -> RestoreResult | None:
    """尝试无损还原；不可还原时返回 ``None``。"""
    if not text or is_replacement_damaged(text):
        return None
    if looks_like_native_text(text):
        return None
    for wrong_encoding, real_encoding in _RESTORE_CHAINS:
        try:
            raw_bytes = text.encode(wrong_encoding)
        except (UnicodeEncodeError, LookupError):
            continue
        try:
            candidate = raw_bytes.decode(real_encoding)
        except (UnicodeDecodeError, LookupError):
            continue
        if _is_plausible_restore(text, candidate):
            return RestoreResult(
                chain=(wrong_encoding, real_encoding),
                restored=candidate,
                original=text,
            )
    return None


def is_repairable(text: str) -> bool:
    """是否可被 :func:`restore_text` 无损还原。"""
    return restore_text(text) is not None


def is_mojibake(text: str) -> bool:
    """是否为编码错误产物（可还原的乱码，或已丢失字节的 U+FFFD 乱码）。"""
    if not text:
        return False
    if is_replacement_damaged(text):
        return True
    return is_repairable(text)


def _restore_char(char: str) -> str | None:
    """尝试还原单个「乱码字符」，要求结果全部为 CJK，避免误还原。"""
    if is_ordinary_char(char):
        return None
    try:
        raw_bytes = char.encode("utf-8")
    except UnicodeEncodeError:
        return None
    for candidate in _decode_candidates(raw_bytes):
        if candidate == char or not candidate:
            continue
        if all(is_cjk(item) for item in candidate):
            return candidate
    return None


def partial_hint(text: str) -> str:
    """为含 U+FFFD 的乱码生成「部分还原提示」，供人工判断。

    缺口处以 :data:`HINT_FILLER` 标注；正常字符原样保留。
    无缺口时返回空串。
    """
    if not text or not is_replacement_damaged(text):
        return ""
    parts: list[str] = []
    for char in text:
        if char == REPLACEMENT_CHAR:
            parts.append(HINT_FILLER)
            continue
        if is_ordinary_char(char):
            parts.append(char)
            continue
        parts.append(_restore_char(char) or char)
    hint = "".join(parts)
    return hint if hint != text else ""
