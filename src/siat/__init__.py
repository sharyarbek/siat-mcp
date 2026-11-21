"""SIAT MCP: статистика siat.stat.uz для LLM через Model Context Protocol."""


def main() -> None:
    from siat.server import main as serve

    serve()
