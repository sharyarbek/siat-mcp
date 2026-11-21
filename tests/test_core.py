"""Тесты загрузчика и ядра на маленьком искусственном наборе данных.

Мы сами пишем «сырые ответы сайта» для двух индикаторов в tmp-папку, собираем из них
базу настоящим загрузчиком и проверяем функции ядра. Сайт и LLM для этого не нужны.
"""

import json
from pathlib import Path

import pytest

from siat import core, load

REGIONS = [("1700", "Республика Узбекистан", True), ("1718", "Самаркандская область", False),
           ("1726", "город Ташкент", False), ("1735", "Республика Каракалпакстан", False)]


def names(ru):
    return {"name": ru, "name_ru": ru, "name_uz": ru + " uz", "name_uzc": ru + " uzc", "name_en": ru + " en"}


def write_raw(root: Path):
    raw = root / "raw"
    tree = [{"id": 1, "code": "2.00.00", **names("Социальная статистика"), "children": [
        {"id": 2, "code": "2.05.00", **names("Труд"), "children": [
            {"id": 101, "code": "2.05.01.0001", **names("Уровень безработицы (годовой)"),
             "updated_xlsx": "2026-11-09T12:00:00+05:00", "children": []},
            {"id": 102, "code": "2.05.01.0002", **names("Объем услуг (квартальный)"),
             "updated_xlsx": "2026-10-01T12:00:00+05:00", "children": []},
        ]}]}]
    (raw).mkdir(parents=True)
    (raw / "tree.json").write_text(json.dumps(tree, ensure_ascii=False), encoding="utf-8")

    def meta(unit):
        return {"meta_data": [
            {"position": 1, "name_ru": "Единица измерения", "value_ru": unit, "value_en": unit + " (en)"},
            {"position": 2, "name_ru": "Составитель официальной статистики", "value_ru": "Статагентство"},
            {"position": 3, "name_ru": "Номер телефона :", "value_ru": "+998 71 000 00 00"},
            {"position": 4, "name_ru": "Электронная почта", "value_ru": "secret@stat.uz"},
            {"position": 5, "name_ru": "Oтветственный сотрудник ФИО", "value_ru": "Иванов Иван"},
        ]}

    # индикатор 101: годовой, регионы складываются в итог, есть ноль
    vals = {"1718": [10, 20], "1726": [30, 0], "1735": [60, 80]}
    table101 = []
    for code, ru, root_ in REGIONS:
        series = [sum(v[i] for v in vals.values()) for i in range(2)] if root_ else vals[code]
        table101.append({"id": 1, "code": code, **names(ru), "is_root": root_, "map_code": None, "parent_id": None,
                         "data": [{"2023": series[0]}, {"2024": series[1]}]})
    # индикатор 102: квартальный, нарастающим итогом, плюс одно годовое значение (смешанные типы)
    q = [("2023-Q1", 10), ("2023-Q2", 25), ("2023-Q3", 40), ("2023-Q4", 60),
         ("2024-Q1", 12), ("2024-Q2", 30), ("2024-Q3", 50), ("2024-Q4", 70), ("2022", 55)]
    table102 = [{"id": 1, "code": "1700", **names("Республика Узбекистан"), "is_root": True, "map_code": None,
                 "parent_id": None, "data": [{p: v} for p, v in q]}]

    for ind_id, table, unit in [(101, table101, "%"), (102, table102, "млрд сум")]:
        d = raw / "ind" / str(ind_id)
        d.mkdir(parents=True)
        card = {"id": ind_id, "code": f"2.05.01.000{ind_id - 100}", "period": "Yillik",
                "view_options": ["metadata", "table", "graphics", "cartogram"],
                "classifier": {"id": 1, **names("Регионы")}, "updated_xlsx": "2026-11-09T12:00:00+05:00"}
        (d / "card.json").write_text(json.dumps(card, ensure_ascii=False), encoding="utf-8")
        (d / "metadata.json").write_text(json.dumps(meta(unit), ensure_ascii=False), encoding="utf-8")
        (d / "table.json").write_text(json.dumps(table, ensure_ascii=False), encoding="utf-8")


@pytest.fixture(scope="module")
def con(tmp_path_factory):
    root = tmp_path_factory.mktemp("data")
    write_raw(root)
    info = load.build(root, root / "siat.db")
    assert info["report"]["indicators"] == 2
    c = core.connect(root / "siat.db")
    yield c
    c.close()


def test_no_pii_in_db(con):
    dump = "\n".join(str(tuple(r)) for r in con.execute("SELECT * FROM indicator_meta"))
    assert "@" not in dump and "+998" not in dump and "Иванов" not in dump
    assert "Статагентство" in dump


def test_search_handles_word_forms(con):
    # в названии «безработицы», ищем «безработица» — триграммы + отрезанное окончание
    r = core.search(con, "безработица")
    assert [x["id"] for x in r["results"]] == [101]
    assert core.search(con, "2.05.01.0002")["results"][0]["id"] == 102  # по коду
    assert core.search(con, "абракадабра")["results"] == []


def test_get_data_filters_and_source(con):
    r = core.get_data(con, "101", items=["Самарканд"], period_from="2024")
    assert r["periods"] == ["2024"] and r["rows"][0]["values"] == [20]
    assert r["source"]["url"] == "https://siat.stat.uz/data/101/?lang=ru"
    assert r["source"]["views"]["cartogram"] == "https://siat.stat.uz/reports-filed/101/map-data"
    assert r["source"]["source_updated"] == "2026-11-09"
    assert "2.05.01.0001" in r["source"]["citation"]


def test_zero_warning(con):
    r = core.get_data(con, "101", period_from="2024")
    assert any("нет данных" in w for w in r["warnings"])


def test_mixed_period_types_and_cumulative(con):
    r = core.get_data(con, "102")
    assert r["period_type"] == "Q"  # большинство — кварталы
    assert any("смешаны" in w for w in r["warnings"])
    assert any("нарастающим итогом" in w for w in r["warnings"])
    assert core.get_data(con, "102", period_type="Y")["periods"] == ["2022"]


def test_rank_with_shares(con):
    r = core.rank_items(con, "101", period="2023")
    assert [x["code"] for x in r["ranking"]] == ["1735", "1726", "1718"]
    assert r["ranking"][0]["share_of_total_pct"] == 60.0  # 60 из 100


def test_compute_change(con):
    r = core.compute_change(con, "101", "Каракалпак", "2023", "2024")
    assert (r["from"]["value"], r["to"]["value"], r["absolute_change"]) == (60, 80, 20)
    assert r["percent_change"] == 33.33


def test_errors_are_helpful(con):
    with pytest.raises(core.NotFound, match="search_indicators"):
        core.get_indicator(con, "999")
    with pytest.raises(ValueError, match="2025-Q3"):
        core.get_data(con, "101", period_from="вчера")
