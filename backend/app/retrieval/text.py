from __future__ import annotations

import re


_WORD_RE = re.compile(r"[a-z0-9_+#.-]{2,}")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def tokenize_terms(text: str) -> list[str]:
    """Split text into English word tokens, single CJK characters and CJK bigrams."""
    normalized = text.lower()
    tokens: list[str] = _WORD_RE.findall(normalized)
    cjk = _CJK_RE.findall(normalized)
    tokens.extend(cjk)
    cjk_text = "".join(cjk)
    tokens.extend(cjk_text[index : index + 2] for index in range(max(0, len(cjk_text) - 1)))
    return tokens
