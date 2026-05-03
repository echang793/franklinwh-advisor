"""HTTP client with rate limiting and retry logic."""

from __future__ import annotations

import time
import logging
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

BASE_URL = "https://www.franklinwh.com"
DEFAULT_DELAY = 1.5  # seconds between requests


class FranklinWHClient:
    def __init__(self, delay: float = DEFAULT_DELAY, timeout: int = 30):
        self.delay = delay
        self.timeout = timeout
        self._last_request_time: float = 0
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        })

    def _throttle(self):
        elapsed = time.time() - self._last_request_time
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)

    def get(self, path: str) -> BeautifulSoup | None:
        url = path if path.startswith("http") else urljoin(BASE_URL, path)
        self._throttle()
        try:
            resp = self.session.get(url, timeout=self.timeout)
            self._last_request_time = time.time()
            resp.raise_for_status()
            logger.debug("GET %s -> %d", url, resp.status_code)
            return BeautifulSoup(resp.text, "lxml")
        except requests.RequestException as e:
            logger.warning("Failed to fetch %s: %s", url, e)
            return None

    def close(self):
        self.session.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
