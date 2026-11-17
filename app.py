
from __future__ import annotations

import os
import time
import sqlite3
import logging

from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import requests

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from scraper import (
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
    5,
    int(os.getenv("PAGE_DELAY_SECONDS", "5"))
)

MAX_BATCHES_PER_RUN = max(
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
            CREATE TABLE IF NOT EXISTS scan_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                next_page INTEGER NOT NULL DEFAULT 1,
                completed INTEGER NOT NULL DEFAULT 0,
                last_success TEXT
            )
        """)

        columns = {
            row["name"]
            for row in db.execute(
                "PRAGMA table_info(scan_state)"
            ).fetchall()
        }

        if "next_url" not in columns:
            db.execute("""
                ALTER TABLE scan_state
                ADD COLUMN next_url TEXT
            """)

        db.execute("""
            INSERT OR IGNORE INTO scan_state
            (id, next_page, completed)
            VALUES (1, 1, 0)
        """)

        db.commit()


def page_number_from_url(url: str | None) -> int:

    if not url:
        return 1

    try:
        values = parse_qs(urlparse(url).query)
        return int(values.get("page", ["1"])[0])
    except (ValueError, TypeError):
        return 1


def fetch_url(url: str) -> tuple[str, str]:

    logger.info("Requesting Amazon URL: %s", url)

    for attempt in range(1, 4):

        try:

            response = session.get(
                url,
                timeout=REQUEST_TIMEOUT
            )

            logger.info(
                "Amazon response: HTTP %s | attempt=%s | "
                "bytes=%s | final_url=%s",
                response.status_code,
                attempt,
                len(response.content),
                response.url
            )

            if response.status_code in (
                429, 500, 502, 503, 504
            ):

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
                    "Amazon CAPTCHA detected"
                )

            return html, response.url

        except requests.RequestException:

            logger.exception(
                "Request failed, attempt %s",
                attempt
            )

            if attempt == 3:
                raise

            time.sleep(attempt * 30)

    raise RuntimeError("Could not download Amazon page")


def save_products(products: list[dict]) -> int:

    now = datetime.now(timezone.utc).isoformat(
        timespec="seconds"
    )

    inserted = 0

    with closing(connect()) as db:

        for product in products:

            existing = db.execute(
                "SELECT asin FROM products WHERE asin=?",
                (product["asin"],)
            ).fetchone()

            if existing:

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

        db.commit()

    logger.info(
        "Saved products=%s | newly inserted=%s",
        len(products),
        inserted
    )

    return inserted


def save_progress(
    next_url: str | None,
    completed: bool
):

    now = datetime.now(timezone.utc).isoformat()

    next_page = page_number_from_url(next_url)

    with closing(connect()) as db:

        db.execute("""
            UPDATE scan_state
            SET next_url=?,
                next_page=?,
                completed=?,
                last_success=?
            WHERE id=1
        """, (
            next_url,
            next_page,
            int(completed),
            now
        ))

        db.commit()


def scan_page(url: str) -> dict:

    html, final_url = fetch_url(url)

    products = parse_resale_html(html)

    next_url = extract_next_page_url(
        html,
        final_url
    )

    new_count = 0

    if products:
        new_count = save_products(products)

    logger.info(
        "Page scan result: products=%s | new=%s | "
        "has_next=%s | url=%s",
        len(products),
        new_count,
        bool(next_url),
        final_url
    )

    if not products and not next_url:
        logger.warning(
            "No products and no next-page link. "
            "Amazon may have returned an empty or unexpected page."
        )

    return {
        "url": final_url,
        "products": len(products),
        "new": new_count,
        "next_url": next_url,
    }


def run_check() -> dict:

    with closing(connect()) as db:

        state = db.execute(
            "SELECT * FROM scan_state WHERE id=1"
        ).fetchone()

    start_url = build_start_url()

    pages_scanned = 0
    parsed_total = 0
    new_total = 0
    completed = False

    logger.info("Starting fresh first-page check")

    # فحص الصفحة الأولى في كل دورة لاكتشاف الإضافات الجديدة
    first_result = scan_page(start_url)

    pages_scanned += 1
    parsed_total += first_result["products"]
    new_total += first_result["new"]

    first_next_url = first_result["next_url"]

    # استكمال الفحص العميق من آخر رابط محفوظ
    deep_url = state["next_url"] or first_next_url

    # إذا لم يوجد رابط تالٍ، فلا توجد صفحات يمكن استكمالها
    if not deep_url:

        save_progress(None, True)

        return {
            "pages_scanned": pages_scanned,
            "parsed": parsed_total,
            "new": new_total,
            "completed": True,
            "next_page": 1,
            "message": "No next-page link was found.",
            "checked_at": datetime.now(
                timezone.utc
            ).isoformat(),
        }

    visited = {start_url}
    last_next_url = deep_url

    for batch_number in range(1, MAX_BATCHES_PER_RUN + 1):

        if not deep_url:
            completed = True
            break

        if deep_url in visited:

            logger.warning(
                "Repeated page URL detected. "
                "Restarting catalog cycle."
            )

            deep_url = first_next_url
            completed = True
            break

        visited.add(deep_url)

        logger.info(
            "Deep scan batch %s/%s",
            batch_number,
            MAX_BATCHES_PER_RUN
        )

        result = scan_page(deep_url)

        pages_scanned += 1
        parsed_total += result["products"]
        new_total += result["new"]

        next_url = result["next_url"]

        if not next_url:

            logger.info(
                "No further page link found. "
                "Catalog traversal reached its end."
            )

            completed = True
            deep_url = first_next_url
            break

        if next_url in visited:

            logger.warning(
                "Amazon returned a repeated next-page link."
            )

            completed = True
            deep_url = first_next_url
            break

        deep_url = next_url
        last_next_url = deep_url

        save_progress(
            deep_url,
            False
        )

        if batch_number < MAX_BATCHES_PER_RUN:
            time.sleep(PAGE_DELAY)

    if completed:

        save_progress(
            deep_url,
            True
        )

    else:

        save_progress(
            deep_url or last_next_url,
            False
        )

    result = {
        "pages_scanned": pages_scanned,
        "parsed": parsed_total,
        "new": new_total,
        "completed": completed,
        "next_page": page_number_from_url(deep_url),
        "checked_at": datetime.now(
            timezone.utc
        ).isoformat(),
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
        "initial_scan_completed": bool(
            state["completed"]
        ),
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
