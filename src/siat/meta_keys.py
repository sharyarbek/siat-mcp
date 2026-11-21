"""Нормализация ключей метаданных SIAT.

Проблема: одно и то же поле на сайте подписано по-разному («Номер телефона :»,
«Номер телефона:», «Для общения»…), технический ключ key_name бывает пустым или
на узбекском, а у отдельных индикаторов поля вообще сдвинуты.

Решение: определяем ключ по русской подписи (name_ru) набором правил.
Правила проверяются по порядку, срабатывает первое.

Безопасность: сохраняем ТОЛЬКО ключи из белого списка KEEP. Всё, что похоже на
персональные данные (телефон, e-mail, ФИО сотрудника), и всё нераспознанное —
отбрасывается. Так ПДн не попадут ни в базу, ни в ответы модели.
"""

from __future__ import annotations

import re

from siat.textnorm import norm

# (ключ, регулярное выражение по нормализованной подписи).
# Подписи в основном русские, но изредка в name_ru лежит узбекский текст — для него вторая ветка «|…».
_RULES: list[tuple[str, re.Pattern]] = [
    ("pii", re.compile(r"телефон|для общения|электронн\w* почт|e-?mail|сотрудник|ф\.?и\.?о"
                       r"|telefon|elektron|mas'ul (xodim|hodim)|f\.?i\.?(o|sh)")),
    ("unit", re.compile(r"единиц\w* измерен|o'lchov birlig")),
    ("periodicity", re.compile(r"периодичн|частота|davriylig")),
    ("methodology_url", re.compile(r"ссылка на методич")),
    ("method_short", re.compile(r"метод\w*.*(кратк|сокращ)")),
    ("methodology", re.compile(r"метод|uslubiyat|hisoblash")),
    ("note", re.compile(r"примечан|^izoh")),
    ("info_source", re.compile(r"источник|manba")),
    ("org_name", re.compile(r"составитель официальн|организаци\w*,? осуществивш")),
    ("classifiers", re.compile(r"классификатор|tasniflagich|klassifikator")),
    ("department", re.compile(r"ответственное управлен")),
    ("indicator_name", re.compile(r"^имя (индикатора|набора)")),
    ("indicator_code", re.compile(r"идентификационн")),
    ("first_published", re.compile(r"дата первой публикац")),
    ("last_modified", re.compile(r"дата последнего изменен")),
]

KEEP = {
    "unit", "periodicity", "methodology", "method_short", "methodology_url", "note",
    "info_source", "org_name", "classifiers", "department", "first_published", "last_modified",
}
# indicator_name и indicator_code не храним: они уже есть в node/indicator.

LANGS = ("uz", "uzc", "ru", "en")


def classify(name_ru: str | None) -> str:
    """Русская подпись поля → нормализованный ключ ('other', если не распознали)."""
    s = norm(name_ru)
    # латинская «o» в начале русского слова встречается вместо кириллической («Oтветственный»)
    s = re.sub(r"^o(?=[а-я])", "о", s)
    for key, rx in _RULES:
        if rx.search(s):
            return key
    return "other"


def normalize_metadata(meta_data: list[dict]) -> tuple[dict[tuple[str, str], str], dict[str, int]]:
    """Список полей из /metadata/ → ({(key, lang): value}, статистика отброшенного).

    Несколько полей с одним ключом (например «Примечание-1…12») склеиваются по порядку.
    """
    out: dict[tuple[str, str], list[str]] = {}
    dropped: dict[str, int] = {}
    for field in sorted(meta_data, key=lambda f: f.get("position") or 0):
        key = classify(field.get("name_ru"))
        if key not in KEEP:
            dropped[key] = dropped.get(key, 0) + 1
            continue
        for lang in LANGS:
            val = (field.get(f"value_{lang}") or "").strip()
            if val:
                out.setdefault((key, lang), [])
                if val not in out[(key, lang)]:
                    out[(key, lang)].append(val)
    return {k: "\n".join(v) for k, v in out.items()}, dropped
