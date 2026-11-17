
from __future__ import annotations

import json
import re
from html import unescape
from urllib.parse import urljoin

from bs4 import BeautifulSoup

BASE_URL = "https://www.amazon.eg/s"
MAX_PAGES = 187

SEARCH_PARAMS = {
    "i": "warehouse-deals",
    "srs": "27972173031",
    "bbn": "27972173031",
    "s": "popularity-rank",
    "fs": "true",
}


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _price(text: str) -> float | None:
    match = re.search(
        r"([\d,]+(?:\.\d{1,2})?)\s*جنيه",
        text or ""
    )
    if not match:
        return None

    try:
        return float(match.group(1).replace(",", ""))
    except ValueError:
        return None


def parse_resale_html(html: str) -> list[dict]:
    outer = BeautifulSoup(html, "html.parser")
    cards = {}

    payloads = [
        node.get("data-payload", "")
        for node in outer.find_all("raw-html")
    ]
    payloads.append(str(outer))

    for payload in payloads:
        inner = BeautifulSoup(unescape(payload), "html.parser")

        product_cards = inner.select(
            '[data-component-type="s-search-result"][data-asin]'
        )

        product_cards += inner.find_all(
            attrs={"data-csa-c-item-id": True}
        )

        for card in product_cards:
            asin = card.get("data-asin", "")

            if not asin:
                raw_id = card.get("data-csa-c-item-id", "")
                match = re.search(r"([A-Z0-9]{10})$", raw_id)
                asin = match.group(1) if match else ""

            if not re.fullmatch(r"[A-Z0-9]{10}", asin):
                continue

            title_node = card.select_one("h2")
            if not title_node:
                continue

            title = _clean(title_node.get_text(" ", strip=True))
            if not title:
                continue

            image = None
            img = card.find("img")

            if img:
                image = img.get("src") or img.get("data-src")

                if not image:
                    dynamic = img.get("data-a-dynamic-image")
                    if dynamic:
                        try:
                            image = next(iter(json.loads(dynamic)))
                        except (ValueError, TypeError):
                            pass

            link = None

            for a in card.find_all("a", href=True):
                href = a["href"]
                if f"/dp/{asin}" in href:
                    link = urljoin(
                        "https://www.amazon.eg",
                        href.split("&amp;")[0]
                    )
                    break

            if not link:
                link = f"https://www.amazon.eg/dp/{asin}"

            price_node = (
                card.select_one('[data-cy="secondary-offer-recipe"]')
                or card.select_one(".a-price .a-offscreen")
            )

            price_text = (
                price_node.get_text(" ", strip=True)
                if price_node else ""
            )

            cards[asin] = {
                "asin": asin,
                "title": title,
                "price": _price(price_text),
                "image_url": image,
                "product_url": link,
            }

    return list(cards.values())
