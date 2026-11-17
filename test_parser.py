import json, sys
from pathlib import Path
from scraper import parse_resale_html

if len(sys.argv) != 2:
    raise SystemExit('Usage: python test_parser.py captured_page.html')
html = Path(sys.argv[1]).read_text(encoding='utf-8', errors='replace')
products = parse_resale_html(html)
print(json.dumps(products, ensure_ascii=False, indent=2))
print(f"\nParsed products: {len(products)}")
