from siat.meta_keys import classify, normalize_metadata


def test_russian_labels():
    assert classify("Единица измерения") == "unit"
    assert classify("Периодичность") == "periodicity"
    assert classify("Методы расчета") == "methodology"
    assert classify("Методология расчета (кратко)") == "method_short"
    assert classify("Методы расчета (сокращенно)") == "method_short"
    assert classify("Ссылка на методический документ") == "methodology_url"
    assert classify("Примечание-3") == "note"
    assert classify("Первичный источник информации") == "info_source"
    assert classify("Составитель официальной статистики") == "org_name"
    assert classify("Название организации, осуществивший расчёт") == "org_name"
    assert classify("Ответственное управление") == "department"


def test_uzbek_labels_in_name_ru():
    assert classify("O‘lchov birligi") == "unit"
    assert classify("Izoh-1") == "note"


def test_pii_is_detected():
    for label in ["Номер телефона :", "Номер телефона:", "Для общения", "Электронная почта",
                  "Oтветственный сотрудник ФИО", "Ответственный сотрудник Ф.И.О", "Mas'ul xodim FISh"]:
        assert classify(label) == "pii", label


def test_unknown_is_other():
    assert classify("Рахимов Алишер Абдуллаевич") == "other"
    assert classify("+998 71 000 00 00") == "other"


def test_normalize_keeps_whitelist_only_and_merges_notes():
    fields = [
        {"position": 1, "name_ru": "Единица измерения", "value_ru": "млрд сум", "value_en": "bln soums"},
        {"position": 2, "name_ru": "Номер телефона:", "value_ru": "+998 71 000 00 00"},
        {"position": 3, "name_ru": "Примечание-1", "value_ru": "первое"},
        {"position": 4, "name_ru": "Примечание-2", "value_ru": "второе"},
        {"position": 5, "name_ru": "Электронная почта", "value_ru": "x@stat.uz"},
    ]
    rows, dropped = normalize_metadata(fields)
    assert rows[("unit", "ru")] == "млрд сум" and rows[("unit", "en")] == "bln soums"
    assert rows[("note", "ru")] == "первое\nвторое"
    assert dropped == {"pii": 2}
    assert not any("@" in v or "+998" in v for v in rows.values())
