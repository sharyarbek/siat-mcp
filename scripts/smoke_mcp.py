"""Смоук-тест: запускает MCP-сервер как настоящий клиент (через stdio) и вызывает инструменты.

Запуск:  uv run python scripts/smoke_mcp.py
"""

import asyncio
import json
import sys

from mcp import Client, StdioServerParameters


def short(x, n=700):
    s = json.dumps(x, ensure_ascii=False)
    return s if len(s) <= n else s[:n] + " …"


async def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    server = StdioServerParameters(command=sys.executable, args=["-m", "siat.server"])
    async with Client(server) as client:
        tools = await client.list_tools()
        print("Инструменты:", [t.name for t in tools.tools])
        prompts = await client.list_prompts()
        print("Промпты:", [p.name for p in prompts.prompts])

        res = await client.read_resource("siat://indicator/1299")
        print("Ресурс siat://indicator/1299:", res.contents[0].text[:150], "…")

        calls = [
            ("search_indicators", {"query": "валовой региональный продукт"}),
            ("get_data", {"indicator": "1.01.03.0001", "items": ["Самарканд"], "period_from": "2023"}),
            ("rank_items", {"indicator": "1.01.03.0001", "top": 3}),
            ("compute_change", {"indicator": "1.01.03.0001", "period_from": "2015", "period_to": "2025"}),
            ("get_indicator", {"indicator": "999999"}),  # ошибка: такого нет
        ]
        for name, args in calls:
            r = await client.call_tool(name, args)
            body = r.structured_content if r.structured_content is not None else r.content[0].text
            print(f"\n{name}({args}) error={r.is_error}\n  {short(body)}")


if __name__ == "__main__":
    asyncio.run(main())
