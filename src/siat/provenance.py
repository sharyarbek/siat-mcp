"""Блок «источник» (provenance) для каждого ответа с данными.

Собирается КОДОМ из базы, а не моделью, поэтому модель не может «придумать» ссылку.
- url: статическая страница сайта с таблицей на нужном языке — лучшая ссылка для цитаты;
- views: ссылки на каждый вид визуализации этого индикатора на сайте
  (шаблон URL взят из JS-кода siat.stat.uz);
- citation: готовая строка, которую модель вставляет под ответом как есть.
"""

from __future__ import annotations

import json

SITE = "https://siat.stat.uz"

# view_options из карточки индикатора → slug в URL интерфейса сайта
VIEW_SLUGS = {
    "metadata": "meta-data",
    "table": "table-data",
    "graphics": "line-data",
    "gistogram": "bar-data",
    "cartogram": "map-data",
    "rating": "rate-data",
    "compare_regions": "compare-data",
    "comparative_ratio": "relationship-data",
    "customization": "custom-data",
}

_CITATION = {
    "ru": "Источник: SIAT (siat.stat.uz), «{name}», код {code}, данные обновлены {date}. {url}",
    "uz": "Manba: SIAT (siat.stat.uz), «{name}», kod {code}, ma'lumotlar yangilangan sana {date}. {url}",
    "uzc": "Манба: SIAT (siat.stat.uz), «{name}», код {code}, маълумотлар янгиланган сана {date}. {url}",
    "en": "Source: SIAT (siat.stat.uz), \"{name}\", code {code}, data updated {date}. {url}",
}


def build_source(*, indicator_id: int, code: str, name: str, unit: str | None, publisher: str | None,
                 primary_source: str | None, view_options_json: str | None, updated_xlsx: str | None,
                 snapshot_date: str | None, lang: str) -> dict:
    url = f"{SITE}/data/{indicator_id}/?lang={lang}"
    views = {}
    for opt in json.loads(view_options_json or "[]"):
        slug = VIEW_SLUGS.get(opt)
        if slug:
            views[opt] = f"{SITE}/reports-filed/{indicator_id}/{slug}"
    date = (updated_xlsx or "")[:10] or None
    return {
        "publisher": publisher,
        "primary_source": primary_source,
        "indicator_code": code,
        "indicator_name": name,
        "unit": unit,
        "url": url,
        "views": views,
        "api": f"{SITE}/api/sdmx/{indicator_id}/table/",
        "source_updated": date,
        "snapshot_date": snapshot_date,
        "citation": _CITATION.get(lang, _CITATION["ru"]).format(name=name, code=code, date=date or "?", url=url),
    }
