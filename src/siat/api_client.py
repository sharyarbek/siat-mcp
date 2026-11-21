"""Вежливый HTTP-клиент к API siat.stat.uz.

Почему «вежливый»: у API есть лимит запросов. При превышении сервер отвечает
429 Too Many Requests и заголовком Retry-After (сколько секунд подождать).
Клиент:
- делает паузу между запросами (self.delay) и подстраивает её сам:
  получили 429 → ждём Retry-After и увеличиваем паузу; долго всё хорошо → чуть уменьшаем;
- повторяет запрос при сетевых ошибках и 5xx с растущей паузой (экспоненциальный backoff);
- представляется честным User-Agent.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

import httpx

log = logging.getLogger("siat.api")

BASE_URL = "https://siat.stat.uz/api"
USER_AGENT = "siat-mcp/0.1 (university study project; polite crawler)"


class ApiError(Exception):
    pass


@dataclass
class Stats:
    requests: int = 0
    ok: int = 0
    rate_limited: int = 0
    retries: int = 0
    waited_s: float = 0.0
    started: float = field(default_factory=time.monotonic)

    def summary(self) -> str:
        el = time.monotonic() - self.started
        rps = self.requests / el if el else 0
        return (f"запросов={self.requests} ok={self.ok} 429={self.rate_limited} повторов={self.retries} "
                f"ожидание={self.waited_s:.0f}с время={el:.0f}с скорость={rps:.2f} req/s")


class SiatApi:
    def __init__(self, delay: float = 0.5, min_delay: float = 0.3, max_delay: float = 6.0, max_retries: int = 6):
        self.delay = delay
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.max_retries = max_retries
        self.stats = Stats()
        self._streak = 0  # успешных запросов подряд
        self._last = 0.0
        self._http = httpx.Client(base_url=BASE_URL, timeout=60, headers={"User-Agent": USER_AGENT},
                                  follow_redirects=True)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> SiatApi:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)
            self.stats.waited_s += seconds

    def get(self, path: str, params: dict | None = None) -> httpx.Response:
        """GET с паузами и повторами. Возвращает ответ 2xx или 404; иначе бросает ApiError."""
        backoff = 2.0
        for attempt in range(self.max_retries + 1):
            # пауза между запросами
            self._sleep(self.delay - (time.monotonic() - self._last))
            self._last = time.monotonic()
            self.stats.requests += 1
            try:
                resp = self._http.get(path, params=params)
            except httpx.TransportError as e:
                log.warning("сеть: %s (%s), попытка %d", path, e, attempt + 1)
                self.stats.retries += 1
                self._sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue

            if resp.status_code == 429:
                self.stats.rate_limited += 1
                retry_after = float(resp.headers.get("Retry-After", "10"))
                self.delay = min(self.delay * 1.5, self.max_delay)
                self._streak = 0
                log.info("429 на %s: ждём %.0fс, новая пауза %.2fс", path, retry_after, self.delay)
                self._sleep(retry_after + 0.5)
                continue
            if resp.status_code >= 500:
                log.warning("%d на %s, попытка %d", resp.status_code, path, attempt + 1)
                self.stats.retries += 1
                self._sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue

            if resp.status_code < 300 or resp.status_code == 404:
                self.stats.ok += 1
                self._streak += 1
                if self._streak >= 50:  # долго без 429 — осторожно ускоряемся
                    self.delay = max(self.delay * 0.9, self.min_delay)
                    self._streak = 0
                return resp
            raise ApiError(f"{resp.status_code} на {path}: {resp.text[:200]}")
        raise ApiError(f"не удалось получить {path} за {self.max_retries + 1} попыток")
