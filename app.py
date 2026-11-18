
from __future__ import annotations

import os
import time
import sqlite3
import logging
import threading

from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

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


# ==========================================
# إعدادات عامة
# ==========================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

logger = logging.getLogger("resale-radar-v2")

ROOT = Path(__file__).resolve().parent

# قاعدة بيانات مستقلة للنظام الجديد
DB_PATH = ROOT / "resale_v2.db"

INTERVAL_SECONDS = max(
    60,
    int(os.getenv("CHECK_INTERVAL_SECONDS", "300"))
)

REQUEST_TIMEOUT = int(
    os.getenv("REQUEST_TIMEOUT_SECONDS", "30")
)

PAGE_DELAY = max(
    1,
    int(os.getenv("PAGE_DELAY_SECONDS", "2"))
)

app = FastAPI(title="Amazon Resale Radar")

app.mount(
    "/static",
    StaticFiles(directory=ROOT / "static"),
    name="static"
)

scheduler = BackgroundScheduler(timezone="UTC")
scan_lock = threading.Lock()

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


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(
        timespec="seconds"
    )


# ==========================================
# قاعدة البيانات
# ==========================================

def connect():
    db = sqlite3.connect(DB_PATH, timeout=30)
    db.row_factory = sqlite3.Row
    return db


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
                first_seen TEXT NOT NULL,
                PRIMARY KEY (asin, category_key),
                FOREIGN KEY (asin) REFERENCES products(asin)
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS scan_state (
                category_key TEXT PRIMARY KEY,
                next_url TEXT,
                completed INTEGER NOT NULL DEFAULT 0,
                last_success TEXT,
                last_error TEXT,
                last_parsed INTEGER NOT NULL DEFAULT 0,
                last_new INTEGER NOT NULL DEFAULT 0
            )
        """)

        db.execute("""
            CREATE INDEX IF NOT EXISTS idx_products_first_seen
            ON products(first_seen)
        """)

        db.execute("""
            CREATE INDEX IF NOT EXISTS idx_product_categories_key
            ON product_categories(category_key)
        """)

        # إنشاء مؤشر مستقل لكل مسار
        for key in CATEGORIES:
            db.execute("""
                INSERT OR IGNORE INTO scan_state(category_key)
                VALUES (?)
            """, (key,))

        db.commit()

    logger.info(
        "Database initialized: %s | routes=%s",
        DB_PATH,
        len(CATEGORIES)
    )


# ==========================================
# جلب صفحات أمازون
# ==========================================

def fetch_url(url: str) -> tuple[str, str]:

    logger.info("Requesting Amazon URL: %s", url)

    for attempt in range(1, 4):

        try:
            response = session.get(
                url,
                timeout=REQUEST_TIMEOUT
            )

            logger.info(
                "Amazon HTTP %s | attempt=%s | bytes=%s",
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

            final_url = response.url
            host = urlparse(final_url).netloc.lower()

            if host not in ("amazon.eg", "www.amazon.eg"):
                raise RuntimeError(
                    "Amazon redirected to an unexpected host"
                )

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

            return html, final_url

        except requests.RequestException:
            logger.exception(
                "Amazon request failed, attempt=%s",
                attempt
            )

            if attempt == 3:
                raise

            time.sleep(attempt * 20)

    raise RuntimeError("Could not download Amazon page")


# ==========================================
# حفظ المنتجات ومنع التكرار
# ==========================================

def save_products(
    products: list[dict],
    category_key: str
) -> int:

    now = utc_now()
    inserted = 0

    with closing(connect()) as db:

        for product in products:

            asin = product.get("asin")

            if not asin:
                continue

            exists = db.execute(
                "SELECT asin FROM products WHERE asin=?",
                (asin,)
            ).fetchone()

            if exists:

                db.execute("""
                    UPDATE products
                    SET title=?,
                        price=COALESCE(?, price),
                        image_url=COALESCE(?, image_url),
                        product_url=?,
                        last_seen=?
                    WHERE asin=?
                """, (
                    product.get("title") or "",
                    product.get("price"),
                    product.get("image_url"),
                    product.get("product_url")
                    or f"https://www.amazon.eg/dp/{asin}",
                    now,
                    asin
                ))

            else:

                db.execute("""
                    INSERT INTO products (
                        asin,
                        title,
                        price,
                        image_url,
                        product_url,
                        first_seen,
                        last_seen
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    asin,
                    product.get("title") or "منتج بدون اسم",
                    product.get("price"),
                    product.get("image_url"),
                    product.get("product_url")
                    or f"https://www.amazon.eg/dp/{asin}",
                    now,
                    now
                ))

                inserted += 1

            # المنتج يمكن أن يرتبط بأكثر من مسار
            db.execute("""
                INSERT OR IGNORE INTO product_categories (
                    asin,
                    category_key,
                    first_seen
                )
                VALUES (?, ?, ?)
            """, (
                asin,
                category_key,
                now
            ))

        db.commit()

    logger.info(
        "Route=%s | parsed=%s | new=%s",
        category_key,
        len(products),
        inserted
    )

    return inserted


# ==========================================
# تحديث حالة مسار الفحص
# ==========================================

def update_scan_state(
    category_key: str,
    next_url: str | None,
    completed: bool,
    parsed: int,
    new: int,
    error: str | None = None
):

    with closing(connect()) as db:

        db.execute("""
            UPDATE scan_state
            SET next_url=?,
                completed=?,
                last_success=?,
                last_error=?,
                last_parsed=?,
                last_new=?
            WHERE category_key=?
        """, (
            next_url,
            int(completed),
            utc_now() if error is None else None,
            error[:1000] if error else None,
            parsed,
            new,
            category_key
        ))

        db.commit()


# ==========================================
# فحص مسار واحد
# ==========================================

def scan_route(category_key: str) -> dict:

    if category_key not in CATEGORIES:
        raise ValueError("Unknown category")

    with closing(connect()) as db:
        state = db.execute("""
            SELECT *
            FROM scan_state
            WHERE category_key=?
        """, (category_key,)).fetchone()

    if not state:
        raise RuntimeError("Scan state not initialized")

    saved_next_url = state["next_url"]
    was_completed = bool(state["completed"])

    first_url = build_start_url(category_key)

    pages = 0
    parsed = 0
    new = 0
    completed = was_completed
    error = None
    next_cursor = saved_next_url

    try:

        # ----------------------------------
        # أولاً: فحص الصفحة الأولى دائمًا
        # ----------------------------------

        html, final_url = fetch_url(first_url)

        first_products = parse_resale_html(html)

        if not first_products:
            raise RuntimeError(
                "No products parsed from first page; "
                "pagination state preserved"
            )

        first_next = extract_next_page_url(
            html,
            final_url
        )

        new += save_products(
            first_products,
            category_key
        )

        pages += 1
        parsed += len(first_products)

        # إذا كان المسار مكتملًا سابقًا،
        # نستمر في مراقبة الصفحة الأولى فقط.
        if was_completed:
            next_cursor = None
            completed = True

        else:

            # أول صفحة عميقة هي الصفحة التالية
            deep_url = saved_next_url or first_next

            if deep_url and deep_url != final_url:

                time.sleep(PAGE_DELAY)

                deep_html, deep_final_url = fetch_url(
                    deep_url
                )

                deep_products = parse_resale_html(
                    deep_html
                )

                if not deep_products:
                    # لا نخترع صفحة جديدة.
                    # نوقف التوسع العميق لهذا المسار
                    # ونستمر في مراقبة الصفحة الأولى.
                    completed = True
                    next_cursor = None

                    logger.warning(
                        "Route=%s reached an empty deep page",
                        category_key
                    )

                else:

                    deep_next = extract_next_page_url(
                        deep_html,
                        deep_final_url
                    )

                    new += save_products(
                        deep_products,
                        category_key
                    )

                    pages += 1
                    parsed += len(deep_products)

                    if deep_next:
                        next_cursor = deep_next
                        completed = False
                    else:
                        next_cursor = None
                        completed = True

            else:
                next_cursor = None
                completed = True

        update_scan_state(
            category_key=category_key,
            next_url=next_cursor,
            completed=completed,
            parsed=parsed,
            new=new
        )

    except Exception as exc:

        error = str(exc)

        logger.exception(
            "Route scan failed: %s",
            category_key
        )

        # لا نغيّر مؤشر الصفحة عند فشل الطلب
        update_scan_state(
            category_key=category_key,
            next_url=saved_next_url,
            completed=was_completed,
            parsed=parsed,
            new=new,
            error=error
        )

    category = CATEGORIES[category_key]

    return {
        "key": category_key,
        "name": category["name"],
        "parent": category["parent"],
        "pages_scanned": pages,
        "parsed": parsed,
        "new": new,
        "completed": completed,
        "next_url": next_cursor,
        "error": error
    }


# ==========================================
# دورة الفحص الكاملة
# ==========================================

def run_check() -> dict:

    if not scan_lock.acquire(blocking=False):
        return {
            "busy": True,
            "message": "A scan is already running"
        }

    try:

        results = []

        for key in CATEGORIES:

            result = scan_route(key)
            results.append(result)

        summary = {
            "routes": results,
            "routes_count": len(results),
            "pages_scanned": sum(
                item["pages_scanned"]
                for item in results
            ),
            "parsed": sum(
                item["parsed"]
                for item in results
            ),
            "new": sum(
                item["new"]
                for item in results
            ),
            "errors": sum(
                1 for item in results
                if item["error"]
            )
        }

        logger.info(
            "Scan cycle finished: %s",
            summary
        )

        return summary

    finally:
        scan_lock.release()


# ==========================================
# تشغيل التطبيق والجدولة
# ==========================================

@app.on_event("startup")
def startup():

    init_db()

    logger.info(
        "Application started | database=%s",
        DB_PATH
    )

    # الجدولة متوقفة افتراضيًا
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

    else:
        logger.info("Scheduler is disabled")


@app.on_event("shutdown")
def shutdown():

    if scheduler.running:
        scheduler.shutdown(wait=False)


# ==========================================
# صفحات الموقع
# ==========================================

@app.get("/")
def home():

    return FileResponse(
        ROOT / "templates" / "index.html"
    )


# ==========================================
# قائمة الأقسام والمسارات
# ==========================================

@app.get("/api/categories")
def categories():

    with closing(connect()) as db:

        states = {
            row["category_key"]: dict(row)
            for row in db.execute(
                "SELECT * FROM scan_state"
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

    result = []

    for key, value in CATEGORIES.items():

        state = states.get(key, {})

        result.append({
            "key": key,
            "name": value["name"],
            "parent": value["parent"],
            "count": counts.get(key, 0),
            "completed": bool(state.get("completed", 0)),
            "last_success": state.get("last_success"),
            "last_error": state.get("last_error"),
            "last_parsed": state.get("last_parsed", 0),
            "last_new": state.get("last_new", 0)
        })

    return result


# ==========================================
# المنتجات
# ==========================================

@app.get("/api/products")
def products(category: str | None = None):

    with closing(connect()) as db:

        if category and category not in CATEGORIES:
            # السماح أيضًا بالتصفية باسم القسم الرئيسي
            parents = {
                value["parent"]
                for value in CATEGORIES.values()
            }

            if category not in parents:
                raise HTTPException(
                    status_code=400,
                    detail="Unknown category"
                )

            rows = db.execute("""
                SELECT DISTINCT p.*
                FROM products p
                JOIN product_categories pc
                    ON pc.asin=p.asin
                JOIN scan_state ss
                    ON ss.category_key=pc.category_key
                WHERE pc.category_key IN (
                    SELECT category_key
                    FROM scan_state
                )
                ORDER BY p.first_seen DESC
            """).fetchall()

            # فلترة القسم الرئيسي من مفاتيح المسارات
            allowed_keys = [
                key for key, value in CATEGORIES.items()
                if value["parent"] == category
            ]

            if allowed_keys:
                placeholders = ",".join(
                    "?" for _ in allowed_keys
                )

                rows = db.execute(f"""
                    SELECT DISTINCT p.*
                    FROM products p
                    JOIN product_categories pc
                        ON pc.asin=p.asin
                    WHERE pc.category_key IN ({placeholders})
                    ORDER BY p.first_seen DESC
                """, allowed_keys).fetchall()

        elif category:

            rows = db.execute("""
                SELECT p.*
                FROM products p
                JOIN product_categories pc
                    ON pc.asin=p.asin
                WHERE pc.category_key=?
                ORDER BY p.first_seen DESC
            """, (category,)).fetchall()

        else:

            rows = db.execute("""
                SELECT *
                FROM products
                ORDER BY first_seen DESC
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


# ==========================================
# حالة النظام
# ==========================================

@app.get("/api/status")
def status():

    with closing(connect()) as db:

        count = db.execute(
            "SELECT COUNT(*) FROM products"
        ).fetchone()[0]

        route_count = db.execute(
            "SELECT COUNT(*) FROM scan_state"
        ).fetchone()[0]

        states = [
            dict(row)
            for row in db.execute("""
                SELECT *
                FROM scan_state
                ORDER BY category_key
            """).fetchall()
        ]

    return {
        "system": "Amazon Resale Radar 2.0",
        "database": DB_PATH.name,
        "saved_products": count,
        "routes_count": route_count,
        "interval_seconds": INTERVAL_SECONDS,
        "scheduler_running": scheduler.running,
        "scan_running": scan_lock.locked(),
        "routes": states
    }


# ==========================================
# تشغيل فحص يدوي
# ==========================================

@app.post("/api/check")
def manual_check():

    if scan_lock.locked():
        raise HTTPException(
            status_code=409,
            detail="A scan is already running"
        )

    try:
        return run_check()

    except Exception as exc:

        logger.exception("Manual scan failed")

        raise HTTPException(
            status_code=502,
            detail=str(exc)
        ) from exc
