"""
Dynamic "News of the Day" server.

Why this exists:
- The generated dist/index.html is a *static snapshot*. Reloading it in a browser
  does NOT fetch new headlines; it only reloads the same file.
- This server makes the page dynamic: the browser loads a page from http://localhost
  and the page fetches fresh headlines from /api/news whenever it is loaded
  (and optionally on an interval).

How to run:
  python -m pip install -r requirements.txt
  python server.py

Then open:
  http://127.0.0.1:8000/
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, send_from_directory

# We reuse the scraping/selection logic already implemented in build.py.
import build


APP_HOST = "127.0.0.1"
APP_PORT = 8000

PROJECT_DIR = Path(__file__).resolve().parent
DIST_DIR = PROJECT_DIR / "dist"

app = Flask(__name__)


@app.get("/")
def index() -> Any:
    """
    Serve the existing UI shell from dist/index.html.

    Important:
    - The JS below expects /api/news to exist on the same origin.
    - If you prefer, you can still open dist/index.html directly, but then it will
      be a static snapshot again.
    """

    return send_from_directory(DIST_DIR, "index.html")


@app.get("/static/<path:filename>")
def static_files(filename: str) -> Any:
    """Serve static assets used by the page (icons/logos/images)."""

    return send_from_directory(DIST_DIR / "static", filename)


@app.get("/api/news")
def api_news() -> Any:
    """
    Return the latest news payload as JSON.

    The client-side page can render this payload and refresh it on reload without
    re-running build.py.

    Notes:
    - This returns remote image URLs (image_url) rather than downloading images.
      That keeps the server lightweight and fast.
    - Some sources are text-only (per user preference); the client should hide
      images for those sources.
    """

    headlines = build._collect_headlines()
    headlines = build._enrich_missing_media(headlines)

    payload = {
        "built_at": datetime.now().isoformat(timespec="seconds"),
        "sources": [s.source for s in build._feed_specs()],
        "text_only_sources": sorted(list(build._TEXT_ONLY_SOURCES)),
        "items": [
            {
                "source": h.source,
                "title": h.title,
                "summary": h.summary,
                "link": h.link,
                "image_url": h.image_url,
            }
            for h in headlines
        ],
    }
    return jsonify(payload)


if __name__ == "__main__":
    # Ensure dist/ exists; if it doesn't, the user should run build.py once to
    # create the initial HTML shell and static assets.
    DIST_DIR.mkdir(parents=True, exist_ok=True)
    app.run(host=APP_HOST, port=APP_PORT, debug=False)

