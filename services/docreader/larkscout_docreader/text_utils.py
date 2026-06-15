"""Chinese amount and company-name text normalization.

Pure leaf: regex/string helpers that normalize extracted document text —
RMB amount uppercasing, amount-phrase canonicalization, signature/watermark
noise removal, and company-name alias collapsing. No dependency on the package
__init__ or models, so no circular import. The OCR-text cleanup that is coupled
to the table/heading classifiers stays in the package.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

_COMPANY_NAME_RE = re.compile(r"[A-Za-z0-9\u4e00-\u9fff()（）·]+(?:股份有限公司|有限责任公司|有限公司)")
_UPPER_AMOUNT_RE = re.compile(
    r"(¥\s*[\d,]+(?:\.\d+)?)\s*[（(]\s*大写[：:]\s*人民币\s*([零〇一二三四五六七八九十百千万亿壹贰叁肆伍陆柒捌玖拾佰仟萬億元角分整正]+)\s*[)）]"
)


def _amount_to_uppercase_rmb(amount_text: str) -> str | None:
    digits = amount_text.replace("¥", "").replace(",", "").strip()
    if not re.fullmatch(r"\d+(?:\.\d{1,2})?", digits):
        return None

    value = round(float(digits) + 1e-9, 2)
    integer = int(value)
    jiao = int((value * 10) % 10)
    fen = int(round(value * 100)) % 10

    digits_map = "零壹贰叁肆伍陆柒捌玖"
    small_units = ["", "拾", "佰", "仟"]
    large_units = ["", "万", "亿", "兆"]

    if integer == 0:
        integer_text = "零元"
    else:
        groups: list[int] = []
        while integer > 0:
            groups.append(integer % 10000)
            integer //= 10000
        if len(groups) > len(large_units):
            # Beyond 兆 (>= 1e16): not representable with our unit table. Return
            # None (an absurd amount for a real document) rather than IndexError.
            return None

        parts: list[str] = []
        zero_between = False
        for idx in range(len(groups) - 1, -1, -1):
            group = groups[idx]
            if group == 0:
                zero_between = bool(parts)
                continue

            if zero_between or (parts and group < 1000):
                parts.append("零")
                zero_between = False

            group_digits: list[str] = []
            zero_inside = False
            for pos in range(3, -1, -1):
                divisor = 10**pos
                digit = group // divisor
                group %= divisor
                if digit == 0:
                    if group_digits:
                        zero_inside = True
                    continue
                if zero_inside:
                    group_digits.append("零")
                    zero_inside = False
                group_digits.append(digits_map[digit] + small_units[pos])

            parts.append("".join(group_digits) + large_units[idx])

        integer_text = "".join(parts) + "元"

    if jiao == 0 and fen == 0:
        return integer_text + "整"

    tail = ""
    if jiao > 0:
        tail += digits_map[jiao] + "角"
    elif fen > 0:
        tail += "零"
    if fen > 0:
        tail += digits_map[fen] + "分"
    return integer_text + tail


def _normalize_amount_phrases(text: str) -> str:
    def repl(match: re.Match[str]) -> str:
        amount_text = match.group(1)
        normalized_upper = _amount_to_uppercase_rmb(amount_text)
        if not normalized_upper:
            return match.group(0)
        return f"{amount_text}（大写：人民币{normalized_upper}）"

    return _UPPER_AMOUNT_RE.sub(repl, text)


def _looks_like_signature_watermark_line(line: str) -> bool:
    compact = re.sub(r"\s+", "", line.strip())
    if not compact:
        return False
    if set(compact) <= {"万", "翼", "签"} and "万翼" in compact:
        return True
    residue = compact
    for token in ("万翼签", "万翼", "翼签"):
        residue = residue.replace(token, "")
    return not residue and len(compact) >= 2


def _cleanup_extracted_text_noise(text: str) -> str:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    cleaned: list[str] = []
    for line in lines:
        stripped = line.strip()
        if _looks_like_signature_watermark_line(stripped):
            continue
        if stripped.upper() == "TINGYUN.COM":
            continue
        cleaned.append(re.sub(r"(\d+)\s*月/个", r"\1 元/个", line.rstrip()))
    return "\n".join(cleaned).strip()


def _collect_company_names(blocks: list[str]) -> list[str]:
    names: set[str] = set()
    for block in blocks:
        for match in _COMPANY_NAME_RE.findall(block):
            names.add(match.strip())
    return sorted(names)


def _split_company_name(name: str) -> tuple[str, str]:
    for suffix in ("股份有限公司", "有限责任公司", "有限公司"):
        if name.endswith(suffix):
            return name[: -len(suffix)], suffix
    return name, ""


def _build_company_name_replacements(blocks: list[str]) -> dict[str, str]:
    names = _collect_company_names(blocks)
    replacements: dict[str, str] = {}
    for name in names:
        stem, suffix = _split_company_name(name)
        best = name
        best_score = 1
        for other in names:
            if other == name:
                continue
            other_stem, other_suffix = _split_company_name(other)
            if not suffix or suffix != other_suffix:
                continue
            if len(stem) < 2 or len(other_stem) < 2:
                continue
            if stem[-2:] != other_stem[-2:]:
                continue
            score = SequenceMatcher(None, stem, other_stem).ratio()
            if score < 0.7:
                continue
            if len(other) > len(best):
                best = other
                best_score = score
        if best != name and best_score >= 0.7:
            replacements[name] = best
    return replacements


def _apply_company_name_replacements(text: str, replacements: dict[str, str]) -> str:
    for src, dst in sorted(replacements.items(), key=lambda item: len(item[0]), reverse=True):
        pattern = re.compile(
            rf"(?<![A-Za-z0-9\u4e00-\u9fff]){re.escape(src)}(?![A-Za-z0-9\u4e00-\u9fff])"
        )
        text = pattern.sub(dst, text)
    return text

