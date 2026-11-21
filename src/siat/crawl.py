"""Скачиватель (Collector): сохраняет сырые ответы API SIAT на диск.

Что делает:
1. Скачивает дерево каталога (/sdmx/json/) → data/raw/tree.json
2. Находит все листья дерева = индикаторы (их ~3261).
3. Для каждого индикатора сохраняет три ответа:
     /sdmx/{id}/           → card.json      (карточка: view_options, classifier, updated_xlsx…)
     /sdmx/{id}/metadata/  → metadata.json  (единицы, методика, источник…)
     /sdmx/{id}/table/     → table.json     (сами числа: регион × период)
4. Ведёт журнал в data/crawl.sqlite: что скачано, когда, хэш. Если процесс прервётся,
   повторный запуск продолжит с места остановки (уже скачанное пропускается).

Collector ничего не знает о схеме БД — он только сохраняет ответы «как есть».

Запуск:
  uv run siat-crawl --limit 100     # пилот: 100 индикаторов, равномерно по всему каталогу
  uv run siat-crawl                 # всё
  uv run siat-crawl --ids 2943 3664 # конкретные индикаторы
  uv run siat-crawl --force ...     # перекачать, даже если уже есть
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from siat.api_client import ApiError, SiatApi

log = logging.getLogger("siat.crawl")

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
ENDPOINTS = {"card": "/sdmx/{id}/", "metadata": "/sdmx/{id}/metadata/", "table": "/sdmx/{id}/table/"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def open_state(data_dir: Path) -> sqlite3.Connection:
    con = sqlite3.connect(data_dir / "crawl.sqlite")
    con.execute("""
        CREATE TABLE IF NOT EXISTS fetch (
            indicator_id INTEGER NOT NULL,
            endpoint     TEXT    NOT NULL,
            status       INTEGER NOT NULL,
            fetched_at   TEXT    NOT NULL,
            sha256       TEXT,
            bytes        INTEGER,
            error        TEXT,
            PRIMARY KEY (indicator_id, endpoint)
        )""")
    return con


def iter_leaves(tree: list[dict]):
    """Обход дерева: листья (узлы без детей) — это индикаторы."""
    stack = list(tree)
    while stack:
        node = stack.pop()
        children = node.get("children") or []
        if children:
            stack.extend(children)
        else:
            yield node


def even_sample(items: list, n: int) -> list:
    """n элементов, равномерно разбросанных по списку (чтобы пилот задел все разделы)."""
    if n >= len(items):
        return items
    step = len(items) / n
    return [items[int(i * step)] for i in range(n)]


def fetch_tree(api: SiatApi, data_dir: Path, force: bool) -> list[dict]:
    path = data_dir / "raw" / "tree.json"
    if path.exists() and not force:
        log.info("дерево уже есть: %s", path)
    else:
        resp = api.get("/sdmx/json/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(resp.content)
        log.info("дерево скачано: %.1f МБ", len(resp.content) / 1e6)
    return json.loads(path.read_text(encoding="utf-8"))


def crawl(ids: list[int], api: SiatApi, con: sqlite3.Connection, data_dir: Path, force: bool) -> None:
    done = set() if force else {
        (r[0], r[1]) for r in con.execute("SELECT indicator_id, endpoint FROM fetch WHERE status IN (200, 404)")
    }
    todo = [(i, ep) for i in ids for ep in ENDPOINTS if (i, ep) not in done]
    log.info("индикаторов: %d, запросов к выполнению: %d (уже есть: %d)", len(ids), len(todo), len(ids) * 3 - len(todo))
    t0 = time.monotonic()
    for k, (ind_id, ep) in enumerate(todo, 1):
        url = ENDPOINTS[ep].format(id=ind_id)
        try:
            resp = api.get(url)
        except ApiError as e:
            log.error("ошибка %s: %s", url, e)
            con.execute("INSERT OR REPLACE INTO fetch VALUES (?,?,?,?,?,?,?)",
                        (ind_id, ep, -1, now_iso(), None, None, str(e)[:500]))
            con.commit()
            continue
        out = data_dir / "raw" / "ind" / str(ind_id) / f"{ep}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(resp.content)
        con.execute("INSERT OR REPLACE INTO fetch VALUES (?,?,?,?,?,?,?)",
                    (ind_id, ep, resp.status_code, now_iso(), hashlib.sha256(resp.content).hexdigest(),
                     len(resp.content), None))
        con.commit()
        if k % 30 == 0 or k == len(todo):
            el = time.monotonic() - t0
            eta = el / k * (len(todo) - k)
            log.info("%d/%d  пауза=%.2fс  осталось ~%.0f мин  | %s", k, len(todo), api.delay, eta / 60,
                     api.stats.summary())


def main() -> None:
    # консоль Windows по умолчанию в cp1252 и падает на кириллице
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Скачать сырые данные SIAT")
    ap.add_argument("--limit", type=int, help="взять N индикаторов равномерно по каталогу (пилот)")
    ap.add_argument("--ids", type=int, nargs="*", help="конкретные id индикаторов")
    ap.add_argument("--force", action="store_true", help="перекачать уже скачанное")
    ap.add_argument("--data-dir", type=Path, default=DATA_DIR)
    args = ap.parse_args()

    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    args.data_dir.mkdir(parents=True, exist_ok=True)

    with SiatApi() as api:
        con = open_state(args.data_dir)
        tree = fetch_tree(api, args.data_dir, args.force and not args.ids)
        leaves = sorted(iter_leaves(tree), key=lambda n: n["code"] or "")
        log.info("листьев (индикаторов) в дереве: %d", len(leaves))
        if args.ids:
            ids = args.ids
        elif args.limit:
            ids = [n["id"] for n in even_sample(leaves, args.limit)]
        else:
            ids = [n["id"] for n in leaves]
        crawl(ids, api, con, args.data_dir, args.force)
        log.info("готово. %s", api.stats.summary())


if __name__ == "__main__":
    main()
