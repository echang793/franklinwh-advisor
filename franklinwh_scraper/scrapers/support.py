"""Scraper for FranklinWH knowledge base / support overview articles."""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urljoin

from ..client import FranklinWHClient

logger = logging.getLogger(__name__)

BASE_URL = "https://www.franklinwh.com"

SUPPORT_PATHS = [
    "/support/overview/",
    "/support/documentation-center/",
    "/support/sizing-guide/",
]


class SupportScraper:
    def __init__(self, client: FranklinWHClient):
        self.client = client

    def scrape_all(self) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        seen: set[str] = set()

        for path in SUPPORT_PATHS:
            soup = self.client.get(path)
            if not soup:
                continue

            # Collect links to individual articles
            for a in soup.select("a[href*='/support/']"):
                href = a.get("href", "")
                if not href or href in seen:
                    continue
                # Skip filter/pagination links
                if "?" in href or href in SUPPORT_PATHS:
                    continue
                seen.add(href)

                title = a.get_text(strip=True)
                if not title:
                    continue

                article = self._scrape_article(href, title)
                if article:
                    results.append(article)
                    logger.debug("Support article: %s", title)

        logger.info("Support: scraped %d articles", len(results))
        return results

    # ------------------------------------------------------------------ #

    def _scrape_article(self, path: str, title: str) -> dict[str, Any] | None:
        soup = self.client.get(path)
        if not soup:
            return None

        body_parts: list[str] = []
        for sel in ("article", ".article-body", ".content", "main"):
            container = soup.select_one(sel)
            if container:
                for tag in container.select("p, li, h2, h3, h4"):
                    text = tag.get_text(strip=True)
                    if len(text) > 20:
                        body_parts.append(text)
                break

        # Fallback
        if not body_parts:
            for p in soup.select("p"):
                text = p.get_text(strip=True)
                if len(text) > 40:
                    body_parts.append(text)

        if not body_parts:
            return None

        return {
            "url": urljoin(BASE_URL, path),
            "title": title,
            "body": "\n\n".join(body_parts[:20]),
        }
