"""Scraper for FranklinWH FAQ articles."""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urljoin, urlencode

from ..client import FranklinWHClient

logger = logging.getLogger(__name__)

BASE_URL = "https://www.franklinwh.com"

FAQ_PATH = "/support/articles/faq"
# user_role=1 → homeowner, user_role=2 → installer
ROLES = {"homeowner": 1, "installer": 2}


class FAQScraper:
    def __init__(self, client: FranklinWHClient, max_pages: int = 5):
        self.client = client
        self.max_pages = max_pages

    def scrape_all(self) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        seen_slugs: set[str] = set()

        # Scrape general + per-role FAQs
        for role_name, role_id in [("all", None), *ROLES.items()]:
            items = self._scrape_role(role_name, role_id, seen_slugs)
            results.extend(items)

        logger.info("FAQ: scraped %d items", len(results))
        return results

    # ------------------------------------------------------------------ #

    def _scrape_role(
        self,
        role_name: str,
        role_id: int | None,
        seen: set[str],
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        page = 1

        while page <= self.max_pages:
            params: dict[str, Any] = {"page": page}
            if role_id is not None:
                params["user_role"] = role_id
            qs = urlencode(params)
            path = f"{FAQ_PATH}?{qs}"

            soup = self.client.get(path)
            if not soup:
                break

            links = soup.select("a[href*='/support/articles/detail/']")
            if not links:
                break

            new_on_page = 0
            for a in links:
                href = a.get("href", "")
                slug = href.rstrip("/").split("/")[-1]
                if slug in seen:
                    continue
                seen.add(slug)
                new_on_page += 1

                question = a.get_text(strip=True)
                # Try to get rating/like count
                rating_tag = a.find_parent().find(string=lambda t: t and t.strip().isdigit()) if a.find_parent() else None
                rating = int(rating_tag.strip()) if rating_tag else None

                detail = self._scrape_detail(href)
                results.append({
                    "slug": slug,
                    "question": question,
                    "role": role_name,
                    "rating": rating,
                    "url": urljoin(BASE_URL, href),
                    "answer": detail.get("answer", ""),
                    "tags": detail.get("tags", []),
                })

            if new_on_page == 0:
                break
            page += 1

        return results

    def _scrape_detail(self, path: str) -> dict[str, Any]:
        soup = self.client.get(path)
        if not soup:
            return {}

        answer_parts: list[str] = []
        for tag in soup.select("article p, article li, .article-body p, .content p"):
            text = tag.get_text(strip=True)
            if text:
                answer_parts.append(text)

        # Fallback: grab all meaningful paragraphs from body
        if not answer_parts:
            for p in soup.select("p"):
                text = p.get_text(strip=True)
                if len(text) > 30:
                    answer_parts.append(text)

        tags: list[str] = []
        for tag in soup.select(".tag, .label, [class*='tag']"):
            t = tag.get_text(strip=True)
            if t:
                tags.append(t)

        return {
            "answer": "\n\n".join(answer_parts[:10]),
            "tags": tags,
        }
