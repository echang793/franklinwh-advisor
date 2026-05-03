"""Scraper for FranklinWH product collection and individual product pages."""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urljoin

from ..client import FranklinWHClient

logger = logging.getLogger(__name__)

BASE_URL = "https://www.franklinwh.com"

COLLECTION_PATH = "/collections/whole-home-battery-backup/"

KNOWN_PRODUCTS = [
    "/products/apower-s-home-battery/",
    "/products/apower2-home-battery-backup/",
    "/products/apower-home-battery-backup/",
    "/products/agate-home-energy-management-system/",
    "/products/meter-adapter-controller/",
]


class ProductsScraper:
    def __init__(self, client: FranklinWHClient):
        self.client = client

    def scrape_all(self) -> list[dict[str, Any]]:
        product_paths = self._discover_product_paths()
        results = []
        for path in product_paths:
            product = self._scrape_product(path)
            if product:
                results.append(product)
                logger.info("Scraped product: %s", product.get("name", path))
        return results

    # ------------------------------------------------------------------ #

    def _discover_product_paths(self) -> list[str]:
        soup = self.client.get(COLLECTION_PATH)
        paths: list[str] = []

        if soup:
            for a in soup.select("a[href*='/products/']"):
                href = a["href"]
                if isinstance(href, str) and href.startswith("/products/"):
                    if href not in paths:
                        paths.append(href)

        # Always include known products in case the collection page misses them
        for p in KNOWN_PRODUCTS:
            if p not in paths:
                paths.append(p)

        return paths

    def _scrape_product(self, path: str) -> dict[str, Any] | None:
        soup = self.client.get(path)
        if not soup:
            return None

        url = urljoin(BASE_URL, path)
        name = self._extract_name(soup)
        description = self._extract_description(soup)
        specs = self._extract_specs(soup)
        features = self._extract_features(soup)
        images = self._extract_images(soup, path)

        return {
            "url": url,
            "name": name,
            "description": description,
            "specs": specs,
            "features": features,
            "images": images,
        }

    # ------------------------------------------------------------------ #

    def _extract_name(self, soup) -> str:
        # og:title is the cleanest source
        og = soup.find("meta", property="og:title")
        if og and og.get("content"):
            return og["content"].strip()

        # <title> tag, strip " | FranklinWH" suffix
        title_tag = soup.find("title")
        if title_tag:
            title = title_tag.get_text(strip=True)
            title = re.sub(r"\s*\|.*$", "", title).strip()
            if title:
                return title

        # First short h1/h2 outside nav/header/footer
        for tag in soup.select("h1, h2"):
            if tag.find_parent(["nav", "header", "footer"]):
                continue
            text = tag.get_text(strip=True)
            # Only accept headings that look like a product name (not a sentence)
            if 2 < len(text) < 80 and text.count(" ") < 8:
                return text
        return ""

    def _extract_description(self, soup) -> str:
        # Try meta description first as it tends to be clean
        meta = soup.find("meta", attrs={"name": "description"})
        if meta and meta.get("content"):
            return meta["content"].strip()

        # Fall back to first meaningful paragraph
        for p in soup.select("p"):
            text = p.get_text(strip=True)
            if len(text) > 60:
                return text
        return ""

    # Spec value pattern: number + unit (or LFP/battery chemistry keywords)
    _SPEC_VALUE_RE = re.compile(
        r"^(\d+[\d,\.]*\s*(?:kWh|kW|Wh|W|V|A|°[FC]|dB[Aa]?|%|years?|year))\s*(.*)$",
        re.IGNORECASE,
    )
    # Spec block pattern: "15 kWh Capacity" all in one short heading
    _SPEC_INLINE_RE = re.compile(
        r"(\d+[\d,\.]*\s*(?:kWh|kW|Wh|W|V|A|°[FC]|dB[Aa]?|%|years?))\s+(.+)",
        re.IGNORECASE,
    )

    def _extract_specs(self, soup) -> dict[str, str]:
        specs: dict[str, str] = {}

        # 1. Table rows (most reliable)
        for tr in soup.select("tr"):
            if tr.find_parent(["nav", "header", "footer"]):
                continue
            cells = [td.get_text(strip=True) for td in tr.select("td, th")]
            if len(cells) >= 2 and cells[0]:
                specs[cells[0]] = " | ".join(cells[1:])

        # 2. Short headings that embed a value inline: "15 kWh Capacity"
        for tag in soup.select("h2, h3, h4, h5, p, span, div"):
            if tag.find_parent(["nav", "header", "footer"]):
                continue
            # Only look at leaf-ish nodes (no nested block children)
            if tag.find(["div", "section", "article", "ul", "ol"]):
                continue
            text = tag.get_text(strip=True)
            if not text or len(text) > 80:
                continue
            m = self._SPEC_INLINE_RE.match(text)
            if m:
                label = m.group(2).strip().rstrip(".")
                val = m.group(1).strip()
                if label and label not in specs:
                    specs[label] = val

        # 3. Sibling pair: value node followed by label node
        for container in soup.select("div, section, li"):
            if container.find_parent(["nav", "header", "footer"]):
                continue
            children = [c for c in container.children
                        if hasattr(c, "get_text")]
            if len(children) < 2:
                continue
            val_text = children[0].get_text(strip=True)
            label_text = children[1].get_text(strip=True)
            if (val_text and label_text
                    and re.search(r"\d", val_text)
                    and len(val_text) < 30
                    and len(label_text) < 60
                    and label_text not in specs):
                m = self._SPEC_VALUE_RE.match(val_text)
                if m:
                    specs[label_text] = m.group(1).strip()

        return specs

    def _extract_features(self, soup) -> list[str]:
        features: list[str] = []
        seen: set[str] = set()

        # Exclude nav/header/footer noise
        for tag in soup.select("h3, h4, li"):
            if tag.find_parent(["nav", "header", "footer"]):
                continue
            text = tag.get_text(strip=True)
            # Skip very long items (likely paragraphs) and very short ones
            if 10 < len(text) < 150 and text not in seen:
                seen.add(text)
                features.append(text)

        return features[:30]

    def _extract_images(self, soup, base_path: str) -> list[str]:
        images: list[str] = []
        for img in soup.select("img[src]"):
            src = img.get("src", "")
            if src and not src.startswith("data:"):
                full = src if src.startswith("http") else urljoin(BASE_URL, src)
                if full not in images:
                    images.append(full)
        return images[:10]
