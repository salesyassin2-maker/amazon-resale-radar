
def extract_next_page_url(
    html: str,
    current_url: str
) -> str | None:

    from urllib.parse import (
        urljoin,
        urlparse,
        parse_qs,
        parse_qsl,
        urlencode,
        urlunparse,
    )

    soup = BeautifulSoup(html, "html.parser")

    current_parsed = urlparse(current_url)
    current_params = parse_qs(current_parsed.query)

    try:
        current_page = int(
            current_params.get("page", ["1"])[0]
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

            classes = link.get("class", [])

            if "s-pagination-disabled" in classes:
                continue

            href = link.get("href")

            if not href:
                continue

            next_url = urljoin(current_url, href)
            parsed = urlparse(next_url)

            if parsed.netloc.lower() not in (
                "amazon.eg",
                "www.amazon.eg",
            ):
                continue

            params = parse_qs(parsed.query)

            try:
                next_page = int(
                    params.get("page", ["1"])[0]
                )
            except (ValueError, TypeError):
                continue

            if next_page > current_page:
                return next_url

    # إذا أعاد أمازون رابط الصفحة نفسها،
    # ننشئ رابط الصفحة التالية مع الاحتفاظ ببقية المعاملات.
    pairs = parse_qsl(
        current_parsed.query,
        keep_blank_values=True
    )

    pairs = [
        (key, value)
        for key, value in pairs
        if key != "page"
    ]

    pairs.append(("page", str(current_page + 1)))

    next_query = urlencode(pairs)

    return urlunparse((
        current_parsed.scheme,
        current_parsed.netloc,
        current_parsed.path,
        current_parsed.params,
        next_query,
        ""
    ))
