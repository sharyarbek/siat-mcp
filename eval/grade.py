"""Проверка ответов модели по эталону.

Модель (в отдельной сессии Claude Code с MCP-сервером siat) отвечает на вопросы из
eval/questions.jsonl и пишет eval/answers.jsonl — по строке {"id": "q001", "answer": "текст ответа"}.
Этот скрипт сравнивает ответы с data/eval/gold.jsonl:

  число     — в ответе есть число, совпадающее с эталоном (допуск 0.1%; для процентов ±0.1 п.п.);
  регион    — в ответе есть полное название правильного элемента («город Ташкент»);
  нет данных — модель честно сказала, что данных нет, а не выдумала число;
  источник  — в ответе есть ссылка siat.stat.uz/data/<id нужного индикатора>.

Запуск:  uv run python eval/grade.py
"""

import json
import re
import sys
from pathlib import Path

from siat import core
from siat.textnorm import norm

# «нет данных», «данных за 2030 год нет», «данные отсутствуют», «значения нет»…
NO_DATA = re.compile(r"\bнет\b[^.]{0,30}(данн|информац|значени)|(данн|значени|информац)\w*[^.]{0,40}\bнет\b"
                     r"|отсутству|недоступ|не опубликован")


def item_pattern(name: str) -> re.Pattern:
    """Название элемента с учётом падежей: «город Ташкент» найдёт «в городе Ташкенте»,
    «Наманганская область» — «в Наманганской области». Слова должны идти подряд."""
    stems = [w[: max(4, len(w) - 2)] if len(w) >= 6 else w for w in re.findall(r"\w+", norm(name))]
    return re.compile(r"\b" + r"\w*\W+".join(re.escape(s) for s in stems) + r"\w*")


def numbers(text: str) -> list[float]:
    """Все числа из текста: «31 566», «148 669,8», «141.22%» → float."""
    t = text.replace(" ", " ").replace(" ", " ")
    t = re.sub(r"(?<=\d) (?=\d{3}\b)", "", t)  # «31 566» → «31566»
    out = []
    for m in re.findall(r"-?\d+(?:[.,]\d+)?", t):
        try:
            out.append(float(m.replace(",", ".")))
        except ValueError:
            pass
    return out


def has_number(text: str, gold: float, abs_tol: float | None = None) -> bool:
    tol = abs_tol if abs_tol is not None else max(abs(gold) * 0.001, 1e-9)
    return any(abs(x - gold) <= tol for x in numbers(text))


def twins(con, ind_id: int) -> list[int]:
    """На SIAT бывают индикаторы-близнецы: одно название в разных разделах (например,
    1.04.01.0031 и 1.04.03.0003). Ссылка на любой из них считается правильной."""
    return [r[0] for r in con.execute(
        "SELECT id FROM node WHERE name_ru = (SELECT name_ru FROM node WHERE id=?) AND id IN (SELECT id FROM indicator)",
        (ind_id,))] or [ind_id]


def grade(g: dict, answer: str, ok_ids: list[int]) -> dict:
    a = norm(answer)
    gold = g["gold"]
    ids = "|".join(map(str, ok_ids))
    cited = re.search(rf"siat\.stat\.uz/(data|reports-filed)/({ids})(?!\d)", a) is not None
    if g["kind"] == "value":
        ok = has_number(answer, gold["value"])
    elif g["kind"] == "top":
        # и название лидера, и его значение — чтобы не засчитать регион, упомянутый как «второе место»
        ok = item_pattern(gold["item"]).search(a) is not None and has_number(answer, gold["value"])
    elif g["kind"] == "change":
        ok = has_number(answer, gold["percent_change"], abs_tol=0.1)
    else:  # missing
        ok = NO_DATA.search(a) is not None
        cited = True  # для «нет данных» ссылка не обязательна
    return {"ok": ok, "cited": cited}


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    gold = {r["id"]: r for r in map(json.loads, Path("data/eval/gold.jsonl").read_text(encoding="utf-8").splitlines())}
    ans_path = Path(sys.argv[1] if len(sys.argv) > 1 else "eval/answers.jsonl")
    if not ans_path.exists():
        sys.exit("нет файла eval/answers.jsonl — сначала пусть модель ответит на вопросы (см. README)")
    answers = {}
    for line in ans_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            answers[r["id"]] = r.get("answer", "")

    con = core.connect()
    by_kind: dict[str, list[int]] = {}
    n_ok = n_cited = 0
    for qid, g in gold.items():
        if qid not in answers:
            print(f"--   {qid} [{g['kind']}] нет ответа")
            by_kind.setdefault(g["kind"], [0, 0])[1] += 1
            continue
        r = grade(g, answers[qid], twins(con, g["indicator_id"]))
        n_ok += r["ok"]
        n_cited += r["cited"]
        k = by_kind.setdefault(g["kind"], [0, 0])
        k[0] += r["ok"]
        k[1] += 1
        if not (r["ok"] and r["cited"]):
            what = [] if r["ok"] else ["НЕВЕРНО"]
            what += [] if r["cited"] else ["без ссылки"]
            print(f"FAIL {qid} [{g['kind']}] {', '.join(what)}\n     вопрос: {g['question']}\n"
                  f"     эталон: {g['gold']}\n     ответ:  {answers[qid][:300]}\n")

    n = len(gold)
    print(f"Правильных ответов: {n_ok}/{n} = {100 * n_ok / n:.0f}%")
    print(f"Со ссылкой на нужный индикатор: {n_cited}/{n} = {100 * n_cited / n:.0f}%")
    for kind, (ok, tot) in by_kind.items():
        print(f"  {kind:8} {ok}/{tot}")


if __name__ == "__main__":
    main()
