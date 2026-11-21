"""Ядро: функции поиска, выборки и вычислений поверх siat.db.

Это обычные Python-функции, которые ничего не знают про MCP — их удобно тестировать.
MCP-сервер (server.py) просто оборачивает их в инструменты.

Принцип: все числа берутся из базы или считаются здесь, кодом. Модель только
выбирает, что вызвать, и пересказывает результат.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from pathlib import Path

from siat.periods import parse_period
from siat.provenance import build_source
from siat.textnorm import norm

DB_PATH = Path(__file__).resolve().parents[2] / "data" / "siat.db"
LANGS = ("ru", "uz", "uzc", "en")
MAX_CELLS = 500

_WARN = {
    "zeros": "В данных есть нули: на SIAT пропуски тоже отдаются как 0, поэтому 0 может означать «нет данных».",
    "cumulative": "Похоже, ряд нарастающим итогом с начала года (эвристика): значение за Q3 = январь–сентябрь. "
                  "Не складывай периоды внутри года; сравнивай одинаковые периоды разных лет.",
}


class NotFound(ValueError):
    pass


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    if not path.exists():
        raise FileNotFoundError(f"База {path} не найдена. Сначала: uv run siat-crawl && uv run siat-load")
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


def _lang(lang: str) -> str:
    if lang not in LANGS:
        raise ValueError(f'lang должен быть одним из: {", ".join(LANGS)}')
    return lang


def _name(row, lang: str, prefix: str = "name_") -> str:
    return row[f"{prefix}{lang}"] or row[f"{prefix}ru"] or row[f"{prefix}uz"] or ""


def snapshot_date(con) -> str | None:
    r = con.execute("SELECT value FROM load_info WHERE key='snapshot_date'").fetchone()
    return json.loads(r[0]) if r else None


# ---------- поиск индикатора ----------

def _resolve(con, ref: str | int) -> sqlite3.Row:
    """id (2943) или код (1.03.01.0005) → строка indicator+node."""
    ref = str(ref).strip()
    sql = "SELECT i.*, n.name_uz, n.name_uzc, n.name_ru, n.name_en, n.parent_id FROM indicator i JOIN node n USING(id) WHERE "
    row = (con.execute(sql + "i.id = ?", (int(ref),)).fetchone() if ref.isdigit()
           else con.execute(sql + "i.code = ?", (ref,)).fetchone())
    if not row:
        raise NotFound(f"Индикатор «{ref}» не найден. Используй search_indicators, чтобы найти id или код.")
    return row


def _path(con, node_id: int, lang: str) -> list[str]:
    out = []
    pid = con.execute("SELECT parent_id FROM node WHERE id=?", (node_id,)).fetchone()[0]
    while pid is not None:
        r = con.execute("SELECT * FROM node WHERE id=?", (pid,)).fetchone()
        out.append(_name(r, lang))
        pid = r["parent_id"]
    return list(reversed(out))


def _meta(con, ind_id: int, lang: str) -> dict[str, str]:
    rows = con.execute("SELECT key, lang, value FROM indicator_meta WHERE indicator_id=?", (ind_id,)).fetchall()
    out: dict[str, str] = {}
    for pref in (lang, "ru", "uz", "en", "uzc"):  # язык запроса, затем запасные
        for r in rows:
            if r["lang"] == pref and r["key"] not in out:
                out[r["key"]] = r["value"]
    return out


def _classifier(con, cid, lang) -> str | None:
    if cid is None:
        return None
    r = con.execute("SELECT * FROM classifier WHERE id=?", (cid,)).fetchone()
    return _name(r, lang) if r else None


def _source(con, ind: sqlite3.Row, lang: str, meta: dict | None = None) -> dict:
    meta = meta or _meta(con, ind["id"], lang)
    return build_source(indicator_id=ind["id"], code=ind["code"], name=_name(ind, lang), unit=meta.get("unit"),
                        publisher=meta.get("org_name"), primary_source=meta.get("info_source"),
                        view_options_json=ind["view_options"], updated_xlsx=ind["updated_xlsx"],
                        snapshot_date=snapshot_date(con), lang=lang)


# Аббревиатуры, которых нет в названиях индикаторов: раскрываем до поиска.
ABBREVIATIONS = {
    "ввп": "валовой внутренний продукт", "врп": "валовой региональный продукт",
    "ипц": "индекс потребительских цен", "снс": "система национальных счетов",
    "yaim": "yalpi ichki mahsulot", "yhm": "yalpi hududiy mahsulot",
    "gdp": "gross domestic product", "grp": "gross regional product", "cpi": "consumer price index",
}


def _expand(query: str) -> str:
    return " ".join(ABBREVIATIONS.get(w, w) or w for w in norm(query).split())


def _fts_terms(query: str) -> list[str]:
    """Слова запроса → «основы» для поиска по триграммам.

    Триграммный индекс ищет подстроку, поэтому отрезаем окончание: «безработица» → «безработи»
    найдёт и «безработицы», и «безработицей»; «браки» → «брак» найдёт «браков».
    Слова короче 3 символов триграммы не ищут.
    """
    return ['"' + s.replace('"', "") + '"' for s in _stems(query)]


def _stems(query: str) -> list[str]:
    out = []
    for w in re.findall(r"[\w']+", _expand(query)):
        if len(w) < 3:
            continue
        if len(w) >= 6:
            w = w[: max(4, len(w) - 2)]
        elif len(w) == 5:
            w = w[:4]
        out.append(w)
    return out


# Веса колонок для bm25: indicator_id, name_ru, name_uz, name_uzc, name_en, path, extra.
# Совпадение в названии важнее, чем в пути каталога или в списке регионов (extra).
_BM25 = "bm25(catalog_fts, 0, 10, 10, 10, 10, 2, 1)"


def _rerank(rows, stems: list[str]) -> list:
    """Поднять наверх индикаторы, где слова запроса стоят в названии подряд (фразой),
    затем те, где все слова есть в названии (пусть и вразбивку), затем остальные по bm25."""
    phrase = re.compile(r"\W+".join(re.escape(s) for s in stems) + r"\w*") if stems else None

    def key(r):
        names = [r["f_ru"] or "", r["f_uz"] or "", r["f_uzc"] or "", r["f_en"] or ""]
        in_phrase = phrase is not None and any(phrase.search(n) for n in names)
        all_in_name = any(all(s in n for s in stems) for n in names)
        return (not in_phrase, not all_in_name, r["score"])

    return sorted(rows, key=key)


def search(con, query: str, lang: str = "ru", section_code: str | None = None, periodicity: str | None = None,
           limit: int = 10) -> dict:
    lang = _lang(lang)
    limit = max(1, min(int(limit), 20))
    q = query.strip()
    # прямое попадание по id или коду
    if re.fullmatch(r"\d+|\d+(\.\d+)+", q):
        try:
            ind = _resolve(con, q)
            return {"query": query, "results": [_card(con, ind, lang)], "match": "exact"}
        except NotFound:
            pass
    terms = _fts_terms(q)
    if not terms:
        raise ValueError("Слишком короткий запрос: нужно хотя бы одно слово из 3+ букв.")
    filters, params = [], []
    if section_code:
        filters.append("i.code LIKE ?")
        params.append(re.sub(r"(\.00)+$", "", section_code.strip()) + ".%")  # 1.03.00 → 1.03.%
    if periodicity:
        p = {"year": "Y", "annual": "Y", "quarter": "Q", "month": "M"}.get(periodicity.lower(), periodicity.upper())
        filters.append("instr(i.period_types, ?) > 0")
        params.append(p)
    where = (" AND " + " AND ".join(filters)) if filters else ""
    # берём кандидатов с запасом (100), переупорядочиваем и обрезаем до limit
    sql = (f"SELECT i.*, n.name_uz, n.name_uzc, n.name_ru, n.name_en, n.parent_id, {_BM25} AS score, "
           f"f.name_ru AS f_ru, f.name_uz AS f_uz, f.name_uzc AS f_uzc, f.name_en AS f_en "
           f"FROM catalog_fts f JOIN indicator i ON i.id = f.indicator_id JOIN node n ON n.id = i.id "
           f"WHERE catalog_fts MATCH ? {where} ORDER BY score LIMIT 100")
    match = "all_words"
    rows = con.execute(sql, (" AND ".join(terms), *params)).fetchall()
    if not rows and len(terms) > 1:
        match = "any_word"
        rows = con.execute(sql, (" OR ".join(terms), *params)).fetchall()
    rows = _rerank(rows, _stems(q))[:limit]
    return {"query": query, "match": match if rows else "none",
            "results": [_card(con, r, lang) for r in rows],
            "hint": None if rows else "Ничего не нашлось. Попробуй синонимы, другой язык (uz/en) или browse_catalog."}


def _card(con, ind: sqlite3.Row, lang: str) -> dict:
    meta = _meta(con, ind["id"], lang)
    return {
        "id": ind["id"], "code": ind["code"], "name": _name(ind, lang),
        "path": " / ".join(_path(con, ind["id"], lang)),
        "unit": meta.get("unit"), "periodicity": meta.get("periodicity"),
        "breakdown": _classifier(con, ind["classifier_id"], lang), "n_items": ind["n_items"],
        "periods": [ind["first_period"], ind["last_period"]], "updated": (ind["updated_xlsx"] or "")[:10],
    }


# ---------- навигация по каталогу ----------

def browse(con, code: str | None = None, lang: str = "ru") -> dict:
    lang = _lang(lang)
    if code:
        parent = con.execute("SELECT * FROM node WHERE code=? AND is_leaf=0", (code,)).fetchone()
        if not parent:
            raise NotFound(f"Раздел «{code}» не найден (для индикатора используй get_indicator).")
        rows = con.execute("SELECT * FROM node WHERE parent_id=? ORDER BY code", (parent["id"],)).fetchall()
        title = _name(parent, lang)
    else:
        rows = con.execute("SELECT * FROM node WHERE parent_id IS NULL ORDER BY code").fetchall()
        title = "SIAT"
    children = []
    for r in rows:
        item = {"code": r["code"], "name": _name(r, lang), "is_indicator": bool(r["is_leaf"])}
        if r["is_leaf"]:
            item["id"] = r["id"]
        else:
            item["n_children"] = con.execute("SELECT count(*) FROM node WHERE parent_id=?", (r["id"],)).fetchone()[0]
        children.append(item)
    return {"section": title, "code": code, "children": children}


# ---------- карточка индикатора ----------

def get_indicator(con, ref: str | int, lang: str = "ru") -> dict:
    lang = _lang(lang)
    ind = _resolve(con, ref)
    meta = _meta(con, ind["id"], lang)
    items = con.execute("SELECT * FROM item WHERE indicator_id=? ORDER BY position", (ind["id"],)).fetchall()
    types = Counter(r[0] for r in con.execute(
        "SELECT period_type FROM observation WHERE indicator_id=?", (ind["id"],)))
    warnings = []
    if ind["has_zeros"]:
        warnings.append(_WARN["zeros"])
    if ind["cumulative_likely"]:
        warnings.append(_WARN["cumulative"])
    if ind["table_empty"]:
        warnings.append("Таблица на сайте пустая: данных нет.")
    return {
        "id": ind["id"], "code": ind["code"], "name": _name(ind, lang),
        "path": " / ".join(_path(con, ind["id"], lang)),
        "unit": meta.get("unit"), "periodicity": meta.get("periodicity"),
        "methodology": meta.get("method_short") or meta.get("methodology"),
        "methodology_link": meta.get("methodology_url") or (meta.get("methodology") if (meta.get("methodology") or "").startswith("http") else None),
        "note": meta.get("note"),
        "breakdown": _classifier(con, ind["classifier_id"], lang),
        "items": [{"code": r["code"], "name": _name(r, lang), "is_total": bool(r["is_root"])} for r in items[:80]],
        "items_total": len(items),
        "periods": {"first": ind["first_period"], "last": ind["last_period"],
                    "types": {k: v for k, v in types.items()}},
        "warnings": warnings,
        "source": _source(con, ind, lang, meta),
    }


# ---------- данные ----------

def _period_bound(value: str | None, end: bool) -> str | None:
    if not value:
        return None
    p = parse_period(value)
    if p.type == "X":
        raise ValueError(f"Не понимаю период «{value}». Формат: 2025, 2025-Q3 или 2025-M09.")
    return (p.end if end else p.start).isoformat()


def _match_items(con, ind_id: int, wanted: list[str] | None, lang: str) -> list[sqlite3.Row]:
    rows = con.execute("SELECT * FROM item WHERE indicator_id=? ORDER BY position", (ind_id,)).fetchall()
    if not wanted:
        return rows
    out = []
    for w in wanted:
        nw = norm(w)
        hit = [r for r in rows if r["code"] == w.strip()] or \
              [r for r in rows if any(nw and nw in norm(r[f"name_{lg}"]) for lg in LANGS)]
        if not hit:
            sample = ", ".join(f"{r['code']}={_name(r, lang)}" for r in rows[:20])
            raise NotFound(f"Элемент «{w}» не найден в разрезе. Доступно: {sample}")
        out.extend(h for h in hit if h not in out)
    return out


def _choose_type(con, ind_id: int, period_type: str | None) -> tuple[str, list[str]]:
    types = Counter(r[0] for r in con.execute(
        "SELECT period_type FROM observation WHERE indicator_id=?", (ind_id,)))
    if not types:
        raise NotFound("У индикатора нет данных (таблица на сайте пустая).")
    notes = []
    if period_type:
        period_type = period_type.upper()
        if period_type not in types:
            raise ValueError(f"Периодов типа {period_type} нет. Есть: {dict(types)} (Y=год, Q=квартал, M=месяц).")
        return period_type, notes
    chosen = types.most_common(1)[0][0]
    if len(types) > 1:
        notes.append(f"В ряду смешаны типы периодов {dict(types)}; показан тип {chosen}. "
                     f"Другой можно выбрать параметром period_type.")
    return chosen, notes


def get_data(con, ref: str | int, items: list[str] | None = None, period_from: str | None = None,
             period_to: str | None = None, period_type: str | None = None, lang: str = "ru") -> dict:
    lang = _lang(lang)
    ind = _resolve(con, ref)
    ptype, notes = _choose_type(con, ind["id"], period_type)
    rows = _match_items(con, ind["id"], items, lang)
    lo, hi = _period_bound(period_from, False), _period_bound(period_to, True)

    sql = "SELECT item_code, period, value FROM observation WHERE indicator_id=? AND period_type=?"
    params: list = [ind["id"], ptype]
    if lo:
        sql += " AND period_start >= ?"
        params.append(lo)
    if hi:
        sql += " AND period_end <= ?"
        params.append(hi)
    sql += " ORDER BY period_start"
    obs = con.execute(sql, params).fetchall()

    periods = list(dict.fromkeys(o["period"] for o in obs))
    codes = [r["code"] for r in rows]
    truncated = False
    if len(codes) * len(periods) > MAX_CELLS:
        truncated = True
        keep = max(1, MAX_CELLS // max(1, len(codes)))
        periods = periods[-keep:]  # самые свежие периоды
        notes.append(f"Ответ обрезан до {len(codes)}×{len(periods)} значений (последние периоды). "
                     f"Сузь запрос: items, period_from/period_to.")
    pset = set(periods)
    values = {(o["item_code"], o["period"]): o["value"] for o in obs if o["period"] in pset}

    meta = _meta(con, ind["id"], lang)
    warnings = notes[:]
    if ind["cumulative_likely"] and ptype in ("Q", "M"):
        warnings.append(_WARN["cumulative"])
    if any(v == 0 for v in values.values()):
        warnings.append(_WARN["zeros"])
    return {
        "indicator": {"id": ind["id"], "code": ind["code"], "name": _name(ind, lang)},
        "unit": meta.get("unit"),
        "period_type": ptype,
        "periods": periods,
        "rows": [{"code": r["code"], "name": _name(r, lang), "is_total": bool(r["is_root"]),
                  "values": [values.get((r["code"], p)) for p in periods]} for r in rows],
        "truncated": truncated,
        "warnings": warnings,
        "source": _source(con, ind, lang, meta),
    }


# ---------- вычисления ----------

def rank_items(con, ref: str | int, period: str | None = None, order: str = "desc", top: int | None = None,
               lang: str = "ru") -> dict:
    lang = _lang(lang)
    ind = _resolve(con, ref)
    if period is None:
        ptype, _ = _choose_type(con, ind["id"], None)
        period = con.execute("SELECT period FROM observation WHERE indicator_id=? AND period_type=? "
                             "ORDER BY period_start DESC LIMIT 1", (ind["id"], ptype)).fetchone()[0]
    rows = con.execute(
        "SELECT it.code, it.is_root, it.name_uz, it.name_uzc, it.name_ru, it.name_en, o.value "
        "FROM observation o JOIN item it ON it.indicator_id=o.indicator_id AND it.code=o.item_code "
        "WHERE o.indicator_id=? AND o.period=?", (ind["id"], period)).fetchall()
    if not rows:
        raise NotFound(f"Нет данных за период «{period}». Посмотри доступные периоды в get_indicator.")
    total = next((r for r in rows if r["is_root"]), None)
    parts = [r for r in rows if not r["is_root"] and r["value"] is not None]
    parts.sort(key=lambda r: r["value"], reverse=(order != "asc"))
    # Доли считаем, только если части действительно складываются в итог: сумма частей = 80–102% итога.
    # Для темпов роста, индексов и средних сумма частей во много раз больше итога — доля бессмысленна,
    # и код это определяет по самим данным. Меньше 100% бывает, когда часть итога не распределена
    # по разрезу (например, у ВРП ~12% не отнесено к регионам) — тогда честно пишем покрытие.
    coverage = (sum(r["value"] for r in parts) / total["value"]) if (total and total["value"] and parts) else None
    additive = coverage is not None and 0.8 <= coverage <= 1.02
    ranked = []
    for i, r in enumerate(parts[: top or None], 1):
        e = {"rank": i, "code": r["code"], "name": _name(r, lang), "value": r["value"]}
        if additive:
            e["share_of_total_pct"] = round(100 * r["value"] / total["value"], 2)
        ranked.append(e)
    meta = _meta(con, ind["id"], lang)
    warnings = []
    if any(r["value"] == 0 for r in parts):
        warnings.append(_WARN["zeros"])
    if additive and coverage < 0.98:
        warnings.append(f"Элементы разреза в сумме дают {coverage * 100:.1f}% итога: остальное не распределено "
                        f"по разрезу. Доли посчитаны от итоговой строки.")
    if total and not additive:
        warnings.append("Доли от итога не посчитаны: элементы не складываются в итоговую строку "
                        "(это темпы, индексы, средние или часть элементов отсутствует).")
    return {
        "indicator": {"id": ind["id"], "code": ind["code"], "name": _name(ind, lang)},
        "unit": meta.get("unit"), "period": period, "order": order,
        "total": {"code": total["code"], "name": _name(total, lang), "value": total["value"]} if total else None,
        "ranking": ranked, "n_items": len(parts), "warnings": warnings,
        "source": _source(con, ind, lang, meta),
    }


def compute_change(con, ref: str | int, item: str | None, period_from: str, period_to: str, lang: str = "ru") -> dict:
    lang = _lang(lang)
    ind = _resolve(con, ref)
    rows = _match_items(con, ind["id"], [item] if item else None, lang)
    row = rows[0] if item else next((r for r in rows if r["is_root"]), rows[0])

    def val(p):
        r = con.execute("SELECT value, period_type FROM observation WHERE indicator_id=? AND item_code=? AND period=?",
                        (ind["id"], row["code"], p)).fetchone()
        if not r:
            raise NotFound(f"Нет значения за «{p}» для «{_name(row, lang)}».")
        return r["value"], r["period_type"]

    (a, ta), (b, tb) = val(period_from), val(period_to)
    warnings = []
    if ta != tb:
        warnings.append("Сравниваются периоды разного типа (год и квартал/месяц) — результат может быть некорректным.")
    if ind["cumulative_likely"] and ta in ("Q", "M") and period_from[4:] != period_to[4:]:
        warnings.append(_WARN["cumulative"] + " Здесь сравниваются РАЗНЫЕ периоды года — вероятно, некорректно.")
    out = {"absolute_change": None if a is None or b is None else round(b - a, 6),
           "percent_change": None, "cagr_pct": None}
    if a not in (None, 0) and b is not None:
        out["percent_change"] = round(100 * (b - a) / abs(a), 2)
        years = int(period_to[:4]) - int(period_from[:4])
        if ta == tb == "Y" and years >= 2 and a > 0 and b > 0:
            out["cagr_pct"] = round(100 * ((b / a) ** (1 / years) - 1), 2)
    meta = _meta(con, ind["id"], lang)
    unit = meta.get("unit") or ""
    if "%" in unit or "темп" in norm(_name(ind, "ru")) or "индекс" in norm(_name(ind, "ru")):
        warnings.append("Показатель — проценты/темп/индекс: «percent_change» здесь — изменение самого процента, "
                        "обычно важнее absolute_change (в процентных пунктах).")
    return {
        "indicator": {"id": ind["id"], "code": ind["code"], "name": _name(ind, lang)},
        "item": {"code": row["code"], "name": _name(row, lang)},
        "unit": meta.get("unit"),
        "from": {"period": period_from, "value": a}, "to": {"period": period_to, "value": b},
        **out, "warnings": warnings, "source": _source(con, ind, lang, meta),
    }
