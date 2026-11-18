
from __future__ import annotations

import os
import time
import sqlite3
import logging

from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import requests
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from scraper import (
    CATEGORIES,
    build_start_url,
    extract_next_page_url,
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
    30,
    int(os.getenv("CHECK_INTERVAL_SECONDS", "30"))
)

REQUEST_TIMEOUT = int(
    os.getenv("REQUEST_TIMEOUT_SECONDS", "30")
)

PAGE_DELAY = max(
    0,
    int(os.getenv("PAGE_DELAY_SECONDS", "5"))
)

MAX_PAGES_PER_RUN = max(
    1,
    int(os.getenv("MAX_PAGES_PER_RUN", "1"))
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
    conn = sqlite3.connect(DB_PATH, timeout=30)
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
            CREATE TABLE IF NOT EXISTS product_categories (
                asin TEXT NOT NULL,
                category_key TEXT NOT NULL,
                PRIMARY KEY (asin, category_key),
                FOREIGN KEY (asin) REFERENCES products(asin)
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS category_scan_state (
                category_key TEXT PRIMARY KEY,
                next_url TEXT,
                completed INTEGER NOT NULL DEFAULT 0,
                last_success TEXT,
                last_error TEXT
            )
        """)

        for key in CATEGORIES:
            db.execute("""
                INSERT OR IGNORE INTO category_scan_state
                (category_key)
                VALUES (?)
            """, (key,))

        db.commit()


def fetch_url(url: str) -> tuple[str, str]:

    logger.info("Requesting Amazon URL: %s", url)

    for attempt in range(1, 4):

        try:

            response = session.get(
                url,
                timeout=REQUEST_TIMEOUT
            )

            logger.info(
                "Amazon response HTTP %s | attempt=%s | bytes=%s",
                response.status_code,
                attempt,
                len(response.content)
            )

            if response.status_code in (
                429, 500, 502, 503, 504
            ):

                if attempt < 3:
                    time.sleep(attempt * 20)
                    continue

            response.raise_for_status()

            html = response.text
            lowered = html.lower()

            if any(term in lowered for term in (
                "captcha",
                "robot check",
                "validatecaptcha",
                "enter the characters you see below"
            )):
                raise RuntimeError(
                    "Amazon CAPTCHA detected; scan paused"
                )

            return html, response.url

        except requests.RequestException:

            logger.exception(
                "Amazon request failed, attempt %s",
                attempt
            )

            if attempt == 3:
                raise

            time.sleep(attempt * 20)

    raise RuntimeError("Could not download Amazon page")


def save_products(
    products: list[dict],
    category_key: str
) -> int:

    now = datetime.now(timezone.utc).isoformat(
        timespec="seconds"
    )

    inserted = 0

    with closing(connect()) as db:

        for product in products:

            exists = db.execute(
                "SELECT asin FROM products WHERE asin=?",
                (product["asin"],)
            ).fetchone()

            if exists:

                db.execute("""
                    UPDATE products
                    SET title=?, price=?, image_url=?,
                        product_url=?, last_seen=?
                    WHERE asin=?
                """, (
                    product["title"],
                    product["price"],
                    product["image_url"],
                    product["product_url"],
                    now,
                    product["asin"]
                ))

            else:

                db.execute("""
                    INSERT INTO products (
                        asin, title, price, image_url,
                        product_url, first_seen, last_seen
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    product["asin"],
                    product["title"],
                    product["price"],
                    product["image_url"],
                    product["product_url"],
                    now,
                    now
                ))

                inserted += 1

            db.execute("""
                INSERT OR IGNORE INTO product_categories
                (asin, category_key)
                VALUES (?, ?)
            """, (
                product["asin"],
                category_key
            ))

        db.commit()

    logger.info(
        "Category=%s | products=%s | new=%s",
        category_key,
        len(products),
        inserted
    )

    return inserted


def scan_category(
    category_key: str,
    max_pages: int = MAX_PAGES_PER_RUN
) -> dict:

    if category_key not in CATEGORIES:
        raise ValueError("Unknown category")

    with closing(connect()) as db:

        state = db.execute("""
            SELECT *
            FROM category_scan_state
            WHERE category_key=?
        """, (category_key,)).fetchone()

    url = (
        state["next_url"]
        or build_start_url(category_key)
    )

    pages = 0
    parsed = 0
    new = 0
    completed = False
    last_error = None

    for index in range(max_pages):

        try:

            html, final_url = fetch_url(url)

            products = parse_resale_html(html)

            next_url = extract_next_page_url(
                html,
                final_url
            )

            new += save_products(
                products,
                category_key
            )

            pages += 1
            parsed += len(products)

            now = datetime.now(
                timezone.utc
            ).isoformat(timespec="seconds")

            if next_url:
                url = next_url
            else:
                completed = True
                url = None

            with closing(connect()) as db:

                db.execute("""
                    UPDATE category_scan_state
                    SET next_url=?,
                        completed=?,
                        last_success=?,
                        last_error=NULL
                    WHERE category_key=?
                """, (
                    url,
                    int(completed),
                    now,
                    category_key
                ))

                db.commit()

            if completed or index + 1 >= max_pages:
                break

            if PAGE_DELAY:
                time.sleep(PAGE_DELAY)

        except Exception as exc:

            last_error = str(exc)

            logger.exception(
                "Category scan failed: %s",
                category_key
            )

            with closing(connect()) as db:

                db.execute("""
                    UPDATE category_scan_state
                    SET last_error=?
                    WHERE category_key=?
                """, (
                    last_error[:1000],
                    category_key
                ))

                db.commit()

            break

    return {
        "category": category_key,
        "name": CATEGORIES[category_key]["name"],
        "pages_scanned": pages,
        "parsed": parsed,
        "new": new,
        "completed": completed,
        "next_url": url,
        "error": last_error
    }


def run_check() -> dict:

    results = []

    for key in CATEGORIES:
        results.append(scan_category(key))

    summary = {
        "categories": results,
        "pages_scanned": sum(
            item["pages_scanned"] for item in results
        ),
        "parsed": sum(
            item["parsed"] for item in results
        ),
        "new": sum(
            item["new"] for item in results
        )
    }

    logger.info(
        "Scan cycle finished: %s",
        summary
    )

    return summary


@app.on_event("startup")
def startup():

    init_db()

    logger.info(
        "Application started. Database=%s",
        DB_PATH
    )

    if os.getenv("RUN_SCHEDULER", "0") == "1":

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
            "Scheduler started: %s seconds",
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


@app.get("/api/categories")
def categories():

    with closing(connect()) as db:

        states = {
            row["category_key"]: dict(row)
            for row in db.execute(
                "SELECT * FROM category_scan_state"
            ).fetchall()
        }

        counts = {
            row["category_key"]: row["total"]
            for row in db.execute("""
                SELECT category_key, COUNT(*) AS total
                FROM product_categories
                GROUP BY category_key
            """).fetchall()
        }

    return [
        {
            "key": key,
            "name": value["name"],
            "count": counts.get(key, 0),
            "completed": bool(
                states.get(key, {}).get("completed", 0)
            ),
            "last_success": states.get(key, {}).get(
                "last_success"
            ),
            "last_error": states.get(key, {}).get(
                "last_error"
            )
        }
        for key, value in CATEGORIES.items()
    ]


@app.get("/api/products")
def products(category: str | None = None):

    with closing(connect()) as db:

        if category and category not in CATEGORIES:
            raise HTTPException(
                status_code=400,
                detail="Unknown category"
            )

        if category:

            rows = db.execute("""
                SELECT p.*
                FROM products p
                JOIN product_categories pc
                    ON pc.asin=p.asin
                WHERE pc.category_key=?
                ORDER BY p.first_seen DESC, p.rowid DESC
            """, (category,)).fetchall()

        else:

            rows = db.execute("""
                SELECT *
                FROM products
                ORDER BY first_seen DESC, rowid DESC
            """).fetchall()

        result = []

        for row in rows:

            item = dict(row)

            item["categories"] = [
                r["category_key"]
                for r in db.execute("""
                    SELECT category_key
                    FROM product_categories
                    WHERE asin=?
                """, (item["asin"],)).fetchall()
            ]

            result.append(item)

    return result


@app.get("/api/status")
def status():

    with closing(connect()) as db:

        count = db.execute(
            "SELECT COUNT(*) FROM products"
        ).fetchone()[0]

        states = [
            dict(row)
            for row in db.execute(
                "SELECT * FROM category_scan_state"
            ).fetchall()
        ]

    return {
        "saved_products": count,
        "interval_seconds": INTERVAL_SECONDS,
        "scheduler_running": scheduler.running,
        "categories": states
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
