
from __future__ import annotations

import os
import time
import sqlite3
import logging

from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from scraper import (
    BASE_URL,
    MAX_PAGES,
    SEARCH_PARAMS,
    parse_resale_html,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
logger = logging.getLogger("resale-radar")

ROOT = Path(__file__).resolve().parent

DB_PATH = Path(
    os.getenv("DATABASE_PATH", ROOT / "resale.db")
)

INTERVAL_SECONDS = max(
    1800,
    int(os.getenv("CHECK_INTERVAL_SECONDS", "1800"))
)

REQUEST_TIMEOUT = int(
    os.getenv("REQUEST_TIMEOUT_SECONDS", "30")
)

PAGE_DELAY = max(
    10,
    int(os.getenv("PAGE_DELAY_SECONDS", "10"))
)

MAX_PAGES_PER_RUN = max(
    1,
    int(os.getenv("MAX_PAGES_PER_RUN", "10"))
)

app = FastAPI(title="Amazon Resale Radar")

app.mount(
    "/static",
    StaticFiles(directory=ROOT / "static"),
    name="static"
)

scheduler = BackgroundScheduler(timezone="UTC")

session = requests.Session()

session.headers.update({
    "User-Agent": os.getenv(
        "AMAZON_USER_AGENT",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ar-EG,ar;q=0.9,en;q=0.7",
    "Accept": (
        "text/html,application/xhtml+xml,"
        "application/xml;q=0.9,*/*;q=0.8"
    ),
})


def connect():
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():

    with closing(connect()) as db:

        db.execute("""
            CREATE TABLE IF NOT EXISTS products (
                asin TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                price REAL,
                image_url TEXT,
                product_url TEXT NOT NULL,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS scan_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                next_page INTEGER NOT NULL DEFAULT 1,
                completed INTEGER NOT NULL DEFAULT 0,
                last_success TEXT
            )
        """)

        db.execute("""
            INSERT OR IGNORE INTO scan_state
            (id, next_page, completed)
            VALUES (1, 1, 0)
        """)

        db.commit()


def fetch_page(page: int) -> str:

    params = dict(SEARCH_PARAMS)
    params["page"] = page

    logger.info("Starting Amazon page %s", page)

    for attempt in range(1, 4):

        try:

            response = session.get(
                BASE_URL,
                params=params,
                timeout=REQUEST_TIMEOUT
            )

            logger.info(
                "Amazon page %s: HTTP %s | attempt %s | "
                "bytes=%s | url=%s",
                page,
                response.status_code,
                attempt,
                len(response.content),
                response.url
            )

            if response.status_code in (429, 500, 502, 503, 504):

                if attempt < 3:
                    time.sleep(attempt * 30)
                    continue

            response.raise_for_status()

            html = response.text
            lowered = html.lower()

            if any(term in lowered for term in (
                "captcha",
                "robot check",
                "validatecaptcha",
                "enter the characters you see below",
            )):
                raise RuntimeError(
                    f"Amazon CAPTCHA detected on page {page}"
                )

            return html

        except requests.RequestException:

            logger.exception(
                "Request failed on page %s, attempt %s",
                page,
                attempt
            )

            if attempt == 3:
                raise

            time.sleep(attempt * 30)

    raise RuntimeError(
        f"Could not download Amazon page {page}"
    )


def diagnose_page(html: str, page: int):

    soup = BeautifulSoup(html, "html.parser")

    selectors = [
        '[data-component-type="s-search-result"]',
        '.s-result-item[data-asin]',
        '[data-asin]',
        '[data-csa-c-item-id]',
        'raw-html',
    ]

    logger.info(
        "========== PAGE %s DIAGNOSTIC ==========",
        page
    )

    for selector in selectors:

        found = soup.select(selector)

        logger.info(
            "Page %s selector %s: %s matches",
            page,
            selector,
            len(found)
        )

        if found and selector != "raw-html":

            sample = found[0].prettify()[:6000]

            logger.info(
                "Page %s sample for %s:\n%s",
                page,
                selector,
                sample
            )

    asin_nodes = soup.find_all(attrs={"data-asin": True})

    for node in asin_nodes[:5]:

        logger.info(
            "Page %s ASIN sample: asin=%s | tag=%s | classes=%s",
            page,
            node.get("data-asin"),
            node.name,
            node.get("class")
        )

    logger.info(
        "========== END DIAGNOSTIC =========="
    )


def save_products(products: list[dict]) -> int:

    now = datetime.now(timezone.utc).isoformat(
        timespec="seconds"
    )

    inserted = 0

    with closing(connect()) as db:

        for p in products:

            existing = db.execute(
                "SELECT asin FROM products WHERE asin=?",
                (p["asin"],)
            ).fetchone()

            if existing:

                db.execute("""
                    UPDATE products
                    SET title=?, price=?, image_url=?,
                        product_url=?, last_seen=?
                    WHERE asin=?
                """, (
                    p["title"],
                    p["price"],
                    p["image_url"],
                    p["product_url"],
                    now,
                    p["asin"]
                ))

            else:

                db.execute("""
                    INSERT INTO products (
                        asin, title, price, image_url,
                        product_url, first_seen, last_seen
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    p["asin"],
                    p["title"],
                    p["price"],
                    p["image_url"],
                    p["product_url"],
                    now,
                    now
                ))

                inserted += 1

        db.commit()

    logger.info(
        "Saved %s products. Newly inserted: %s",
        len(products),
        inserted
    )

    return inserted


def save_progress(next_page: int, completed: bool):

    now = datetime.now(timezone.utc).isoformat()

    with closing(connect()) as db:

        db.execute("""
            UPDATE scan_state
            SET next_page=?, completed=?, last_success=?
            WHERE id=1
        """, (
            next_page,
            int(completed),
            now
        ))

        db.commit()


def run_check() -> dict:

    with closing(connect()) as db:

        state = db.execute(
            "SELECT * FROM scan_state WHERE id=1"
        ).fetchone()

    start_page = state["next_page"]

    if state["completed"]:
        start_page = 1

    end_page = min(
        start_page + MAX_PAGES_PER_RUN - 1,
        MAX_PAGES
    )

    pages_scanned = 0
    empty_pages = 0
    parsed_total = 0
    new_total = 0

    completed = False
    next_page = start_page
    now = None

    logger.info(
        "Scan started: pages %s to %s",
        start_page,
        end_page
    )

    for page in range(start_page, end_page + 1):

        html = fetch_page(page)

        pages_scanned += 1

        logger.info(
            "Page %s downloaded. HTML length=%s",
            page,
            len(html)
        )

        products = parse_resale_html(html)

        logger.info(
            "Page %s parsing result: %s products",
            page,
            len(products)
        )

        # الصفحة الفارغة يتم تجاوزها دون إيقاف الفحص
        if not products:

            empty_pages += 1

            logger.warning(
                "Page %s has no products. Skipping to next page.",
                page
            )

            diagnose_page(html, page)

        else:

            new_count = save_products(products)

            new_total += new_count
            parsed_total += len(products)

        # حفظ التقدم بعد كل صفحة، سواء كانت فارغة أم لا
        completed = page >= MAX_PAGES

        next_page = 1 if completed else page + 1

        save_progress(next_page, completed)

        now = datetime.now(timezone.utc).isoformat()

        logger.info(
            "Page %s completed. Products=%s | New=%s | "
            "Empty pages=%s | Next page=%s",
            page,
            len(products),
            new_total,
            empty_pages,
            next_page
        )

        if page < end_page:
            time.sleep(PAGE_DELAY)

    result = {
        "pages_scanned": pages_scanned,
        "first_page": start_page,
        "last_page": start_page + pages_scanned - 1,
        "parsed": parsed_total,
        "new": new_total,
        "empty_pages": empty_pages,
        "initial_scan_completed": completed,
        "next_page": next_page,
        "checked_at": now,
    }

    logger.info("Scan finished: %s", result)

    return result


@app.on_event("startup")
def startup():

    init_db()

    logger.info(
        "Application started. Database=%s",
        DB_PATH
    )

    if os.getenv("RUN_SCHEDULER", "1") == "1":

        scheduler.add_job(
            run_check,
            "interval",
            seconds=INTERVAL_SECONDS,
            id="resale-check",
            max_instances=1,
            coalesce=True
        )

        scheduler.start()

        logger.info(
            "Scheduler started. Interval=%s seconds",
            INTERVAL_SECONDS
        )


@app.on_event("shutdown")
def shutdown():

    if scheduler.running:
        scheduler.shutdown(wait=False)


@app.get("/")
def home():

    return FileResponse(
        ROOT / "templates" / "index.html"
    )


@app.get("/api/products")
def products():

    with closing(connect()) as db:

        rows = db.execute("""
            SELECT * FROM products
            ORDER BY first_seen DESC, rowid DESC
        """).fetchall()

    return [dict(row) for row in rows]


@app.get("/api/status")
def status():

    with closing(connect()) as db:

        count = db.execute(
            "SELECT COUNT(*) FROM products"
        ).fetchone()[0]

        state = db.execute(
            "SELECT * FROM scan_state WHERE id=1"
        ).fetchone()

    return {
        "saved_products": count,
        "interval_seconds": INTERVAL_SECONDS,
        "scheduler_running": scheduler.running,
        "next_page": state["next_page"],
        "initial_scan_completed": bool(state["completed"]),
        "last_success": state["last_success"],
    }


@app.post("/api/check")
def manual_check():

    try:
        return run_check()

    except Exception as exc:

        logger.exception("Manual scan failed")

        raise HTTPException(
            status_code=502,
            detail=str(exc)
        ) from exc
