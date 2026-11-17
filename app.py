from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import requests
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from scraper import BASE_URL, parse_resale_html

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("DATABASE_PATH", ROOT / "resale.db"))
INTERVAL_SECONDS = max(30, int(os.getenv("CHECK_INTERVAL_SECONDS", "30")))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "20"))

app = FastAPI(title="Amazon Resale Radar")
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
scheduler = BackgroundScheduler(timezone="UTC")


def connect():
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with closing(connect()) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS products (
            asin TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            price REAL,
            image_url TEXT,
            product_url TEXT NOT NULL,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL
        )""")
        db.commit()


def fetch_page() -> str:
    headers = {
        "User-Agent": os.getenv("AMAZON_USER_AGENT", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0 Safari/537.36"),
        "Accept-Language": "ar-EG,ar;q=0.9,en;q=0.7",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    response = requests.get(BASE_URL, headers=headers, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    html = response.text
    lowered = html.lower()
    if "captcha" in lowered or "robot check" in lowered or "validatecaptcha" in lowered:
        raise RuntimeError("Amazon returned a CAPTCHA/robot-check page; no products were processed.")
    return html


def save_products(products: list[dict]) -> int:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    inserted = 0
    with closing(connect()) as db:
        for p in products:
            existing = db.execute("SELECT asin FROM products WHERE asin=?", (p["asin"],)).fetchone()
            if existing:
                db.execute("""UPDATE products SET title=?, price=?, image_url=?, product_url=?, last_seen=?
                              WHERE asin=?""",
                           (p["title"], p["price"], p["image_url"], p["product_url"], now, p["asin"]))
            else:
                db.execute("""INSERT INTO products
                              (asin,title,price,image_url,product_url,first_seen,last_seen)
                              VALUES (?,?,?,?,?,?,?)""",
                           (p["asin"], p["title"], p["price"], p["image_url"], p["product_url"], now, now))
                inserted += 1
        db.commit()
    return inserted


def run_check() -> dict:
    html = fetch_page()
    products = parse_resale_html(html)
    if not products:
        raise RuntimeError("No product cards were parsed. Amazon may have changed the page structure or returned a different page.")
    new_count = save_products(products)
    return {"parsed": len(products), "new": new_count, "checked_at": datetime.now(timezone.utc).isoformat()}


@app.on_event("startup")
def startup():
    init_db()
    if os.getenv("RUN_SCHEDULER", "1") == "1":
        scheduler.add_job(run_check, "interval", seconds=INTERVAL_SECONDS, id="resale-check", max_instances=1, coalesce=True)
        scheduler.start()


@app.on_event("shutdown")
def shutdown():
    if scheduler.running:
        scheduler.shutdown(wait=False)


@app.get("/")
def home():
    return FileResponse(ROOT / "templates" / "index.html")


@app.get("/api/products")
def products():
    with closing(connect()) as db:
        rows = db.execute("SELECT * FROM products ORDER BY first_seen DESC, rowid DESC").fetchall()
    return [dict(row) for row in rows]


@app.get("/api/status")
def status():
    with closing(connect()) as db:
        count = db.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    return {"saved_products": count, "interval_seconds": INTERVAL_SECONDS, "scheduler_running": scheduler.running}


@app.post("/api/check")
def manual_check():
    try:
        return run_check()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
