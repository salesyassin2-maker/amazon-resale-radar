
from __future__ import annotations

import json
import re
from html import unescape
from urllib.parse import urljoin, urlparse, parse_qs, urlencode

from bs4 import BeautifulSoup


BASE_URL = "https://www.amazon.eg/s"

BASE_PARAMS = {
    "i": "warehouse-deals",
    "srs": "27972173031",
    "bbn": "27972173031",
    "s": "popularity-rank",
    "dc": "",
    "fs": "true",
}

CATEGORIES = {
    "fashion": {
        "name": "الأزياء",
        "node": "18018165031",
    },
    "home_kitchen": {
        "name": "المنزل والمطبخ",
        "node": "18021933031",
    },
    "home_improvement": {
        "name": "أدوات وتحسين المنزل",
        "node": "18021990031",
    },
    "grocery": {
        "name": "سوبر ماركت",
        "node": "18020637031",
    },
    "baby": {
        "name": "منتجات الأطفال",
        "node": "18017931031",
    },
}


def build_start_url(category_key: str | None = None) -> str:
    params = dict(BASE_PARAMS)

    if category_key:
        if category_key not in CATEGORIES:
            raise ValueError(f"Unknown category: {category_key}")

        params["rh"] = (
            f"n:27972173031,"
            f"n:{CATEGORIES[category_key]['node']}"
        )
        params["rnid"] = "27972173031"
    else:
        params["rh"] = "n:27972173031"
        params["rnid"] = "27972173031"

    return BASE_URL + "?" + urlencode(params)


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _price(text: str) -> float | None:
    if not text:
        return None

    text = text.translate(
        str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
    )
    text = text.translate(
        str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")
    )

    match = re.search(
        r"([\d,]+(?:\.\d{1,2})?)",
        text.replace("٬", ",").replace("٫", ".")
    )

    if not match:
        return None

    try:
        return float(match.group(1).replace(",", ""))
    except ValueError:
        return None


def _get_asin(card) -> str:
    asin = card.get("data-asin", "").strip()

    if re.fullmatch(r"[A-Z0-9]{10}", asin):
        return asin

    raw_id = card.get("data-csa-c-item-id", "")
    match = re.search(r"([A-Z0-9]{10})$", raw_id)

    if match:
        return match.group(1)

    for link in card.find_all("a", href=True):
        match = re.search(
            r"/(?:dp|gp/product)/([A-Z0-9]{10})",
            link["href"]
        )
        if match:
            return match.group(1)

    return ""


def _get_title(card) -> str:
    node = card.select_one("h2")

    if not node:
        node = card.select_one(
            ".a-text-normal, [class*='product-title']"
        )

    return (
        _clean(node.get_text(" ", strip=True))
        if node else ""
    )


def _get_image(card):
    img = card.find("img")

    if not img:
        return None

    image = (
        img.get("src")
        or img.get("data-src")
        or img.get("data-old-hires")
    )

    if image and image.startswith("//"):
        image = "https:" + image

    if image:
        return image

    dynamic = img.get("data-a-dynamic-image")

    if dynamic:
        try:
            return next(iter(json.loads(dynamic)))
        except (ValueError, TypeError, StopIteration):
            pass

    return None


def _get_link(card, asin: str) -> str:
    for a in card.find_all("a", href=True):
        href = a["href"]

        if (
            f"/dp/{asin}" in href
            or f"/gp/product/{asin}" in href
        ):
            return urljoin(
                "https://www.amazon.eg",
                href.split("?")[0]
            )

    return f"https://www.amazon.eg/dp/{asin}"


def _get_price(card):
    selectors = [
        '[data-cy="secondary-offer-recipe"]',
        ".a-price .a-offscreen",
        ".a-price-whole",
        "[data-a-color='price']",
    ]

    for selector in selectors:
        node = card.select_one(selector)

        if node:
            value = _price(
                node.get_text(" ", strip=True)
            )

            if value is not None:
                return value

    return None


def parse_resale_html(html: str) -> list[dict]:
    outer = BeautifulSoup(html, "html.parser")
    cards: dict[str, dict] = {}

    payloads = [
        node.get("data-payload", "")
        for node in outer.find_all("raw-html")
        if node.get("data-payload")
    ]

    payloads.append(str(outer))

    for payload in payloads:
        inner = BeautifulSoup(
            unescape(payload),
            "html.parser"
        )

        found = []

        selectors = [
            '[data-component-type="s-search-result"]',
            ".s-result-item[data-asin]",
            "[data-csa-c-item-id]",
            "[data-asin]",
        ]

        for selector in selectors:
            found.extend(inner.select(selector))

        for card in found:
            asin = _get_asin(card)

            if not asin or asin in cards:
                continue

            title = _get_title(card)

            if not title:
                continue

            cards[asin] = {
                "asin": asin,
                "title": title,
                "price": _get_price(card),
                "image_url": _get_image(card),
                "product_url": _get_link(card, asin),
            }

    return list(cards.values())


def extract_next_page_url(
    html: str,
    current_url: str
) -> str | None:

    soup = BeautifulSoup(html, "html.parser")

    current = urlparse(current_url)
    current_values = parse_qs(current.query)

    try:
        current_page = int(
            current_values.get("page", ["1"])[0]
        )
    except (ValueError, TypeError):
        current_page = 1

    selectors = [
        "a.s-pagination-next[href]",
        "a[rel='next'][href]",
        "a[aria-label='Next'][href]",
        "a[aria-label='التالي'][href]",
        "a.s-pagination-item.s-pagination-next[href]",
    ]

    for selector in selectors:
        for link in soup.select(selector):

            if "s-pagination-disabled" in link.get(
                "class", []
            ):
                continue

            href = link.get("href")

            if not href:
                continue

            candidate = urljoin(current_url, href)
            parsed = urlparse(candidate)

            if parsed.netloc.lower() not in (
                "amazon.eg",
                "www.amazon.eg",
            ):
                continue

            values = parse_qs(parsed.query)

            try:
                next_page = int(
                    values.get("page", ["1"])[0]
                )
            except (ValueError, TypeError):
                continue

            if next_page > current_page:
                return candidate

    # لا ننشئ صفحات افتراضية.
    # غياب رابط التالي يعني أن أمازون لم يقدم رابطًا صالحًا.
    return None
