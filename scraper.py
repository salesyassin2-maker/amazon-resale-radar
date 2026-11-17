from __future__ import annotations

import re
from html import unescape
from urllib.parse import urljoin

from bs4 import BeautifulSoup

BASE_URL = "https://www.amazon.eg/b?node=27972173031"


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _price(text: str) -> float | None:
    # Examples in the captured page: "1,379.21 جنيه" and "40.82 جنيه"
    match = re.search(r"([\d,]+(?:\.\d{1,2})?)\s*جنيه", text or "")
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", ""))
    except ValueError:
        return None


def parse_resale_html(html: str) -> list[dict]:
    """Parse product cards from the captured Amazon Resale page structure.

    Amazon currently embeds product cards inside escaped <raw-html data-payload>
    elements. This parser is intentionally defensive because Amazon can change
    its markup at any time.
    """
    outer = BeautifulSoup(html, "html.parser")
    cards: dict[str, dict] = {}

    payloads = [node.get("data-payload", "") for node in outer.find_all("raw-html")]
    # Also support a response where card markup is already present in the DOM.
    payloads.append(str(outer))

    for payload in payloads:
        if "data-csa-c-item-id" not in payload:
            continue
        inner = BeautifulSoup(unescape(payload), "html.parser")
        for card in inner.find_all(attrs={"data-csa-c-item-id": True}):
            raw_id = card.get("data-csa-c-item-id", "")
            asin_match = re.search(r"(?:asin\.)?([A-Z0-9]{10})$", raw_id)
            if not asin_match:
                continue
            asin = asin_match.group(1)

            title_node = card.find("h2")
            if title_node:
                title = _clean(title_node.get_text(" ", strip=True))
            else:
                title = _clean(card.get_text(" ", strip=True))[:300]

            image = None
            for img in card.find_all("img"):
                image = img.get("src") or img.get("data-src") or img.get("data-a-dynamic-image")
                if image:
                    break

            link = None
            for a in card.find_all("a", href=True):
                href = a.get("href", "")
                if f"/dp/{asin}" in href:
                    link = urljoin("https://www.amazon.eg", href.split("&amp;")[0])
                    break
            if not link:
                link = f"https://www.amazon.eg/dp/{asin}"

            price_node = card.select_one('[data-cy="secondary-offer-recipe"]')
            price_text = price_node.get_text(" ", strip=True) if price_node else ""
            price = _price(price_text)

            if title and asin:
                cards[asin] = {
                    "asin": asin,
                    "title": title,
                    "price": price,
                    "image_url": image,
                    "product_url": link,
                }

    return list(cards.values())
