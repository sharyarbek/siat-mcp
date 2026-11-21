"""Нормализация текста для поиска.

Одинаково применяется к тексту в индексе и к запросу пользователя, чтобы мелочи
написания не мешали найти индикатор:
- нижний регистр;
- все виды апострофов узбекской латиницы (‘ ’ ʼ ʻ ` ´ ') → один символ ';
- ё → е;
- пробелы схлопываются.
"""

import re

_APOSTROPHES = str.maketrans({c: "'" for c in "‘’ʼʻ`´′"})
_WS = re.compile(r"\s+")


def norm(text: str | None) -> str:
    if not text:
        return ""
    t = text.translate(_APOSTROPHES).lower().replace("ё", "е")
    return _WS.sub(" ", t).strip()
