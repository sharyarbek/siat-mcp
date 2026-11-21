"""Проверка качества поиска: для каждого запроса — есть ли нужный индикатор в top-5.

«Нужный» = название (на русском) содержит все ожидаемые фрагменты. Запросы специально
разные: на русском, узбекском и английском, в разных падежах, разговорные формулировки.

Запуск:  uv run python eval/search_check.py
"""

import sys

from siat import core

# (запрос, язык поиска, фрагменты, которые должны быть в name_ru найденного индикатора)
CASES = [
    ("валовой региональный продукт", "ru", ["регионального", "продукта"]),
    ("ВРП на душу населения", "ru", ["регионального", "на душу"]),
    ("безработица", "ru", ["безработ"]),
    ("численность населения", "ru", ["численность", "населения"]),
    ("рождаемость", "ru", ["рождаем"]),
    ("смертность", "ru", ["смертн"]),
    ("браки", "ru", ["брак"]),
    ("экспорт товаров", "ru", ["экспорт"]),
    ("импорт", "ru", ["импорт"]),
    ("инфляция индекс потребительских цен", "ru", ["индекс", "цен"]),
    ("производство пшеницы", "ru", ["пшениц"]),
    ("розничный товарооборот", "ru", ["розничн"]),
    ("инвестиции в основной капитал", "ru", ["инвестиц"]),
    ("количество студентов", "ru", ["студент"]),
    ("средняя заработная плата", "ru", ["заработн"]),
    ("aholi soni", "uz", ["численность постоянного населения"]),
    ("ishsizlik darajasi", "uz", ["безработ"]),
    ("eksport", "uz", ["экспорт"]),
    ("gross domestic product", "en", ["валов", "продукт"]),
    ("ВВП", "ru", ["валов", "внутренн"]),
    ("number of hospitals", "en", ["больнич"]),  # «Число больничных учреждений»
]


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    hits1 = hits5 = 0
    with core.connect() as con:
        for query, lang, expect in CASES:
            res = core.search(con, query, lang=lang, limit=5)["results"]
            names = [con.execute("SELECT name_ru FROM node WHERE id=?", (r["id"],)).fetchone()[0] or "" for r in res]
            ok = [all(e.lower() in n.lower() for e in expect) for n in names]
            rank = ok.index(True) + 1 if any(ok) else None
            hits1 += rank == 1
            hits5 += rank is not None
            mark = "OK " if rank else "MISS"
            print(f"{mark} [{lang}] {query!r:42} rank={rank}  top1={names[0][:60] if names else '—'}")
    n = len(CASES)
    print(f"\nTop-1: {hits1}/{n} = {100 * hits1 / n:.0f}%   Top-5: {hits5}/{n} = {100 * hits5 / n:.0f}%")


if __name__ == "__main__":
    main()
