# Amazon Resale Radar

A private starter web app for monitoring Amazon.eg Resale and listing discovered products newest-first.

## Run locally (Windows)

1. Install Python 3.11 or newer.
2. Open Command Prompt in this folder.
3. Run:

```bat
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
uvicorn app:app --host 127.0.0.1 --port 8000
```

4. Open http://127.0.0.1:8000

The first automatic scan runs after startup and repeats every 30 seconds. Use **تحديث الآن** for a manual scan.

## Test the parser against the supplied captured page

```bat
python test_parser.py "PATH_TO_CAPTURED_HTML.txt"
```

## Important

- The parser was built against the Amazon page HTML captured during setup. Amazon can change this structure.
- This project makes ordinary HTTP requests; it does not bypass CAPTCHA, login, access controls, or other restrictions. If Amazon returns a robot-check page, the scan reports an error and does not treat it as an empty product list.
- A 30-second interval is a target, not a guarantee. Amazon may rate-limit requests or return incomplete/different content.
- The app is not deployed or password-protected yet. Do not expose it publicly until authentication and HTTPS are configured.
- SQLite keeps the first-discovered timestamp and updates current product details without changing the original discovery order.
