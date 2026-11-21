"""Генератор вопросов для проверки качества (eval) с эталонными ответами из нашей базы.

Идея: правильный ответ мы знаем точно, потому что он лежит в siat.db. Значит, можно
сгенерировать вопросы по шаблонам и потом автоматически проверять, назвала ли модель
правильное число и дала ли ссылку на источник.

Типы вопросов:
  value   — «Каким был X в регионе R в году Y?»                 → число
  top     — «Какой регион был первым по X в году Y?»             → название региона
  change  — «На сколько % изменился X с Y1 по Y2 (Республика)?»   → процент
  missing — вопрос про год, которого нет в данных                → ответ «нет данных»

Запуск:  uv run python eval/make_questions.py --n 40 --seed 7

Пишет два файла:
  eval/questions.jsonl   — только id и текст вопроса (это видит отвечающая модель);
  data/eval/gold.jsonl   — правильные ответы (лежат отдельно, чтобы модель их не подсмотрела).
"""

import argparse
import json
import random
import sys
from pathlib import Path

from siat import core


def candidates(con):
    """Годовые индикаторы с разрезом «Регионы», 15 строк и данными минимум за 5 лет."""
    return con.execute("""
        SELECT i.id, i.code, n.name_ru FROM indicator i JOIN node n USING(id)
        WHERE i.classifier_id = 1 AND i.n_items = 15 AND i.period_types = 'Y' AND i.n_obs >= 75
          AND i.has_zeros = 0
        ORDER BY i.id""").fetchall()


def years(con, ind_id):
    return [r[0] for r in con.execute(
        "SELECT DISTINCT period FROM observation WHERE indicator_id=? ORDER BY period", (ind_id,))]


def unit(con, ind_id):
    r = con.execute("SELECT value FROM indicator_meta WHERE indicator_id=? AND key='unit' AND lang='ru'",
                    (ind_id,)).fetchone()
    return r[0] if r else ""


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=40)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    rnd = random.Random(args.seed)

    con = core.connect()
    pool = candidates(con)
    rnd.shuffle(pool)
    kinds = ["value", "value", "top", "change", "missing"]
    out = []
    for k, (ind_id, code, name) in enumerate(pool):
        if len(out) >= args.n:
            break
        kind = kinds[k % len(kinds)]
        ys = years(con, ind_id)
        u = unit(con, ind_id)
        base = {"id": f"q{len(out) + 1:03d}", "kind": kind, "indicator_id": ind_id, "indicator_code": code,
                "unit": u, "source_url": f"https://siat.stat.uz/data/{ind_id}/"}
        if kind == "value":
            y = rnd.choice(ys)
            # случайный регион выбираем через rnd (с seed), а не через SQL random() — чтобы прогон повторялся
            reg = rnd.choice(con.execute("""SELECT it.code, it.name_ru, o.value FROM observation o
                JOIN item it ON it.indicator_id=o.indicator_id AND it.code=o.item_code
                WHERE o.indicator_id=? AND o.period=? AND it.is_root=0 ORDER BY it.code""",
                              (ind_id, y)).fetchall())
            q = f"Какое значение показателя «{name}» было в регионе «{reg[1]}» в {y} году?"
            out.append({**base, "question": q, "gold": {"value": reg[2], "item": reg[1], "period": y}})
        elif kind == "top":
            y = ys[-1]
            r = core.rank_items(con, str(ind_id), y, "desc", 1)["ranking"][0]
            q = f"Какой регион занимал первое место по показателю «{name}» в {y} году?"
            out.append({**base, "question": q, "gold": {"item": r["name"], "value": r["value"], "period": y}})
        elif kind == "change":
            if "%" in u or "процент" in u.lower() or "темп" in name.lower() or len(ys) < 6:
                continue
            y1, y2 = ys[-6], ys[-1]
            r = core.compute_change(con, str(ind_id), None, y1, y2)
            if r["percent_change"] is None:
                continue
            q = (f"На сколько процентов изменился показатель «{name}» в целом по Республике Узбекистан "
                 f"с {y1} по {y2} год?")
            out.append({**base, "question": q, "gold": {"percent_change": r["percent_change"], "from": y1, "to": y2}})
        else:  # missing
            y = str(int(ys[-1]) + 5)
            q = f"Каким был показатель «{name}» в Самаркандской области в {y} году?"
            out.append({**base, "question": q, "gold": {"no_data": True, "period": y}})

    q_path, g_path = Path("eval/questions.jsonl"), Path("data/eval/gold.jsonl")
    g_path.parent.mkdir(parents=True, exist_ok=True)
    with q_path.open("w", encoding="utf-8") as fq, g_path.open("w", encoding="utf-8") as fg:
        for row in out:
            fq.write(json.dumps({"id": row["id"], "question": row["question"]}, ensure_ascii=False) + "\n")
            fg.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"сгенерировано {len(out)} вопросов из {len(pool)} подходящих индикаторов\n"
          f"  вопросы: {q_path}\n  ответы:  {g_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
