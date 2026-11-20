
from __future__ import annotations

import os
import re
import time
import sqlite3
import logging
import threading

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
    CATEGORIES,
    build_start_url,
    extract_next_page_url,
    parse_resale_html,
)


# ==========================================
# الإعدادات العامة
# ==========================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

logger = logging.getLogger("resale-radar-v2")

ROOT = Path(__file__).resolve().parent

DB_PATH = ROOT / "resale_v2.db"

# الحد الأقصى للمنتجات المحفوظة
MAX_STORED_PRODUCTS = 200

# السماح بفاصل 30 ثانية
INTERVAL_SECONDS = max(
    30,
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


# ==========================================
# الاحتفاظ بأحدث 200 منتج فقط
# ==========================================

def prune_old_products(db):
    """
    Keep only the newest 200 products.

    Older products and their category associations
    are deleted.

    Scan states and application metadata are preserved.
    """

    # حذف ارتباطات المنتجات الأقدم
    db.execute("""
        DELETE FROM product_categories
        WHERE asin IN (
            SELECT asin
            FROM products
            ORDER BY first_seen DESC, rowid DESC
            LIMIT -1 OFFSET ?
        )
    """, (MAX_STORED_PRODUCTS,))

    # حذف المنتجات الأقدم
    cursor = db.execute("""
        DELETE FROM products
        WHERE asin IN (
            SELECT asin
            FROM products
            ORDER BY first_seen DESC, rowid DESC
            LIMIT -1 OFFSET ?
        )
    """, (MAX_STORED_PRODUCTS,))

    deleted = cursor.rowcount

    if deleted:
        logger.info(
            "Product cleanup: deleted=%s | limit=%s",
            deleted,
            MAX_STORED_PRODUCTS
        )

    return deleted


# ==========================================
# تهيئة قاعدة البيانات والترقيات
# ==========================================

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
            CREATE TABLE IF NOT EXISTS app_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)

        # إضافة عمود التوقف المؤقت إن لم يكن موجودًا
        columns = {
            row["name"]
            for row in db.execute(
                "PRAGMA table_info(scan_state)"
            ).fetchall()
        }

        if "pagination_blocked_url" not in columns:
            db.execute("""
                ALTER TABLE scan_state
                ADD COLUMN pagination_blocked_url TEXT
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

        # ترقية واحدة فقط:
        # إعادة فتح المسارات التي أُغلقت بالمنطق القديم
        migration = db.execute("""
            SELECT value
            FROM app_meta
            WHERE key='pagination_logic_v2'
        """).fetchone()

        if not migration:

            db.execute("""
                UPDATE scan_state
                SET completed=0,
                    pagination_blocked_url=NULL
                WHERE completed=1
            """)

            db.execute("""
                INSERT INTO app_meta(key, value)
                VALUES ('pagination_logic_v2', '1')
            """)

            logger.info(
                "Pagination migration completed"
            )

        # تنظيف المنتجات مع الحفاظ على مؤشرات الفحص
        prune_old_products(db)

        db.commit()

    logger.info(
        "Database initialized: %s | routes=%s",
        DB_PATH,
        len(CATEGORIES)
    )


# ==========================================
# التحقق من نهاية نتائج أمازون
# ==========================================

def is_explicit_end_page(html: str) -> bool:

    if not html:
        return False

    if re.search(
        r'class=["\'][^"\']*s-no-results',
        html,
        re.IGNORECASE
    ):
        return True

    text = re.sub(r"<[^>]+>", " ", html)
    text = text.lower()

    markers = (
        "we couldn't find any results",
        "we could not find any results",
        "no results found",
        "لم يتم العثور على نتائج",
        "لا توجد نتائج",
    )

    return any(marker in text for marker in markers)


# ==========================================
# التحقق من أن أمازون أعاد القسم المطلوب
# ==========================================

def validate_route_url(
    expected_url: str,
    final_url: str
) -> None:

    expected = urlparse(expected_url)
    actual = urlparse(final_url)

    host = actual.netloc.lower()

    if host not in ("amazon.eg", "www.amazon.eg"):
        raise RuntimeError(
            "Amazon redirected to an unexpected host"
        )

    expected_params = parse_qs(expected.query)
    actual_params = parse_qs(actual.query)

    expected_department = expected_params.get("i", [])
    actual_department = actual_params.get("i", [])

    if expected_department:
        if not actual_department:
            raise RuntimeError(
                "Amazon response lost the department filter"
            )

        if expected_department[0] != actual_department[0]:
            raise RuntimeError(
                "Amazon redirected to a different department"
            )

    expected_rh = expected_params.get("rh", [])
    actual_rh = actual_params.get("rh", [])

    if expected_rh:
        if not actual_rh:
            raise RuntimeError(
                "Amazon response lost the category filters"
            )

        expected_tokens = [
            token.strip()
            for token in expected_rh[0].split(",")
            if token.strip()
        ]

        actual_filter = actual_rh[0]

        for token in expected_tokens:
            if token not in actual_filter:
                raise RuntimeError(
                    "Amazon response changed the category filters"
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

        # الاحتفاظ بأحدث 200 منتج
        prune_old_products(db)

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
    error: str | None = None,
    blocked_url: str | None = None
):

    with closing(connect()) as db:

        if error is None:

            db.execute("""
                UPDATE scan_state
                SET next_url=?,
                    completed=?,
                    last_success=?,
                    last_error=NULL,
                    last_parsed=?,
                    last_new=?,
                    pagination_blocked_url=?
                WHERE category_key=?
            """, (
                next_url,
                int(completed),
                utc_now(),
                parsed,
                new,
                blocked_url,
                category_key
            ))

        else:

            # لا نمسح آخر توقيت فحص ناجح عند الخطأ
            db.execute("""
                UPDATE scan_state
                SET next_url=?,
                    completed=?,
                    last_error=?,
                    last_parsed=?,
                    last_new=?,
                    pagination_blocked_url=?
                WHERE category_key=?
            """, (
                next_url,
                int(completed),
                error[:1000],
                parsed,
                new,
                blocked_url,
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
    blocked_url = state["pagination_blocked_url"]
    was_completed = bool(state["completed"])

    first_url = build_start_url(category_key)

    pages = 0
    parsed = 0
    new = 0

    completed = was_completed
    error = None

    next_cursor = saved_next_url
    current_blocked_url = blocked_url

    try:

        # ----------------------------------
        # أولاً: فحص الصفحة الأولى دائمًا
        # ----------------------------------

        html, final_url = fetch_url(first_url)

        validate_route_url(first_url, final_url)

        first_products = parse_resale_html(html)

        if not first_products:

            if is_explicit_end_page(html):

                update_scan_state(
                    category_key,
                    None,
                    True,
                    0,
                    0,
                    blocked_url=None
                )

                return {
                    "key": category_key,
                    "name": CATEGORIES[category_key]["name"],
                    "parent": CATEGORIES[category_key]["parent"],
                    "pages_scanned": 1,
                    "parsed": 0,
                    "new": 0,
                    "completed": True,
                    "next_url": None,
                    "error": None
                }

            raise RuntimeError(
                "First page returned no parseable products; "
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

        # ----------------------------------
        # المسار المكتمل سابقًا
        # ----------------------------------

        if was_completed and not first_next:

            next_cursor = None
            completed = True
            current_blocked_url = None

        else:

            if was_completed and first_next:
                saved_next_url = None
                current_blocked_url = None

            # اختيار المؤشر المستقل لهذا المسار
            if saved_next_url and saved_next_url != current_blocked_url:
                deep_url = saved_next_url
            elif first_next and first_next != current_blocked_url:
                deep_url = first_next
                current_blocked_url = None
            else:
                deep_url = None

            if not deep_url:

                if current_blocked_url:
                    next_cursor = current_blocked_url
                    error = (
                        "Pagination paused: the previous page "
                        "could not be safely advanced"
                    )
                else:
                    next_cursor = None
                    error = (
                        "Pagination not confirmed: Amazon did not "
                        "provide a valid next-page link"
                    )

                completed = False

            else:

                if deep_url == current_blocked_url:

                    next_cursor = deep_url
                    completed = False

                    error = (
                        "Pagination paused: this page needs "
                        "further validation"
                    )

                else:

                    next_cursor = deep_url

                    time.sleep(PAGE_DELAY)

                    deep_html, deep_final_url = fetch_url(
                        deep_url
                    )

                    validate_route_url(
                        first_url,
                        deep_final_url
                    )

                    deep_products = parse_resale_html(
                        deep_html
                    )

                    if not deep_products:

                        if is_explicit_end_page(deep_html):

                            next_cursor = None
                            completed = True
                            current_blocked_url = None
                            error = None

                        else:

                            next_cursor = deep_url
                            completed = False
                            current_blocked_url = deep_url

                            error = (
                                "Pagination paused: deep page "
                                "returned no parseable products"
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
                            current_blocked_url = None
                            error = None

                        else:

                            next_cursor = deep_url
                            completed = False
                            current_blocked_url = deep_url

                            error = (
                                "Pagination paused: no verified "
                                "next-page link was found"
                            )

        update_scan_state(
            category_key=category_key,
            next_url=next_cursor,
            completed=completed,
            parsed=parsed,
            new=new,
            error=error,
            blocked_url=current_blocked_url
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
            next_url=next_cursor,
            completed=False,
            parsed=parsed,
            new=new,
            error=error,
            blocked_url=current_blocked_url
        )

        completed = False

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
            coalesce=True,
            replace_existing=True
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
# صفحة الموقع
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

            parents = {
                value["parent"]
                for value in CATEGORIES.values()
            }

            if category not in parents:
                raise HTTPException(
                    status_code=400,
                    detail="Unknown category"
                )

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

            else:
                rows = []

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
        result = run_check()

        if result.get("busy"):
            raise HTTPException(
                status_code=409,
                detail="A scan is already running"
            )

        return result

    except HTTPException:
        raise

    except Exception as exc:

        logger.exception("Manual scan failed")

        raise HTTPException(
            status_code=502,
            detail=str(exc)
        ) from exc
