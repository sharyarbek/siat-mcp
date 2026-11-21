"""Загрузчик (Loader): сырые файлы data/raw → база data/siat.db.

База каждый раз собирается заново во временный файл, а потом подменяет старую.
Поэтому загрузка идемпотентна: запустил дважды — получил одно и то же,
а MCP-сервер никогда не увидит наполовину собранную базу.

Запуск:  uv run siat-load
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from siat.meta_keys import normalize_metadata
from siat.periods import parse_period
from siat.textnorm import norm

log = logging.getLogger("siat.load")

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
SCHEMA = Path(__file__).with_name("schema.sql")
LANGS = ("uz", "uzc", "ru", "en")


# ---------- эвристика «нарастающий итог» ----------

def cumulative_likely(series: list[tuple[str, str, float]]) -> bool:
    """series: [(period_type, period_raw, value)] одной строки таблицы, отсортировано по времени.

    Квартальный/месячный ряд считаем «похоже, нарастающим итогом», если в большинстве лет
    значения внутри года не убывают (и заметно растут), а в начале следующего года падают.
    Это эвристика — отдаём её как предупреждение, а не как факт.
    """
    by_year: dict[str, list[float]] = defaultdict(list)
    for ptype, raw, value in series:
        if ptype in ("Q", "M") and value is not None:
            by_year[raw[:4]].append(value)
    years = [v for _, v in sorted(by_year.items()) if len(v) >= 3]
    if len(years) < 2:
        return False
    rising = sum(1 for v in years if all(b >= a for a, b in zip(v, v[1:])) and v[-1] > 1.5 * v[0] > 0)
    resets = sum(1 for prev, cur in zip(years, years[1:]) if cur[0] < 0.7 * prev[-1])
    return rising / len(years) >= 0.7 and resets >= 1


# ---------- чтение raw ----------

def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def walk_tree(tree: list[dict]):
    """(node, parent_id, level, path_names_ru) для всех узлов дерева."""
    stack = [(n, None, 0, []) for n in reversed(tree)]
    while stack:
        node, parent, level, path = stack.pop()
        yield node, parent, level, path
        for child in reversed(node.get("children") or []):
            stack.append((child, node["id"], level + 1, path + [node]))


def build(data_dir: Path, db_path: Path) -> dict:
    raw = data_dir / "raw"
    tmp = db_path.with_suffix(".tmp")
    if tmp.exists():
        tmp.unlink()
    con = sqlite3.connect(tmp)
    con.executescript(SCHEMA.read_text(encoding="utf-8"))

    fetched_at = {}
    crawl_db = data_dir / "crawl.sqlite"
    if crawl_db.exists():
        c2 = sqlite3.connect(crawl_db)
        fetched_at = dict(c2.execute(
            "SELECT indicator_id, MAX(fetched_at) FROM fetch WHERE status = 200 GROUP BY indicator_id"))
        c2.close()

    report = Counter()
    dropped_meta = Counter()
    classifiers: dict[int, dict] = {}

    tree = read_json(raw / "tree.json")
    leaves = []
    for node, parent, level, path in walk_tree(tree):
        is_leaf = not node.get("children")
        con.execute("INSERT INTO node VALUES (?,?,?,?,?,?,?,?,?,?)", (
            node["id"], parent, node.get("code"), level, int(is_leaf), node.get("position"),
            *(node.get(f"name_{lg}") for lg in LANGS)))
        if is_leaf:
            leaves.append((node, path))
    report["nodes"] = sum(1 for _ in walk_tree(tree))
    report["leaves_in_tree"] = len(leaves)

    for node, path in leaves:
        ind_id = node["id"]
        d = raw / "ind" / str(ind_id)
        if not all((d / f"{ep}.json").exists() for ep in ("card", "metadata", "table")):
            report["skipped_not_downloaded"] += 1
            continue
        card, meta, table = read_json(d / "card.json"), read_json(d / "metadata.json"), read_json(d / "table.json")

        cl = card.get("classifier") or {}
        if cl.get("id") is not None:
            classifiers[cl["id"]] = cl

        # метаданные: только белый список ключей
        meta_rows, dropped = normalize_metadata(meta.get("meta_data") or [])
        dropped_meta.update(dropped)
        con.executemany("INSERT INTO indicator_meta VALUES (?,?,?,?)",
                        [(ind_id, k, lg, v) for (k, lg), v in meta_rows.items()])

        # строки таблицы (элементы разреза) и значения
        obs = []
        ptypes = set()
        periods = []
        has_zero = False
        cumul_votes = []
        for pos, row in enumerate(table):
            con.execute("INSERT OR IGNORE INTO item VALUES (?,?,?,?,?,?,?,?)", (
                ind_id, row["code"], pos, int(bool(row.get("is_root"))),
                *(row.get(f"name_{lg}") for lg in LANGS)))
            series = []
            for cell in row.get("data") or []:
                for raw_period, value in cell.items():
                    p = parse_period(raw_period)
                    if p.type == "X":
                        report["periods_unknown_format"] += 1
                    if isinstance(value, bool) or not isinstance(value, (int, float, type(None))):
                        report["values_non_numeric"] += 1
                        value = None
                    has_zero |= value == 0
                    ptypes.add(p.type)
                    periods.append((p.start.isoformat() if p.start else "9999", raw_period))
                    obs.append((ind_id, row["code"], raw_period, p.type,
                                p.start.isoformat() if p.start else None,
                                p.end.isoformat() if p.end else None, value))
                    series.append((p.type, raw_period, value, p.start))
            series.sort(key=lambda s: s[3].isoformat() if s[3] else "9999")
            if row.get("is_root") or pos == 0:
                cumul_votes.append(cumulative_likely([(t, r, v) for t, r, v, _ in series]))
        con.executemany("INSERT OR IGNORE INTO observation VALUES (?,?,?,?,?,?,?)", obs)
        periods.sort()

        con.execute("INSERT INTO indicator VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            ind_id, card.get("code") or node.get("code"), cl.get("id"), card.get("period"),
            json.dumps(card.get("view_options") or []), card.get("updated_xlsx") or node.get("updated_xlsx"),
            len(table), len(obs), "".join(sorted(ptypes)),
            periods[0][1] if periods else None, periods[-1][1] if periods else None,
            int(not obs), int(has_zero), int(any(cumul_votes)), fetched_at.get(ind_id)))

        # поисковый индекс (нормализованные тексты)
        unit = " ".join(v for (k, _), v in meta_rows.items() if k == "unit")
        extra = " ".join(filter(None, [
            node.get("code"), unit,
            *(cl.get(f"name_{lg}") for lg in LANGS),
            " ".join(r.get("name_ru") or "" for r in table[:40]),  # названия элементов разреза
        ]))
        path_text = " / ".join(p.get("name_ru") or "" for p in path) + " " + \
                    " / ".join(p.get("name_uz") or "" for p in path)
        con.execute("INSERT INTO catalog_fts VALUES (?,?,?,?,?,?,?)", (
            ind_id, *(norm(node.get(f"name_{lg}")) for lg in ("ru", "uz", "uzc", "en")),
            norm(path_text), norm(extra)))

        report["indicators"] += 1
        report["observations"] += len(obs)
        report["empty_tables"] += int(not obs)
        report["cumulative_likely"] += int(any(cumul_votes))

    con.executemany("INSERT INTO classifier VALUES (?,?,?,?,?)",
                    [(cid, *(c.get(f"name_{lg}") for lg in LANGS)) for cid, c in classifiers.items()])

    snapshot = max(fetched_at.values()) if fetched_at else None
    info = {
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "snapshot_date": snapshot[:10] if snapshot else None,
        "report": dict(report),
        "metadata_dropped": dict(dropped_meta),
    }
    con.executemany("INSERT INTO load_info VALUES (?,?)",
                    [(k, json.dumps(v, ensure_ascii=False)) for k, v in info.items()])
    con.commit()
    con.execute("VACUUM")
    con.close()
    os.replace(tmp, db_path)  # атомарная подмена
    return info


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Собрать siat.db из сырых файлов")
    ap.add_argument("--data-dir", type=Path, default=DATA_DIR)
    args = ap.parse_args()
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    db = args.data_dir / "siat.db"
    info = build(args.data_dir, db)
    log.info("готово: %s (%.1f МБ)", db, db.stat().st_size / 1e6)
    print(json.dumps(info, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
