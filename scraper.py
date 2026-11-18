
from __future__ import annotations

import json
import re
from html import unescape
from urllib.parse import urljoin, urlparse, parse_qs, urlencode

from bs4 import BeautifulSoup


BASE_URL = "https://www.amazon.eg/s"

COMMON = {
    "srs": "27972173031",
    "bbn": "27972173031",
    "dc": "",
}

# كل مفتاح يمثل مسار فحص مستقل
CATEGORIES = {

    # =========================
    # الأزياء - 6 مسارات
    # =========================

    "fashion_women": {
        "name": "نساء",
        "parent": "الأزياء",
        "mode": "filter",
        "department": "fashion",
        "root": "18018165031",
        "filter": "p_n_g-101015233022111",
        "value": "96332614031",
        "rnid": "96332613031",
    },

    "fashion_men": {
        "name": "رجال",
        "parent": "الأزياء",
        "mode": "filter",
        "department": "fashion",
        "root": "18018165031",
        "filter": "p_n_g-101015233022111",
        "value": "96332615031",
        "rnid": "96332613031",
    },

    "fashion_girls_boys": {
        "name": "بنات وأولاد",
        "parent": "الأزياء",
        "mode": "filter",
        "department": "fashion",
        "root": "18018165031",
        "filter": "p_n_g-101015233022111",
        # القيمة محفوظة بالترميز الذي ظهر في رابط أمازون
        "value": "96332618031%7C96332622031",
        "rnid": "96332613031",
    },

    "fashion_baby": {
        "name": "الطفل",
        "parent": "الأزياء",
        "mode": "filter",
        "department": "fashion",
        "root": "18018165031",
        "filter": "p_n_g-101015233022111",
        "value": "98208548031",
        "rnid": "96332613031",
    },

    "fashion_unisex": {
        "name": "للجنسين",
        "parent": "الأزياء",
        "mode": "filter",
        "department": "fashion",
        "root": "18018165031",
        "filter": "p_n_g-101015233022111",
        "value": "98208549031",
        "rnid": "96332613031",
    },

    "fashion_luggage": {
        "name": "الأمتعة ومعدات السفر",
        "parent": "الأزياء",
        "mode": "node",
        "department": "fashion",
        "root": "18018165031",
        "node": "21845148031",
        "rnid": "18018165031",
    },

    # =========================
    # المنزل والمطبخ - 7 مسارات
    # =========================

    "home_kitchen_1": {
        "name": "المنزل والمطبخ - فرع 1",
        "parent": "المنزل والمطبخ",
        "mode": "node",
        "department": "home",
        "root": "18021933031",
        "node": "21863792031",
        "rnid": "18021933031",
    },

    "home_kitchen_2": {
        "name": "المنزل والمطبخ - فرع 2",
        "parent": "المنزل والمطبخ",
        "mode": "node",
        "department": "home",
        "root": "18021933031",
        "node": "21863794031",
        "rnid": "18021933031",
    },

    "home_kitchen_3": {
        "name": "المنزل والمطبخ - فرع 3",
        "parent": "المنزل والمطبخ",
        "mode": "node",
        "department": "home",
        "root": "18021933031",
        "node": "21863798031",
        "rnid": "18021933031",
    },

    "home_kitchen_4": {
        "name": "المنزل والمطبخ - فرع 4",
        "parent": "المنزل والمطبخ",
        "mode": "node",
        "department": "home",
        "root": "18021933031",
        "node": "21863791031",
        "rnid": "18021933031",
    },

    "home_kitchen_5": {
        "name": "المنزل والمطبخ - فرع 5",
        "parent": "المنزل والمطبخ",
        "mode": "node",
        "department": "home",
        "root": "18021933031",
        "node": "21863800031",
        "rnid": "18021933031",
    },

    "home_kitchen_6": {
        "name": "المنزل والمطبخ - فرع 6",
        "parent": "المنزل والمطبخ",
        "mode": "node",
        "department": "home",
        "root": "18021933031",
        "node": "21863799031",
        "rnid": "18021933031",
    },

    "home_kitchen_7": {
        "name": "المنزل والمطبخ - فرع 7",
        "parent": "المنزل والمطبخ",
        "mode": "node",
        "department": "home",
        "root": "18021933031",
        "node": "21863790031",
        "rnid": "18021933031",
    },

    # =========================
    # أدوات وتحسين المنزل - 7 مسارات
    # =========================

    "improvement_1": {
        "name": "أدوات وتحسين المنزل - فرع 1",
        "parent": "أدوات وتحسين المنزل",
        "mode": "node",
        "department": "home-improvement",
        "root": "18021990031",
        "node": "21874765031",
        "rnid": "18021990031",
    },

    "improvement_2": {
        "name": "أدوات وتحسين المنزل - فرع 2",
        "parent": "أدوات وتحسين المنزل",
        "mode": "node",
        "department": "home-improvement",
        "root": "18021990031",
        "node": "21874773031",
        "rnid": "18021990031",
    },

    "improvement_3": {
        "name": "أدوات وتحسين المنزل - فرع 3",
        "parent": "أدوات وتحسين المنزل",
        "mode": "node",
        "department": "home-improvement",
        "root": "18021990031",
        "node": "21874769031",
        "rnid": "18021990031",
    },

    "improvement_4": {
        "name": "أدوات وتحسين المنزل - فرع 4",
        "parent": "أدوات وتحسين المنزل",
        "mode": "node",
        "department": "home-improvement",
        "root": "18021990031",
        "node": "21874766031",
        "rnid": "18021990031",
    },

    "improvement_5": {
        "name": "أدوات وتحسين المنزل - فرع 5",
        "parent": "أدوات وتحسين المنزل",
        "mode": "node",
        "department": "home-improvement",
        "root": "18021990031",
        "node": "21874775031",
        "rnid": "18021990031",
    },

    "improvement_6": {
        "name": "أدوات وتحسين المنزل - فرع 6",
        "parent": "أدوات وتحسين المنزل",
        "mode": "node",
        "department": "home-improvement",
        "root": "18021990031",
        "node": "21874776031",
        "rnid": "18021990031",
    },

    "improvement_7": {
        "name": "أدوات وتحسين المنزل - فرع 7",
        "parent": "أدوات وتحسين المنزل",
        "mode": "node",
        "department": "home-improvement",
        "root": "18021990031",
        "node": "21874767031",
        "rnid": "18021990031",
    },

    # =========================
    # أقسام مستقلة
    # =========================

    "grocery": {
        "name": "سوبر ماركت",
        "parent": "سوبر ماركت",
        "mode": "standalone",
        "department": None,
        "root": "18020637031",
    },

    "baby": {
        "name": "منتجات الأطفال",
        "parent": "منتجات الأطفال",
        "mode": "standalone",
        "department": None,
        "root": "18017931031",
    },
}


def build_start_url(category_key: str) -> str:
    if category_key not in CATEGORIES:
        raise ValueError(f"Unknown category: {category_key}")

    category = CATEGORIES[category_key]
    params = dict(COMMON)

    if category["mode"] == "filter":
        params["i"] = category["department"]
        params["rh"] = (
            f"n:{category['root']},"
            f"{category['filter']}:{category['value']}"
        )
        params["rnid"] = category["rnid"]

    elif category["mode"] == "node":
        params["i"] = category["department"]
        params["rh"] = (
            f"n:{category['root']},"
            f"n:27972173031,"
            f"n:{category['node']}"
        )
        params["rnid"] = category["rnid"]

    else:
        params["rh"] = (
            f"n:27972173031,"
            f"n:{category['root']}"
        )
        params["rnid"] = "26263709031"

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

    for attribute in (
        "data-old-hires",
        "src",
        "data-src",
    ):
        image = img.get(attribute)

        if image and image.startswith("//"):
            image = "https:" + image

        if image and image.startswith("http"):
            return image

    dynamic = img.get("data-a-dynamic-image")

    if dynamic:
        try:
            images = json.loads(dynamic)
            if images:
                return next(iter(images))
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

    # لا نصنع صفحات افتراضية.
    return None
