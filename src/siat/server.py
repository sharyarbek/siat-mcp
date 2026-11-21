"""MCP-сервер SIAT: официальная статистика Узбекистана (siat.stat.uz) для LLM.

Как это работает:
- MCPServer — объект сервера из официального SDK `mcp`.
- Декоратор @mcp.tool() превращает обычную функцию в «инструмент», который может
  вызывать модель: имя функции = имя инструмента, docstring = описание для модели,
  аннотации типов (+ Field(description=...)) = JSON-схема параметров.
- Вся логика — в core.py; здесь только тонкие обёртки.
- Транспорт stdio: клиент (Claude Code) запускает этот модуль как подпроцесс и общается
  через stdin/stdout. ПОЭТОМУ НИКОГДА НЕ ПИШЕМ В STDOUT (print) — это сломает протокол.
  Логи — только в stderr.
- Соединение с базой открывается на каждый вызов и сразу закрывается: так загрузчик
  может подменить siat.db, пока сервер запущен (на Windows открытый файл подменить нельзя).
"""

from __future__ import annotations

import logging
import sys
from contextlib import closing
from typing import Annotated, Literal

import json

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ResourceNotFoundError, ToolError
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import Field

from siat import core

sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # кириллица в логах на Windows; stdout — для протокола
logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("siat")

Lang = Annotated[Literal["ru", "uz", "uzc", "en"],
                 Field(description="Язык названий и ссылок: ru, uz (латиница), uzc (кириллица), en")]
Indicator = Annotated[str, Field(description="id индикатора (например 2943) или его код (например 1.03.01.0005)")]
PeriodStr = Annotated[str, Field(description="Период: 2025 (год), 2025-Q3 (квартал) или 2025-M09 (месяц)")]

INSTRUCTIONS = """\
Официальная статистика Узбекистана с портала SIAT (siat.stat.uz): 3000+ индикаторов
(экономика, население, социальная сфера, экология) по регионам, отраслям, странам, продуктам и т.д.

Как работать:
1. Найди индикатор: search_indicators (или browse_catalog для навигации по разделам).
2. Посмотри get_indicator: единица измерения, периоды, элементы разреза, предупреждения.
3. Возьми числа: get_data / rank_items / compute_change. Не считай сам то, что умеют инструменты.
Правила ответа:
- Числа бери только из ответов инструментов, не округляй без нужды, указывай единицу измерения.
- Под ответом ОБЯЗАТЕЛЬНО приведи source.citation дословно (это ссылка на источник).
- Учитывай warnings: 0 может означать «нет данных»; квартальные/месячные ряды бывают нарастающим итогом.
- Если данных нет — так и скажи, не выдумывай.
"""

mcp = MCPServer("siat", title="SIAT — статистика Узбекистана", instructions=INSTRUCTIONS, version="0.2.0")
RO = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)


def _run(fn, *args) -> CallToolResult:
    """Вызвать функцию ядра и упаковать результат.

    - Успех: компактный JSON в content (его читает модель — меньше токенов) + тот же объект
      в structured_content (для программ).
    - Ожидаемая ошибка (не найдено, неверный период…): ToolError — SDK передаст модели наш
      текст с подсказкой, что делать дальше. Любая другая ошибка — это баг: модель увидит
      только общее сообщение, а трассировка уйдёт в лог (stderr).
    """
    try:
        with closing(core.connect()) as con:
            result = fn(con, *args)
    except (ValueError, FileNotFoundError) as e:  # NotFound — подкласс ValueError
        raise ToolError(str(e)) from e
    text = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    return CallToolResult(content=[TextContent(type="text", text=text)], structured_content=result)


@mcp.tool(annotations=RO)
def search_indicators(
    query: Annotated[str, Field(description="Что ищем, на любом языке: «безработица», «aholi soni», «GDP per capita»")],
    lang: Lang = "ru",
    section_code: Annotated[str | None, Field(description="Ограничить разделом, например 1.03.00 (сельское хозяйство)")] = None,
    periodicity: Annotated[Literal["year", "quarter", "month"] | None, Field(description="Только ряды с такой периодичностью")] = None,
    limit: Annotated[int, Field(ge=1, le=20)] = 10,
) -> CallToolResult:
    """Найти статистические индикаторы SIAT по ключевым словам (поиск по названиям на 4 языках,
    пути в каталоге, единицам и элементам разреза). Возвращает карточки без чисел: id, код, название,
    единицу, периодичность, вид разреза и диапазон периодов. Если ничего не нашлось — попробуй синонимы
    или другой язык."""
    return _run(core.search, query, lang, section_code, periodicity, limit)


@mcp.tool(annotations=RO)
def browse_catalog(
    code: Annotated[str | None, Field(description="Код раздела (например 1.01.00); пусто — корень каталога")] = None,
    lang: Lang = "ru",
) -> CallToolResult:
    """Навигация по дереву каталога SIAT: показывает подразделы или индикаторы внутри раздела."""
    return _run(core.browse, code, lang)


@mcp.tool(annotations=RO)
def get_indicator(indicator: Indicator, lang: Lang = "ru") -> CallToolResult:
    """Карточка индикатора: единица измерения, периодичность, методика, примечания, вид разреза и его
    элементы (регионы/отрасли/страны…), доступные периоды, предупреждения о качестве данных и источник
    со ссылками на таблицу и визуализации на сайте."""
    return _run(core.get_indicator, indicator, lang)


@mcp.tool(annotations=RO)
def get_data(
    indicator: Indicator,
    items: Annotated[list[str] | None, Field(description="Элементы разреза: коды (1718) или названия («Самарканд»). Пусто — все")] = None,
    period_from: Annotated[str | None, Field(description="С какого периода: 2020, 2020-Q1, 2020-M01")] = None,
    period_to: Annotated[str | None, Field(description="По какой период включительно")] = None,
    period_type: Annotated[Literal["Y", "Q", "M"] | None, Field(description="Y=годы, Q=кварталы, M=месяцы (если в ряду смешаны)")] = None,
    lang: Lang = "ru",
) -> CallToolResult:
    """Значения индикатора: таблица «элемент разреза × период». Не больше 500 значений за раз —
    если больше, ответ помечен truncated=true и содержит последние периоды (сузь запрос)."""
    return _run(core.get_data, indicator, items, period_from, period_to, period_type, lang)


@mcp.tool(annotations=RO)
def rank_items(
    indicator: Indicator,
    period: Annotated[str | None, Field(description="Период (2025, 2025-Q3…); пусто — последний доступный")] = None,
    order: Literal["desc", "asc"] = "desc",
    top: Annotated[int | None, Field(ge=1, le=100, description="Сколько первых мест вернуть")] = None,
    lang: Lang = "ru",
) -> CallToolResult:
    """Рейтинг элементов разреза (регионов, отраслей, стран…) по значению за период. Доля от итога
    считается автоматически, только если элементы действительно складываются в итоговую строку."""
    return _run(core.rank_items, indicator, period, order, top, lang)


@mcp.tool(annotations=RO)
def compute_change(
    indicator: Indicator,
    period_from: PeriodStr,
    period_to: PeriodStr,
    item: Annotated[str | None, Field(description="Элемент разреза (код или название); пусто — итоговая строка")] = None,
    lang: Lang = "ru",
) -> CallToolResult:
    """Изменение значения между двумя периодами: абсолютное, в процентах и среднегодовой темп (CAGR,
    для годовых рядов). Считает код, а не модель."""
    return _run(core.compute_change, indicator, item, period_from, period_to, lang)


@mcp.resource("siat://indicator/{indicator_id}", mime_type="application/json")
def indicator_resource(indicator_id: str) -> str:
    """Карточка индикатора SIAT (то же, что get_indicator, на русском)."""
    try:
        with closing(core.connect()) as con:
            return json.dumps(core.get_indicator(con, indicator_id, "ru"), ensure_ascii=False)
    except (ValueError, FileNotFoundError) as e:
        raise ResourceNotFoundError(str(e)) from e


@mcp.prompt()
def regional_comparison(topic: str, year: str = "") -> str:
    """Сравнить регионы Узбекистана по теме."""
    return (f"Сравни регионы Узбекистана по теме «{topic}»{' за ' + year if year else ' за последний доступный год'}. "
            "Найди подходящий индикатор в SIAT, построй рейтинг (rank_items), прокомментируй лидеров и отстающих, "
            "укажи единицы измерения и обязательно приведи citation источника.")


@mcp.prompt()
def trend_analysis(topic: str, region: str = "Республика Узбекистан") -> str:
    """Проанализировать динамику показателя."""
    return (f"Проанализируй динамику показателя «{topic}» для «{region}» по данным SIAT: найди индикатор, возьми ряд "
            "(get_data), посчитай изменение за весь период и за последний год (compute_change), учти предупреждения "
            "(нарастающий итог, нули) и приведи citation источника.")


def main() -> None:
    mcp.run("stdio")


if __name__ == "__main__":
    main()
