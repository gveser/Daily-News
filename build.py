"""
News of the Day - static homepage builder

What this script does (high level):
1) Download RSS/Atom feeds for several news sites.
2) Extract headline + link + (optional) image URL for each item.
3) Download/cache images locally (when an image URL is available).
4) Generate a single static HTML page in dist/index.html with a 4x4 grid.

This keeps things simple:
- No web server needed. Just open dist/index.html in your browser.
- Feeds are fetched on each full build; ``python build.py --use-cache`` rebuilds HTML from
  ``cache/build-cache.json`` (written after a successful full build) to skip network-heavy steps.
- Article images are still stored under ``dist/``; the build cache only stores metadata/paths.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import shutil
import sys
import textwrap
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import quote, urlparse

import feedparser
import requests
from bs4 import BeautifulSoup


@dataclass(frozen=True)
class Headline:
    """A single headline card to render on the page."""

    source: str
    title: str
    link: str
    # A short 1–2 sentence excerpt shown under the headline.
    summary: Optional[str] = None
    image_url: Optional[str] = None
    # image_path is the *local* path (relative to dist/) when downloaded.
    image_path: Optional[str] = None


@dataclass(frozen=True)
class FeedSpec:
    """
    One news source and one-or-more feed URLs to try.

    Why multiple URLs?
    - Some publishers change RSS endpoints or temporarily block one endpoint.
    - Trying a small fallback list makes the script more robust.
    """

    source: str
    homepage_url: str
    urls: list[str]
    # Some publishers provide better "headline news" via HTML pages than RSS.
    # If rss_urls fail (or don't include images), we can optionally scrape.
    use_homepage_scrape: bool = False
    # When scraping a homepage, only keep links whose URL matches this regex.
    # This helps avoid navigation links, tag pages, etc.
    homepage_link_allow_regex: Optional[str] = None


# Sources the user wants to render as text-only (no images shown).
_TEXT_ONLY_SOURCES = {
    # (Empty for now) — user prefers images whenever possible.
}

# Region filter categories for the top buttons.
# These labels match what we display in the UI.
_REGION_BY_SOURCE: dict[str, str] = {
    # UK
    "The Economist": "UK",
    "The Guardian": "UK",
    "BBC News": "UK",
    "Financial Times": "UK",
    "Reuters": "UK",
    # Germany
    "Der Spiegel": "DE",
    "Deutsche Welle": "DE",
    "Süddeutsche Zeitung": "DE",
    "Tagesschau": "DE",
    "RNZ": "DE",
    "NZZ": "EU",
    # Europe (EU)
    "Le Monde": "EU",
    "El País": "EU",
    "France 24": "EU",
    "EUobserver": "EU",
    "Agence France-Presse": "EU",
    # United States
    "Associated Press": "US",
    "The New York Times": "US",
    "The Washington Post": "US",
    "The Wall Street Journal": "US",
    "USA Today": "US",
    "Vox": "US",
    "Politico": "US",
    "Axios": "US",
    "The Atlantic": "US",
    "Fox News": "US",
    # Pittsburgh (PGH)
    "WESA": "PGH",
    "NEXTpittsburgh": "PGH",
    "Pgh City Paper": "PGH",
    "Pgh PublicSource": "PGH",
    "Trib|Live": "PGH",
    # International
    "Times of India": "Int'l",
    "The Straits Times": "Int'l",
    "South China Morning Post": "Int'l",
    "The Jerusalem Post": "Int'l",
    "AllAfrica": "Int'l",
    "Al Jazeera": "Int'l",
    "BBC World Service": "Int'l",
    "The Diplomat": "Int'l",
}


@dataclass(frozen=True)
class TopTopic:
    """
    One "top news" topic cluster across many sources.

    - title: short label shown above the cluster panel
    - items: the individual headlines that were grouped into this topic
    """

    title: str
    items: list["Headline"]


def _tokenize_for_topic(text: str) -> list[str]:
    """
    Turn headline text into a list of normalized tokens for rough topic grouping.

    We intentionally keep this lightweight (no external NLP dependencies):
    - lower-case
    - keep only a–z/0–9 words
    - remove common stopwords
    - drop very short tokens
    """

    # Stopwords:
    # We filter common "filler" words so they don't dominate Top News keywords.
    # This list includes:
    # - English fillers (the, is, with, common verbs like get/say, media words like video, ...)
    # - Common German article/preposition fillers (der, die, das, auf, ...)
    # - Common French article/preposition fillers (le, la, les, de, des, ...)
    # - Common Spanish function words (los, las, que, para, ...) for multilingual headlines
    #
    # This is intentionally a small, pragmatic list (not a full NLP stopword corpus).
    stop = {
        "the",
        "a",
        "an",
        "and",
        "or",
        "but",
        "to",
        "of",
        "in",
        "on",
        "for",
        "with",
        "at",
        "by",
        "from",
        "as",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "has",
        "have",
        "had",
        "will",
        "would",
        "could",
        "should",
        "after",
        "before",
        "over",
        "under",
        "into",
        "about",
        "it",
        "its",
        "their",
        "they",
        "you",
        "your",
        "we",
        "our",
        "us",
        "he",
        "she",
        "his",
        "her",
        "this",
        "that",
        "these",
        "those",
        "says",
        "say",
        "said",
        "news",
        "live",
        "update",
        "updates",
        "how",
        "still",
        # More English fillers (headlines / function words that rarely identify a topic)
        "all",
        "not",
        "no",
        "nor",
        "so",
        "if",
        "than",
        "then",
        "too",
        "also",
        "only",
        "even",
        "very",
        "any",
        "own",
        "same",
        "few",
        "off",
        "per",
        "via",
        "now",
        "new",
        "day",
        "way",
        "week",
        "year",
        "time",
        "times",
        "many",
        "much",
        "each",
        "both",
        "such",
        "most",
        "more",
        "some",
        "out",
        "up",
        "down",
        "amid",
        "among",
        "against",
        "despite",
        "following",
        "within",
        "without",
        "through",
        "during",
        "until",
        "since",
        "once",
        "again",
        "back",
        "away",
        "well",
        "really",
        "yet",
        "already",
        "ever",
        "while",
        "across",
        "around",
        "along",
        "today",
        "tonight",
        "what",
        "when",
        "why",
        "who",
        "whom",
        "whose",
        "which",
        "get",
        "got",
        "gets",
        "go",
        "goes",
        "going",
        "went",
        "gone",
        "come",
        "came",
        "comes",
        "make",
        "made",
        "makes",
        "take",
        "took",
        "takes",
        "see",
        "saw",
        "know",
        "knew",
        "think",
        "thinks",
        "thought",
        "tell",
        "told",
        "tells",
        "give",
        "gave",
        "find",
        "found",
        "look",
        "looks",
        "show",
        "shows",
        "seem",
        "seems",
        "want",
        "wants",
        "need",
        "needs",
        "like",
        "video",
        "watch",
        "photo",
        "photos",
        "image",
        "images",
        "read",
        "full",
        "story",
        "stories",
        "latest",
        "top",
        "breaking",
        "report",
        "reports",
        "according",

        # German
        "der",
        "die",
        "das",
        "den",
        "dem",
        "des",
        "ein",
        "eine",
        "einer",
        "eines",
        "einem",
        "einen",
        "und",
        "oder",
        "aber",
        "auf",
        "im",
        "in",
        "am",
        "an",
        "aus",
        "bei",
        "mit",
        "von",
        "vom",
        "zum",
        "zur",
        "über",
        "unter",
        "nach",
        "vor",
        "für",
        "ist",
        "sind",
        "war",
        "waren",

        # French
        "le",
        "la",
        "les",
        "un",
        "une",
        "des",
        "du",
        "de",
        "d",
        "et",
        "ou",
        "mais",
        "en",
        "dans",
        "sur",
        "avec",
        "par",
        "pour",
        "sans",
        "aux",
        "au",
        "est",
        "sont",

        # Spanish (e.g. El País / Latin American headlines tokenized a–z)
        "los",
        "las",
        "por",
        "para",
        "como",
        "que",
        "del",
        "al",
        "ya",
        "muy",
        "mas",
        "pero",
        "todo",
        "todos",
        "esta",
        "este",
        "estos",
        "estas",
        "hay",
        "son",
        "ser",
        "fue",
        "han",
        "haber",
    }

    raw = re.findall(r"[a-z0-9]+", (text or "").lower())
    out: list[str] = []
    for w in raw:
        if len(w) < 3:
            continue
        if w in stop:
            continue
        out.append(w)
    return out


def _jaccard(a: set[str], b: set[str]) -> float:
    """Simple similarity score between token sets."""

    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def _compute_top_topics(items: list["Headline"], k: int = 2) -> list[TopTopic]:
    """
    Find the top-k widely-reported topics across all sources.

    "Widely reported" is approximated as: clusters that are reported by the
    largest number of distinct sources.

    This is heuristic and intentionally simple: it clusters headlines by token
    overlap (Jaccard similarity). It's not perfect, but works surprisingly well
    for "big stories" that many outlets cover on the same day.
    """

    # Keep a larger pool (not only the first item) so we can capture more overlap.
    pool: list[Headline] = []
    for h in items:
        if not h.title:
            continue
        pool.append(h)

    # Greedy clustering in original order (which is already grouped by source).
    #
    # Important nuance (user request):
    # Some sources are effectively the same outlet under different labels (e.g. BBC News
    # vs BBC World Service). For Top News we treat those as *one* source so a story that
    # appears on both does not get double credit for "number of outlets covering it".
    def canonical_topic_source(source: str) -> str:
        if source in ("BBC News", "BBC World Service"):
            return "BBC"
        return source

    clusters: list[dict] = []
    for h in pool:
        toks = set(_tokenize_for_topic(h.title))
        if len(toks) < 2:
            continue

        best_idx = None
        best_sim = 0.0
        for i, c in enumerate(clusters):
            sim = _jaccard(toks, c["centroid"])
            if sim > best_sim:
                best_sim = sim
                best_idx = i

        # Only merge when the overlap is meaningful.
        if best_idx is not None and best_sim >= 0.35 and len(toks & clusters[best_idx]["centroid"]) >= 2:
            clusters[best_idx]["items"].append(h)
            clusters[best_idx]["centroid"] |= toks
            clusters[best_idx]["sources"].add(canonical_topic_source(h.source))
        else:
            clusters.append(
                {"items": [h], "centroid": set(toks), "sources": {canonical_topic_source(h.source)}}
            )

    # Score clusters by number of distinct sources (then by item count).
    clusters.sort(key=lambda c: (len(c["sources"]), len(c["items"])), reverse=True)
    # We'll select the top-k topics *after* expansion across sources, because
    # expansion changes which tokens dominate a topic and helps avoid duplicates.

    def short_title_for(cluster_items: list[Headline]) -> str:
        # Use the most common tokens as a compact label.
        counts: dict[str, int] = {}
        for it in cluster_items:
            for t in set(_tokenize_for_topic(it.title)):
                counts[t] = counts.get(t, 0) + 1
        common = sorted(counts.items(), key=lambda x: (-x[1], x[0]))[:3]
        if common:
            # Only force ALL CAPS for short tokens that are likely acronyms.
            # (User preference: cap <=3, not <=4.)
            return " • ".join(w.upper() if len(w) <= 3 else w.title() for w, _ in common)
        # Fallback: shorten the first title.
        first = cluster_items[0].title.strip()
        return (first[:57] + "…") if len(first) > 58 else first

    def keywords_for(cluster_items: list[Headline]) -> list[str]:
        """
        Return the three topic keywords used in the Top News label.

        We keep this separate so we can de-duplicate topics using the same
        keyword logic the user sees in the UI.
        """

        counts: dict[str, int] = {}
        for it in cluster_items:
            for t in set(_tokenize_for_topic(it.title)):
                counts[t] = counts.get(t, 0) + 1
        common = sorted(counts.items(), key=lambda x: (-x[1], x[0]))[:3]
        return [w for w, _ in common]

    def expand_cluster_across_sources(cluster: dict) -> list[Headline]:
        """
        After we have a topic cluster, make sure we include as many sources as possible.

        Some outlets use different wording (e.g. "former FBI director" vs a surname),
        which can cause them to miss the initial clustering threshold. Here we:
        - derive a small set of "key tokens" for the topic
        - for each source, pick the best-matching headline among that source's items
        """

        # Key tokens = the most frequent tokens in the cluster.
        counts: dict[str, int] = {}
        for it in cluster["items"]:
            for t in set(_tokenize_for_topic(it.title)):
                counts[t] = counts.get(t, 0) + 1
        key_tokens = [w for w, _ in sorted(counts.items(), key=lambda x: (-x[1], x[0]))[:6]]
        key_set = set(key_tokens)

        # Group pool by *canonical* source so we can scan each outlet's headlines.
        # This merges BBC News + BBC World Service into one "BBC" pool.
        pool_by_canon: dict[str, list[Headline]] = {}
        for h in pool:
            pool_by_canon.setdefault(canonical_topic_source(h.source), []).append(h)

        def score(it: Headline) -> int:
            toks = set(_tokenize_for_topic(it.title))
            # Weight: direct key token hits matter most.
            hits = len(toks & key_set)
            overlap = len(toks & cluster["centroid"])
            return hits * 3 + overlap

        picked: dict[str, Headline] = {}
        # Start with items already in the cluster (but de-dupe by canonical outlet).
        for it in cluster["items"]:
            picked.setdefault(canonical_topic_source(it.source), it)

        # Expand: pick best item per missing outlet if it matches meaningfully.
        # Iterate in canonical order derived from _feed_specs().
        canon_order: list[str] = []
        for spec in _feed_specs():
            csrc = canonical_topic_source(spec.source)
            if csrc not in canon_order:
                canon_order.append(csrc)

        for csrc in canon_order:
            if csrc in picked:
                continue
            candidates = pool_by_canon.get(csrc) or []
            if not candidates:
                continue
            best = max(candidates, key=score)
            # Require at least one key token hit OR two centroid overlaps.
            toks = set(_tokenize_for_topic(best.title))
            hits = len(toks & key_set)
            cent = len(toks & cluster["centroid"])
            # Stricter matching to avoid unrelated headlines sneaking in:
            # - Either 2+ key token hits (strong)
            # - Or a decent centroid similarity with at least 1 key token
            # - Or 3+ centroid overlaps (fallback)
            if (hits >= 2) or (_jaccard(toks, cluster["centroid"]) >= 0.22 and hits >= 1) or (cent >= 3):
                picked[src] = best

        # Return in canonical order.
        return [picked[csrc] for csrc in canon_order if csrc in picked]

    def signature_for_items(cluster_items: list[Headline]) -> frozenset[str]:
        counts: dict[str, int] = {}
        for it in cluster_items:
            for t in set(_tokenize_for_topic(it.title)):
                counts[t] = counts.get(t, 0) + 1
        top_tokens = [w for w, _ in sorted(counts.items(), key=lambda x: (-x[1], x[0]))[:5]]
        return frozenset(top_tokens)

    chosen: list[TopTopic] = []
    chosen_sigs: set[frozenset[str]] = set()
    chosen_token_sets: list[set[str]] = []
    chosen_keywords: list[set[str]] = []

    for c in clusters:
        if len(chosen) >= max(0, k):
            break

        ordered = expand_cluster_across_sources(c)
        if len({canonical_topic_source(h.source) for h in ordered}) < 3:
            # Not widely reported enough; skip.
            continue

        sig = signature_for_items(ordered)
        if sig and sig in chosen_sigs:
            continue

        tok_set = set(sig)
        too_close = any(_jaccard(tok_set, s) >= 0.45 for s in chosen_token_sets if s)
        if too_close:
            continue

        # User requested: if 2 of the 3 keywords are identical, group the topics into one.
        # We enforce this by skipping any candidate topic whose visible keywords overlap an
        # already-chosen topic by >=2.
        kw_set = set(keywords_for(ordered))
        if len(kw_set) >= 2 and any(len(kw_set & prev) >= 2 for prev in chosen_keywords):
            continue

        chosen.append(TopTopic(title=short_title_for(ordered), items=ordered))
        if sig:
            chosen_sigs.add(sig)
            chosen_token_sets.append(set(sig))
            chosen_keywords.append(kw_set)

    # Ordering (user requested):
    # 1) Sort topics by number of related sources (descending).
    # 2) If topics share at least one of their visible keywords, keep them adjacent.
    #
    # We treat "share one topic" as: their 3-keyword sets overlap by >= 1 keyword.
    # We build connected components on that overlap graph, then order components by
    # their strongest topic (most sources), and order topics within each component
    # by source count.
    if len(chosen) >= 2:
        source_counts = [len({canonical_topic_source(h.source) for h in t.items}) for t in chosen]

        parent = list(range(len(chosen)))

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: int, b: int) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra

        for i in range(len(chosen)):
            for j in range(i + 1, len(chosen)):
                if chosen_keywords[i] and chosen_keywords[j] and (chosen_keywords[i] & chosen_keywords[j]):
                    union(i, j)

        comps: dict[int, list[int]] = {}
        for idx in range(len(chosen)):
            comps.setdefault(find(idx), []).append(idx)

        # Sort components and topics inside them.
        ordered_indices: list[int] = []
        for _, members in sorted(
            comps.items(),
            key=lambda kv: max(source_counts[i] for i in kv[1]),
            reverse=True,
        ):
            members_sorted = sorted(members, key=lambda i: source_counts[i], reverse=True)
            ordered_indices.extend(members_sorted)

        chosen = [chosen[i] for i in ordered_indices]

    return chosen


@dataclass
class _MetaCache:
    """
    A tiny on-disk cache for article metadata (og:image and description).

    Why this exists:
    - Without caching, we re-download the same article pages on every build.
    - Many sources require 6+ article fetches per source, which gets slow fast.

    Cache format (JSON):
    {
      "url": {"ts": 1714170000, "image_url": "...", "description": "..."},
      ...
    }

    We use a TTL so the cache stays reasonably fresh.
    """

    path: Path
    ttl_s: int = 60 * 60 * 6  # 6 hours
    # Failed metadata lookups are cached as `{image_url: null, description: null}`.
    # Keeping those "negative" entries for hours can hide fixes when a publisher briefly
    # rate-limits automated requests, so we expire negatives faster than successes.
    # Keep negatives very short: many sites intermittently rate-limit or temporarily
    # omit metadata, and a long-lived negative cache makes the page look "broken"
    # for a long time even after the next successful fetch.
    negative_ttl_s: int = 60 * 2  # 2 minutes
    _data: dict[str, dict] = None  # set in load()

    def load(self) -> None:
        self._data = {}
        try:
            if self.path.exists():
                self._data = json.loads(self.path.read_text(encoding="utf-8")) or {}
        except Exception:
            # If the cache is corrupt, ignore it and start fresh.
            self._data = {}

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            # Non-fatal: the build output is more important than caching.
            pass

    def get(self, url: str) -> Optional[tuple[Optional[str], Optional[str]]]:
        if not self._data:
            return None
        rec = self._data.get(url)
        if not rec:
            return None
        ts = rec.get("ts")
        if not isinstance(ts, (int, float)):
            return None
        age = time.time() - float(ts)
        neg = (rec.get("image_url") is None) and (rec.get("description") is None)
        ttl = self.negative_ttl_s if neg else self.ttl_s
        if age > ttl:
            return None
        return (rec.get("image_url") or None, rec.get("description") or None)

    def set(self, url: str, image_url: Optional[str], description: Optional[str]) -> None:
        if self._data is None:
            self._data = {}
        self._data[url] = {
            "ts": int(time.time()),
            "image_url": image_url,
            "description": description,
        }


def _new_http_session() -> requests.Session:
    """
    Create a single requests Session.

    Sessions reuse TCP connections which reduces latency substantially when we're
    making lots of requests (homepages + many article metadata fetches).
    """

    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0 Safari/537.36"
            )
        }
    )
    return s


def _browser_like_headers() -> dict[str, str]:
    """
    Headers that look like a normal desktop browser.

    Why we need this:
    - Many publishers block non-browser user agents for RSS/HTML.
    - Google News wrapper pages embed signing parameters we need to decode real URLs.
    """

    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://news.google.com/",
    }


def _google_news_token_from_url(url: str) -> Optional[str]:
    """
    Extract the opaque Google News token from a wrapper URL.

    Typical shapes:
    - https://news.google.com/rss/articles/<token>?...
    - https://news.google.com/articles/<token>?...
    """

    try:
        host = (urlparse(url).hostname or "").lower()
        if host not in {"news.google.com", "www.news.google.com"}:
            return None
        parts = [p for p in urlparse(url).path.split("/") if p]
        if len(parts) < 2:
            return None
        if parts[-2] not in {"articles", "rss"}:
            # Still handle .../articles/<token> even if middle segment differs.
            pass
        return parts[-1] or None
    except Exception:
        return None


def _decode_google_news_publishers_batch(
    session: requests.Session,
    articles: list[dict[str, str]],
    timeout_s: int = 30,
) -> dict[str, str]:
    """
    Turn Google News wrapper tokens into publisher URLs.

    This uses Google's internal `batchexecute` endpoint with signing parameters that
    are embedded in the HTML returned for each `/rss/articles/<token>` page.

    Returns:
      token -> publisher_url
    """

    if not articles:
        return {}

    articles_reqs: list[list[str]] = []
    for art in articles:
        articles_reqs.append(
            [
                "Fbv4je",
                (
                    '["garturlreq",[["X","X",["X","X"],null,null,1,1,"US:en",null,1,null,null,null,null,null,0,1],'
                    '"X","X",1,[1,1,1],1,1,null,0,0,null,0],"'
                    f'{art["gn_art_id"]}'
                    f'",{art["timestamp"]},"'
                    f'{art["signature"]}'
                    '"]'
                ),
            ]
        )

    payload = "f.req=" + quote(json.dumps([articles_reqs]))
    r = session.post(
        "https://news.google.com/_/DotsSplashUi/data/batchexecute",
        headers={
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
            **_browser_like_headers(),
        },
        data=payload,
        timeout=timeout_s,
    )
    r.raise_for_status()

    parts = r.text.split("\n\n")
    if len(parts) < 2:
        return {}

    middle = json.loads(parts[1])
    publisher_urls: list[str] = []
    if isinstance(middle, list):
        for row in middle:
            # Typical successful rows look like:
            # ["wrb.fr","Fbv4je","[\"garturlres\",\"https://publisher/article\",1]", ...]
            if not isinstance(row, list) or len(row) < 3:
                continue
            if row[0] != "wrb.fr":
                continue
            payload = row[2]
            if not isinstance(payload, str):
                continue
            try:
                inner = json.loads(payload)
            except Exception:
                continue
            if not isinstance(inner, list) or len(inner) < 2:
                continue
            if inner[0] != "garturlres":
                continue
            publisher_url = inner[1]
            if isinstance(publisher_url, str) and publisher_url.startswith("http"):
                publisher_urls.append(publisher_url)

    # IMPORTANT: If Google returns fewer decoded URLs than we requested (e.g. due to
    # rate limiting), blindly zipping would shift mappings and attach the wrong link
    # to the wrong headline (which shows up as "wrong photo in the wrong box").
    if len(publisher_urls) != len(articles):
        return {}

    out: dict[str, str] = {}
    for art, publisher_url in zip(articles, publisher_urls):
        out[str(art["gn_art_id"])] = publisher_url
    return out


def _homepage_registered_domain(source: str) -> Optional[str]:
    """
    Return a coarse registered-domain string for a source's configured homepage.

    Example:
    - https://www.reuters.com/ -> "reuters.com"
    - https://nextpittsburgh.com/ -> "nextpittsburgh.com"

    We use this as a lightweight sanity check after decoding Google News wrapper URLs.
    """

    home = ""
    for spec in _feed_specs():
        if spec.source == source:
            home = spec.homepage_url
            break

    if not home:
        return None

    host = (urlparse(home).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host or None


def _url_matches_source_homepage(source: str, url: str) -> bool:
    """
    Return True if `url` looks like it belongs to the publisher implied by our FeedSpec homepage.

    This is intentionally conservative: we only *reject* obvious mismatches when Google News
    decoding lands on a different publisher domain than the feed constraint suggests.
    """

    allowed = _homepage_registered_domain(source)
    if not allowed:
        return True

    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False

    if host.startswith("www."):
        host = host[4:]

    return host == allowed or host.endswith("." + allowed)


def _rewrite_google_news_links(session: requests.Session, headlines: list[Headline]) -> list[Headline]:
    """
    Replace `news.google.com/rss/articles/...` links with direct publisher URLs.

    Without this, og:image enrichment often only sees generic Google-branded images.
    """

    # Collect unique tokens once (many headlines may share the same token).
    seen_tokens: set[str] = set()
    articles_meta: list[dict[str, str]] = []

    for h in headlines:
        if "news.google.com/rss/articles/" not in h.link:
            continue
        token = _google_news_token_from_url(h.link)
        if not token:
            continue
        if token in seen_tokens:
            continue

        seen_tokens.add(token)
        try:
            # Google can rate-limit bursts of wrapper fetches. Use a small retry loop.
            r = None
            for attempt in range(4):
                r = session.get(h.link, headers=_browser_like_headers(), timeout=25, allow_redirects=True)
                if r.status_code == 429:
                    time.sleep(0.4 * (attempt + 1))
                    continue
                r.raise_for_status()
                break
            if r is None:
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            div = soup.select_one("c-wiz > div")
            if div is None:
                continue
            sig = div.get("data-n-a-sg")
            ts = div.get("data-n-a-ts")
            if not sig or not ts:
                continue
            articles_meta.append(
                {"gn_art_id": token, "signature": str(sig), "timestamp": str(ts)}
            )
        except Exception:
            continue

        # Small pacing helps avoid Google rate limits when decoding many items.
        time.sleep(0.25)

    decoded_by_token: dict[str, str] = {}
    # Decode one-by-one. It's slower than batching, but avoids occasional ordering
    # mismatches that can attach the wrong publisher URL to the wrong headline.
    for art in articles_meta:
        try:
            one = _decode_google_news_publishers_batch(session, [art])
            decoded_by_token.update(one)
        except Exception:
            continue

    # Apply replacements.
    updated: list[Headline] = []
    for h in headlines:
        if "news.google.com/rss/articles/" not in h.link:
            updated.append(h)
            continue
        token = _google_news_token_from_url(h.link)
        pub = decoded_by_token.get(token or "") if token else None
        if pub and _url_matches_source_homepage(h.source, pub):
            updated.append(Headline(**{**h.__dict__, "link": pub}))
        else:
            updated.append(h)

    return updated


def _feed_specs() -> list[FeedSpec]:
    """
    All news sources we show, in the exact order we render them.

    IMPORTANT: The user asked for Deutsche Welle directly below Der Spiegel.
    We encode that ordering here so both scraping/rendering stay consistent.
    """

    return [
        # Existing sources
        FeedSpec(
            source="The Guardian",
            homepage_url="https://www.theguardian.com/",
            urls=["https://www.theguardian.com/world/rss"],
            use_homepage_scrape=True,
            homepage_link_allow_regex=r"https://www\.theguardian\.com/.+/\d{4}/[a-z]{3}/\d{1,2}/",
        ),
        FeedSpec(
            source="The Economist",
            homepage_url="https://www.economist.com/",
            # The Economist blocks our homepage scraping (403) frequently.
            # A constrained Google News RSS feed is more reliable in automated builds,
            # and we replace Google placeholder images during enrichment.
            urls=["https://news.google.com/rss/search?q=site%3Aeconomist.com&hl=en-US&gl=US&ceid=US%3Aen"],
            use_homepage_scrape=False,
        ),
        FeedSpec(
            source="Der Spiegel",
            homepage_url="https://www.spiegel.de/",
            urls=["https://www.spiegel.de/schlagzeilen/index.rss"],
            use_homepage_scrape=True,
            homepage_link_allow_regex=r"https://www\.spiegel\.de/.+-a-[0-9a-f-]{6,}",
        ),
        # Germany: Tagesschau should be #2
        FeedSpec(
            source="Tagesschau",
            homepage_url="https://www.tagesschau.de/",
            urls=["https://www.tagesschau.de/index~rss2.xml"],
            use_homepage_scrape=False,
        ),
        # Deutsche Welle (kept near top German sources)
        FeedSpec(
            source="Deutsche Welle",
            homepage_url="https://www.dw.com/",
            urls=[
                # DW provides multiple RSS feeds; this one is a broad "Top Stories" style feed.
                "https://rss.dw.com/rdf/rss-en-top",
                "https://rss.dw.com/rdf/rss-en-all",
            ],
            use_homepage_scrape=True,
            homepage_link_allow_regex=r"https://www\.dw\.com/.+/a-\d+",
        ),
        FeedSpec(
            source="Süddeutsche Zeitung",
            homepage_url="https://www.sueddeutsche.de/",
            urls=["https://rss.sueddeutsche.de/rss/Topthemen"],
            use_homepage_scrape=False,
        ),
        FeedSpec(
            source="RNZ",
            homepage_url="https://www.rnz.de/",
            urls=["http://www.rnz.de/feed/136-RL_Topthemen_free.xml"],
            use_homepage_scrape=False,
        ),
        FeedSpec(
            source="BBC World Service",
            homepage_url="https://www.bbc.com/worldservice",
            # BBC World Service RSS hub provides many language feeds, but not a
            # dedicated "english.html". We use BBC World News RSS as an English proxy.
            urls=["https://feeds.bbci.co.uk/news/world/rss.xml"],
            use_homepage_scrape=False,
        ),
        # Int'l: Al Jazeera (moved up to 2nd position in Int'l tab)
        FeedSpec(
            source="Al Jazeera",
            homepage_url="https://www.aljazeera.com/",
            urls=["https://www.aljazeera.com/xml/rss/all.xml"],
            use_homepage_scrape=False,
        ),
        FeedSpec(
            source="The Diplomat",
            homepage_url="https://thediplomat.com/",
            urls=["https://thediplomat.com/feed/"],
            use_homepage_scrape=False,
        ),
        FeedSpec(
            source="The Straits Times",
            homepage_url="https://www.straitstimes.com/",
            urls=["https://www.straitstimes.com/news/world/rss.xml"],
            use_homepage_scrape=True,
            homepage_link_allow_regex=r"https://www\.straitstimes\.com/(singapore|world|asia|business|opinion|sport|life|multimedia|tech|environment|money|invest)/.+",
        ),
        # Int'l: Times of India (immediately after Straits Times in the Int'l tab)
        FeedSpec(
            source="Times of India",
            homepage_url="https://timesofindia.indiatimes.com/",
            urls=["https://timesofindia.indiatimes.com/rssfeedstopstories.cms"],
            use_homepage_scrape=False,
        ),

        # Added sources
        #
        # US tab (`data-region="US"`) — US-only row order in DOM / `_feed_specs()` order:
        # 1) Axios is the first US source (first US row).
        # 2) The last three US rows are exactly: Fox News, then The Wall Street Journal,
        #    then The Atlantic (Atlantic is last among US sources; no US rows follow).
        FeedSpec(
            source="Axios",
            homepage_url="https://www.axios.com/",
            # Axios serves this feed from api.axios.com (via redirect), which is fine.
            urls=["https://www.axios.com/feeds/feed.rss"],
            use_homepage_scrape=False,
        ),
        FeedSpec(
            source="Associated Press",
            homepage_url="https://apnews.com/",
            urls=[],
            use_homepage_scrape=True,
            homepage_link_allow_regex=r"https://apnews\.com/article/",
        ),
        FeedSpec(
            source="BBC News",
            homepage_url="https://www.bbc.com/news",
            urls=["https://feeds.bbci.co.uk/news/rss.xml"],
            use_homepage_scrape=False,
        ),
        FeedSpec(
            source="Financial Times",
            homepage_url="https://www.ft.com/",
            urls=["https://www.ft.com/rss/home"],
            use_homepage_scrape=False,
        ),
        # UK: Reuters
        #
        # Note (May 2026): `www.reuters.com` now returns HTTP 401 to many automated clients,
        # which makes og:image extraction and even homepage scraping impossible here.
        # The "Reuters Best" RSS feed (WordPress) is accessible and still provides Reuters
        # stories with usable preview images, so we use it instead of reuters.com.
        FeedSpec(
            source="Reuters",
            homepage_url="https://reutersbest.com/",
            urls=["https://reutersbest.com/feed/"],
            use_homepage_scrape=False,
        ),
        FeedSpec(
            source="Vox",
            homepage_url="https://www.vox.com/",
            urls=["https://www.vox.com/rss/index.xml"],
            use_homepage_scrape=False,
        ),
        # US: Politico (keep near other US digital-native sources)
        FeedSpec(
            source="Politico",
            homepage_url="https://www.politico.com/",
            # `rss.politico.com` endpoints are more automation-friendly than some `www.politico.com/rss/*` URLs.
            urls=["https://rss.politico.com/politics-news.xml"],
            use_homepage_scrape=False,
        ),
        FeedSpec(
            source="South China Morning Post",
            homepage_url="https://www.scmp.com/",
            urls=["https://www.scmp.com/rss"],
            use_homepage_scrape=True,
            # SCMP's homepage contains many persistent "section" links (e.g. People & Culture)
            # that are not actual story pages. Their story URLs reliably include `/article/<id>`,
            # so we constrain scraping to those to avoid pulling static nav headlines.
            homepage_link_allow_regex=r"https://www\.scmp\.com/.+/article/\d+",
        ),
        FeedSpec(
            source="The Jerusalem Post",
            homepage_url="https://www.jpost.com/",
            urls=["https://www.jpost.com/rss/rssfeedsfrontpage.aspx"],
            use_homepage_scrape=False,
        ),
        FeedSpec(
            source="The New York Times",
            homepage_url="https://www.nytimes.com/",
            urls=["https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml"],
            use_homepage_scrape=False,
        ),
        FeedSpec(
            source="The Washington Post",
            homepage_url="https://www.washingtonpost.com/",
            urls=["https://feeds.washingtonpost.com/rss/world"],
            use_homepage_scrape=True,
            homepage_link_allow_regex=r"https://www\.washingtonpost\.com/.+/[0-9]{4}/[0-9]{2}/[0-9]{2}/",
        ),
        FeedSpec(
            source="USA Today",
            homepage_url="https://www.usatoday.com/",
            # USA Today's historical rssfeeds.usatoday.com endpoints often redirect to HTML now.
            # To keep this source reliable without brittle scraping, we use a Google News RSS
            # query constrained to usatoday.com.
            urls=["https://news.google.com/rss/search?q=site%3Ausatoday.com&hl=en-US&gl=US&ceid=US%3Aen"],
            use_homepage_scrape=False,
        ),
        # US: Fox News (official Google Publisher + legacy feeds path as fallback)
        FeedSpec(
            source="Fox News",
            homepage_url="https://www.foxnews.com/",
            urls=[
                "https://moxie.foxnews.com/google-publisher/latest.xml",
                "https://feeds.foxnews.com/foxnews/latest",
            ],
            use_homepage_scrape=False,
        ),
        FeedSpec(
            source="The Wall Street Journal",
            homepage_url="https://www.wsj.com/",
            urls=[
                # Public RSS endpoints via Dow Jones. Some links may be paywalled.
                "https://feeds.content.dowjones.io/public/rss/RSSWorldNews",
                "https://feeds.content.dowjones.io/public/rss/RSSMarketsMain",
            ],
            use_homepage_scrape=False,
        ),
        FeedSpec(
            source="The Atlantic",
            homepage_url="https://www.theatlantic.com/",
            # The Atlantic's reliable RSS endpoint.
            urls=["https://www.theatlantic.com/feed/all/"],
            use_homepage_scrape=False,
        ),

        # --- Region ordering tweaks (user requested) ---
        # EU tab order: EUobserver -> El País -> France 24 -> Le Monde -> AFP -> NZZ

        # EU tab requested order: EUobserver -> El País -> France 24 -> Le Monde -> AFP -> NZZ
        FeedSpec(
            source="EUobserver",
            homepage_url="https://euobserver.com/",
            urls=["https://euobserver.com/rss"],
            use_homepage_scrape=False,
        ),
        FeedSpec(
            source="El País",
            homepage_url="https://elpais.com/",
            # English edition:
            urls=["https://feeds.elpais.com/mrss-s/pages/ep/site/english.elpais.com/portada"],
            use_homepage_scrape=False,
        ),
        FeedSpec(
            source="France 24",
            homepage_url="https://www.france24.com/en/",
            urls=["https://www.france24.com/en/rss"],
            use_homepage_scrape=False,
        ),
        FeedSpec(
            source="Le Monde",
            homepage_url="https://www.lemonde.fr/",
            urls=["https://www.lemonde.fr/rss/une.xml"],
            use_homepage_scrape=False,
        ),
        FeedSpec(
            source="Agence France-Presse",
            homepage_url="https://www.afp.com/",
            urls=[
                "https://news.google.com/rss/search?q=site%3Aafp.com+when%3A7d&hl=en&gl=FR&ceid=FR%3Aen",
            ],
            use_homepage_scrape=False,
        ),
        FeedSpec(
            source="NZZ",
            homepage_url="https://www.nzz.ch/",
            urls=["https://www.nzz.ch/startseite.rss"],
            use_homepage_scrape=False,
        ),

        # Int'l: AllAfrica (moved to bottom of Int'l list)
        FeedSpec(
            source="AllAfrica",
            homepage_url="https://allafrica.com/",
            urls=["http://allafrica.com/tools/headlines/rdf/latest/headlines.rdf"],
            use_homepage_scrape=False,
        ),

        # Pittsburgh local sources (PGH tab)
        FeedSpec(
            source="WESA",
            homepage_url="https://www.wesa.fm/",
            # WESA exposes `index.rss`, but it is frequently empty (channel-only) while still returning HTTP 200.
            # A constrained Google News RSS feed is the most reliable way to get timely headlines.
            urls=["https://news.google.com/rss/search?q=site%3Awesa.fm&hl=en-US&gl=US&ceid=US%3Aen"],
            use_homepage_scrape=False,
        ),
        FeedSpec(
            source="NEXTpittsburgh",
            homepage_url="https://nextpittsburgh.com/",
            urls=["https://nextpittsburgh.com/feed/"],
            use_homepage_scrape=False,
        ),
        FeedSpec(
            source="Pgh City Paper",
            homepage_url="https://www.pghcitypaper.com/",
            # Prefer the publisher RSS when it allows scripted clients (browser-like UA helps).
            # Keep Google News RSS as a fallback if the direct feed is temporarily blocked.
            urls=[
                "https://www.pghcitypaper.com/feed/",
                "https://news.google.com/rss/search?q=site%3Apghcitypaper.com&hl=en-US&gl=US&ceid=US%3Aen",
            ],
            use_homepage_scrape=False,
        ),
        FeedSpec(
            source="Pgh PublicSource",
            homepage_url="https://www.publicsource.org/",
            urls=["https://www.publicsource.org/feed/"],
            use_homepage_scrape=False,
        ),
        FeedSpec(
            source="Trib|Live",
            homepage_url="https://triblive.com/",
            urls=["https://triblive.com/category/top-stories/feed/"],
            use_homepage_scrape=False,
        ),
    ]


@dataclass(frozen=True)
class WeatherSummary:
    """
    Weather data we display in the page header.

    All temperatures are in Fahrenheit for readability in Pittsburgh.
    """

    current_f: float
    high_f: float
    low_f: float
    rain_probability_pct: Optional[int]
    weather_code: Optional[int]


@dataclass(frozen=True)
class LocationSpec:
    """
    A single named location for weather.

    We store lat/lon so we can call Open-Meteo directly without needing an API key.
    """

    name: str
    latitude: float
    longitude: float


def _safe_filename_from_url(url: str) -> str:
    """
    Turn an arbitrary URL into a safe local filename.

    We intentionally do NOT try to preserve the original filename, because:
    - URLs can be long and contain query parameters
    - multiple items may point to different URLs with the same basename
    - some URLs do not have a clear extension
    """

    h = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]

    # Best-effort guess at extension. If we can't find one, default to .jpg
    # because most news images are JPEGs. Browsers can often still display
    # images even if the extension is imperfect, but this is "good enough".
    m = re.search(r"\.(jpg|jpeg|png|webp|gif)(?:\?|$)", url, flags=re.IGNORECASE)
    ext = (m.group(1).lower() if m else "jpg").replace("jpeg", "jpg")

    return f"{h}.{ext}"


def _extract_image_url(entry: dict) -> Optional[str]:
    """
    Extract an image URL from a feed entry (best-effort).

    RSS/Atom feeds are not consistent. Images may appear in several places:
    - media_content / media_thumbnail (common in RSS)
    - links with rel="enclosure"
    - summary/content HTML (img tags)
    """

    # 1) media:content
    for key in ("media_content", "media_thumbnail"):
        media_list = entry.get(key) or []
        if isinstance(media_list, list) and media_list:
            url = media_list[0].get("url")
            if url:
                return url

    # 2) enclosures / links
    for link in entry.get("links") or []:
        # Some feeds place images as "enclosure" or provide a "type" like image/jpeg.
        rel = (link.get("rel") or "").lower()
        typ = (link.get("type") or "").lower()
        href = link.get("href")
        if not href:
            continue
        if rel == "enclosure" and typ.startswith("image/"):
            return href

    # 3) Look for an <img src="..."> inside summary/content HTML.
    html_blobs: list[str] = []
    if entry.get("summary"):
        html_blobs.append(str(entry["summary"]))
    for c in entry.get("content") or []:
        if isinstance(c, dict) and c.get("value"):
            html_blobs.append(str(c["value"]))

    img_re = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', flags=re.IGNORECASE)
    for blob in html_blobs:
        m = img_re.search(blob)
        if m:
            return m.group(1)

    return None


def _strip_html(text: str) -> str:
    """
    Remove HTML tags and collapse whitespace.

    Many RSS feeds put summaries in HTML. We keep this simple and robust by
    using BeautifulSoup's get_text().
    """

    soup = BeautifulSoup(text or "", "html.parser")
    cleaned = soup.get_text(" ", strip=True)
    return re.sub(r"\s+", " ", cleaned).strip()


def _truncate_to_two_sentences(text: str, max_chars: int = 220) -> str:
    """
    Convert an arbitrary text blob into a short 1–2 sentence excerpt.

    We use a lightweight heuristic: split on sentence-ending punctuation.
    If that fails, fall back to a character limit.
    """

    t = re.sub(r"\s+", " ", (text or "")).strip()
    if not t:
        return ""

    parts = re.split(r"(?<=[.!?])\s+", t)
    parts = [p.strip() for p in parts if p.strip()]
    if not parts:
        return (t[: max_chars - 1] + "…") if len(t) > max_chars else t

    excerpt = parts[0]
    if len(parts) > 1:
        candidate = f"{excerpt} {parts[1]}"
        excerpt = candidate if len(candidate) <= max_chars else excerpt

    if len(excerpt) > max_chars:
        excerpt = excerpt[: max_chars - 1].rstrip() + "…"
    return excerpt


def _extract_entry_summary(entry: dict) -> Optional[str]:
    """
    Extract a human-readable summary from a RSS/Atom entry (best-effort).
    """

    raw = entry.get("summary") or entry.get("description") or ""
    cleaned = _strip_html(str(raw))
    cleaned = _truncate_to_two_sentences(cleaned)
    return cleaned or None


def _first_non_empty(*values: Optional[str]) -> Optional[str]:
    """Return the first value that is a non-empty string."""

    for v in values:
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _fetch_feed(url: str, timeout_s: int = 20) -> feedparser.FeedParserDict:
    """
    Fetch a feed URL and parse it via feedparser.

    We use requests first to control headers and timeouts, then parse bytes.
    """

    headers = {
        **_browser_like_headers(),
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
        # RSS is still an HTTP resource; a Referer isn't required, but keeping it
        # stable avoids some bot-management edge cases.
        "Referer": "",
    }
    # Blank Referer keys can confuse some servers; remove empty headers.
    headers = {k: v for k, v in headers.items() if v != ""}

    # Some hosts (including local publishers) will intermittently rate-limit RSS (HTTP 429).
    # A small retry with backoff avoids dropping an entire row for a transient limit.
    last_exc: Optional[Exception] = None
    for attempt in range(4):
        try:
            resp = requests.get(url, headers=headers, timeout=timeout_s)
            if resp.status_code == 429:
                time.sleep(0.6 * (attempt + 1))
                continue
            resp.raise_for_status()
            return feedparser.parse(resp.content)
        except Exception as e:
            last_exc = e
            time.sleep(0.3 * (attempt + 1))
            continue

    assert last_exc is not None
    raise last_exc


def _fetch_html(url: str, timeout_s: int = 25) -> str:
    """
    Fetch a HTML page and return it as text.

    We keep headers similar to a normal browser to avoid simple bot blocks.
    """

    headers = _browser_like_headers()
    r = requests.get(url, headers=headers, timeout=timeout_s)
    r.raise_for_status()
    return r.text


def _normalize_url(base: str, maybe_relative: str) -> str:
    """
    Convert a relative URL to an absolute URL (best-effort).

    We avoid adding another dependency (urllib.parse is fine for this).
    """

    from urllib.parse import urljoin

    return urljoin(base, maybe_relative)


def _dedupe_by_link(items: Iterable[Headline]) -> list[Headline]:
    """Remove duplicates while preserving order (dedupe key = link)."""

    seen: set[str] = set()
    out: list[Headline] = []
    for it in items:
        if it.link in seen:
            continue
        seen.add(it.link)
        out.append(it)
    return out


def _collect_top_headlines_from_homepage(spec: FeedSpec, n: int) -> list[Headline]:
    """
    Collect "top" headlines by scraping the publisher homepage.

    This is used when RSS provides "some headlines" but not the top-of-site mix.
    The approach:
    - Fetch homepage HTML
    - Walk <a href="..."> in DOM order
    - Keep only links that look like real article URLs (via allow-regex)
    - Use link text as headline
    - Enrich image via og:image from the article page (best-effort)

    This is intentionally heuristic and resilient rather than perfect.
    """

    if not spec.homepage_link_allow_regex:
        return []

    allow = re.compile(spec.homepage_link_allow_regex)

    html_text = _fetch_html(spec.homepage_url, timeout_s=25)
    soup = BeautifulSoup(html_text, "html.parser")

    # Step 1: collect a buffer of candidate article links in DOM order.
    candidates: list[Headline] = []
    seen_links: set[str] = set()
    # We need a *large* candidate buffer because the "max 3 Trump items" rule can
    # cause us to skip a lot of otherwise-top stories on some days.
    #
    # If this buffer is too small, the selector can run out of non-Trump items
    # and end up returning fewer than 6 headlines (which "messes up" the layout).
    max_links_to_scan = 4000
    max_candidates = max(250, n * 40)
    scanned = 0
    for a in soup.find_all("a", href=True):
        scanned += 1
        if scanned > max_links_to_scan:
            break

        href = str(a.get("href"))
        if not href or href.startswith("#"):
            continue

        abs_url = _normalize_url(spec.homepage_url, href)
        if not allow.search(abs_url):
            continue

        # Ignore obvious non-article endpoints.
        if any(x in abs_url for x in ("/live/", "/video/", "/podcast", "/audio/", "/subscribe")):
            continue

        title = a.get_text(" ", strip=True)
        if not title or len(title) < 12:
            continue
        # Filter out obvious "non-headline" items.
        t_lower = title.lower()
        if any(x in t_lower for x in ("paid press release", "paid press releases", "advertisement", "subscribe", "sign in")):
            continue

        if abs_url in seen_links:
            continue
        seen_links.add(abs_url)

        candidates.append(Headline(source=spec.source, title=title, link=abs_url, summary=None, image_url=None))
        if len(candidates) >= max_candidates:
            break

    # Step 2: enrich (in parallel) + select with Trump cap using title+summary.
    #
    # Parallelism matters a lot here: each article metadata fetch is an HTTP request.
    # Running them concurrently typically cuts build time dramatically.
    session = _new_http_session()
    cache = _MetaCache(path=Path(__file__).resolve().parent / "cache" / "article-meta.json")
    cache.load()

    def enrich_one(it: Headline) -> Headline:
        img, desc = _fetch_article_meta(session, cache, it.link, timeout_s=20)
        return Headline(**{**it.__dict__, "image_url": img, "summary": desc})

    enriched_list: list[Headline] = []
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {pool.submit(enrich_one, it): it for it in candidates}
        for fut in as_completed(futures):
            enriched_list.append(fut.result())

    # Preserve original order (as_completed returns arbitrary order).
    by_link = {h.link: h for h in enriched_list}
    ordered_enriched = [by_link[it.link] for it in candidates if it.link in by_link]

    cache.save()

    selected: list[Headline] = []
    trump_count = 0
    for enriched in ordered_enriched:
        if _is_trump_item(enriched):
            if trump_count >= 3:
                continue
            trump_count += 1
        selected.append(enriched)
        if len(selected) >= n:
            break

    return selected


def _collect_economist_headlines_from_homepage(n: int) -> list[Headline]:
    """
    Collect "headline news" from The Economist by scraping their homepage.

    Why do this?
    - Some Economist RSS endpoints return 403, or omit images entirely.
    - The homepage usually contains the top stories with images.

    Implementation detail:
    - The Economist uses a modern JS app that often embeds a large JSON blob in
      a <script id="__NEXT_DATA__"> tag.
    - We'll parse that JSON and extract likely articles (headline + url + image).

    This is best-effort and intentionally defensive: if the structure changes,
    we degrade gracefully (we still render other sources).
    """

    homepage = "https://www.economist.com/"
    # Important: As of some periods, The Economist redirects unauthenticated
    # homepage requests to a subscribe page. If that happens, we won't be able
    # to scrape the homepage reliably (so we will fall back to RSS).
    html_text = _fetch_html(homepage)
    soup = BeautifulSoup(html_text, "html.parser")

    next_data = soup.find("script", attrs={"id": "__NEXT_DATA__"})
    if not next_data or not next_data.string:
        # Fallback: try to parse article links from HTML directly.
        return _collect_economist_headlines_from_homepage_fallback(html_text, n=n)

    try:
        data = json.loads(next_data.string)
    except Exception:
        return _collect_economist_headlines_from_homepage_fallback(html_text, n=n)

    # We recursively walk the JSON and collect candidates that look like articles.
    candidates: list[Headline] = []

    def walk(obj: object) -> None:
        if isinstance(obj, dict):
            # Common keys we might see. We intentionally keep this loose.
            title = _first_non_empty(
                obj.get("headline"),
                obj.get("title"),
                obj.get("displayHeadline"),
                obj.get("shortHeadline"),
            )
            url = _first_non_empty(obj.get("url"), obj.get("link"))

            # Image URLs can be nested (e.g., obj["image"]["url"]).
            image_url: Optional[str] = None
            img = obj.get("image") if isinstance(obj.get("image"), dict) else None
            if img:
                image_url = _first_non_empty(img.get("url"), img.get("src"), img.get("source"))

            # Some objects use "promoImage" or "leadImage" style keys.
            for k in ("promoImage", "leadImage", "heroImage"):
                if image_url:
                    break
                v = obj.get(k)
                if isinstance(v, dict):
                    image_url = _first_non_empty(v.get("url"), v.get("src"), v.get("source"))

            if title and url and "/20" in url:
                abs_url = _normalize_url(homepage, url)
                abs_img = _normalize_url(homepage, image_url) if image_url else None
                candidates.append(
                    Headline(
                        source="The Economist",
                        title=title,
                        link=abs_url,
                        image_url=abs_img,
                    )
                )

            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)

    walk(data)

    # Remove duplicates (homepage JSON can repeat the same story in multiple places).
    candidates = _dedupe_by_link(candidates)

    # Take the first n.
    return _take_latest(candidates, n)


def _collect_economist_headlines_from_homepage_fallback(html_text: str, n: int) -> list[Headline]:
    """
    Fallback Economist extraction if __NEXT_DATA__ isn't present/parseable.

    This scans for <a href="/..."> links that look like article pages and tries
    to pair them with a nearby image. It won't be as accurate as JSON parsing,
    but it helps keep the page populated when the site structure changes.
    """

    homepage = "https://www.economist.com/"
    soup = BeautifulSoup(html_text, "html.parser")

    out: list[Headline] = []
    for a in soup.find_all("a", href=True):
        href = str(a["href"])
        if not href.startswith("/"):
            continue
        # Heuristic: Economist article URLs typically contain a year segment (/2026/...).
        if not re.search(r"/20\d{2}/", href):
            continue

        title = a.get_text(" ", strip=True)
        if not title or len(title) < 10:
            continue

        # Try to find an image within the link, or in the immediate vicinity.
        img_tag = a.find("img")
        if not img_tag:
            img_tag = a.parent.find("img") if a.parent else None

        image_url = None
        if img_tag:
            image_url = _first_non_empty(img_tag.get("src"), img_tag.get("data-src"))

        out.append(
            Headline(
                source="The Economist",
                title=title,
                link=_normalize_url(homepage, href),
                image_url=_normalize_url(homepage, image_url) if image_url else None,
            )
        )

        if len(out) >= n:
            break

    return _dedupe_by_link(out)


def _extract_og_image(article_url: str, timeout_s: int = 25) -> Optional[str]:
    """
    Extract an OpenGraph image (og:image) from an article page.

    Many publishers (including The Economist) include a meta tag like:
      <meta property="og:image" content="https://...jpg">

    This is useful when RSS feeds don't include image URLs.

    Returns the image URL if found, otherwise None.
    """

    try:
        # We want the final URL too (to detect subscription redirects).
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        r = requests.get(article_url, headers=headers, timeout=timeout_s, allow_redirects=True)
        r.raise_for_status()
        final_url = str(r.url)
        html_text = r.text
    except Exception:
        return None

    # Some sites redirect to subscription pages; if so, don't treat that as success.
    if "economist.com" in final_url and "/subscribe" in final_url:
        return None

    # BeautifulSoup makes meta extraction straightforward and robust.
    soup = BeautifulSoup(html_text, "html.parser")
    tag = soup.find("meta", attrs={"property": "og:image"})
    if tag and tag.get("content"):
        return str(tag.get("content")).strip() or None

    # Some sites use "name=twitter:image" instead of og:image.
    tag2 = soup.find("meta", attrs={"name": "twitter:image"})
    if tag2 and tag2.get("content"):
        return str(tag2.get("content")).strip() or None

    return None


def _try_extract_og_image(article_url: str, timeouts_s: list[int]) -> Optional[str]:
    """
    Try extracting og:image with a few timeouts.

    Some sites (notably large US newspapers) can be slow; a single short timeout
    may cause us to miss otherwise-available images.
    """

    for t in timeouts_s:
        img = _extract_og_image(article_url, timeout_s=t)
        if img:
            return img
    return None


def _enrich_missing_media(headlines: list[Headline]) -> list[Headline]:
    """
    Fill missing image_url / summary for selected sources.

    Important constraints:
    - Some publishers are slow or intermittently block automated requests.
    - We cap enrichment work to keep builds from taking forever.
    """

    enrich_sources = {
        "The Economist",
        "Deutsche Welle",
        "Al Jazeera",
        "The Jerusalem Post",
        "The New York Times",
        "The Washington Post",
        # Pittsburgh locals:
        # Several of these use Google News RSS wrappers or feeds without embedded media,
        # so og:image extraction from the article page is the reliable path.
        "WESA",
        "NEXTpittsburgh",
        "Pgh City Paper",
        "Trib|Live",
        # New sources that often don't embed images in RSS:
        "RNZ",
        "EUobserver",
        "AllAfrica",
        "South China Morning Post",
        "USA Today",
        "Agence France-Presse",
        # Reuters feed items often lack media tags; og:image on article pages is reliable.
        "Reuters",
    }

    # Cap number of enrichment fetches per source per run.
    per_source_budget = {
        "The Washington Post": 2,  # WaPo is slow; keep it very small
        "The New York Times": 4,
        "The Economist": 4,
        "Deutsche Welle": 6,
        "The Jerusalem Post": 6,
        "Al Jazeera": 6,
        "WESA": 6,
        "NEXTpittsburgh": 6,
        "Pgh City Paper": 6,
        "Trib|Live": 6,
        "RNZ": 6,
        "EUobserver": 6,
        "AllAfrica": 6,
        "South China Morning Post": 6,
        "USA Today": 6,
        "Agence France-Presse": 6,
        "Reuters": 6,
    }
    used: dict[str, int] = {}

    session = _new_http_session()
    cache = _MetaCache(path=Path(__file__).resolve().parent / "cache" / "article-meta.json")
    cache.load()

    # First decide which URLs we want to enrich this run (respecting budgets).
    to_enrich: list[Headline] = []
    passthrough: list[Headline] = []
    for h in headlines:
        if h.source not in enrich_sources:
            passthrough.append(h)
            continue

        budget = per_source_budget.get(h.source, 0)
        count = used.get(h.source, 0)
        if count >= budget:
            passthrough.append(h)
            continue

        used[h.source] = count + 1
        to_enrich.append(h)

    def enrich_one(h: Headline) -> Headline:
        # Important: a headline can have a summary but still be missing an image
        # (common for feeds that include descriptions but no media). In that case
        # we still want to attempt og:image extraction.
        #
        # Also important: Google News wrapper pages often produce a generic Google
        # og:image thumbnail (lh3.googleusercontent.com). Those are not useful and
        # should be replaced with the publisher's og:image when possible.
        img_url_str = str(h.image_url or "")
        is_google_placeholder_img = (
            bool(h.image_url)
            and (
                ("lh3.googleusercontent.com" in img_url_str)
                or ("googleusercontent.com" in img_url_str)
                or ("gstatic.com/images/branding" in img_url_str)
            )
        )

        if h.image_url and h.summary and not is_google_placeholder_img:
            return h

        # Some independent publishers aggressively rate-limit concurrent requests.
        # A tiny per-fetch delay is usually enough to avoid HTTP 429 responses during builds.
        if h.source in {"NEXTpittsburgh"}:
            time.sleep(0.35)

        # A few sources are notably slower.
        timeout = 35 if h.source in {"The Washington Post"} else 20
        img, desc = _fetch_article_meta(session, cache, h.link, timeout_s=timeout)

        # If the current image is a Google placeholder, prefer the publisher image we just fetched.
        new_img = h.image_url
        if img and (not h.image_url or is_google_placeholder_img):
            new_img = img

        return Headline(**{**h.__dict__, "image_url": new_img, "summary": (h.summary or desc)})

    enriched_map: dict[str, Headline] = {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(enrich_one, h): h for h in to_enrich}
        for fut in as_completed(futures):
            enriched = fut.result()
            enriched_map[enriched.link] = enriched

    cache.save()

    # Reconstruct original order.
    out: list[Headline] = []
    for h in headlines:
        out.append(enriched_map.get(h.link, h))
    return out


def _extract_meta_description(article_url: str, timeout_s: int = 25) -> Optional[str]:
    """
    Extract a short description from an article page.

    We try, in order:
    - og:description
    - meta name="description"
    - meta name="twitter:description"
    """

    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        r = requests.get(article_url, headers=headers, timeout=timeout_s, allow_redirects=True)
        r.raise_for_status()
        final_url = str(r.url)
        if "economist.com" in final_url and "/subscribe" in final_url:
            return None

        soup = BeautifulSoup(r.text, "html.parser")
        for selector in (
            ("meta", {"property": "og:description"}),
            ("meta", {"name": "description"}),
            ("meta", {"name": "twitter:description"}),
        ):
            tag = soup.find(selector[0], attrs=selector[1])
            if tag and tag.get("content"):
                cleaned = _truncate_to_two_sentences(_strip_html(str(tag.get("content"))))
                return cleaned or None
    except Exception:
        return None

    return None


def _fetch_article_meta(
    session: requests.Session,
    cache: _MetaCache,
    article_url: str,
    timeout_s: int = 20,
) -> tuple[Optional[str], Optional[str]]:
    """
    Fetch BOTH:
    - a representative image URL (og:image / twitter:image)
    - a 1–2 sentence description (og:description / meta description)

    in a single HTTP request.

    This is faster than calling separate functions that each download the page.
    """

    cached = cache.get(article_url)
    if cached is not None:
        # AFP pages often have a description but no og:image, even though a usable
        # inline <img> exists. If we cached (None, description) previously, allow
        # re-fetch so our HTML-image fallback can populate the image.
        if (cached[0] is None) and ("afp.com/" in article_url):
            cached = None
        else:
            return cached

    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }

        # Some publishers respond with HTTP 429 when many article pages are fetched in parallel.
        # A couple quick retries with backoff usually succeeds without slowing the whole build much.
        r = None
        for attempt in range(4):
            r = session.get(article_url, headers=headers, timeout=timeout_s, allow_redirects=True)
            if r.status_code == 429:
                time.sleep(0.35 * (attempt + 1))
                continue
            r.raise_for_status()
            break

        if r is None:
            raise RuntimeError("No response")
        final_url = str(r.url)

        # Avoid treating subscription redirects as valid article pages.
        if "economist.com" in final_url and "/subscribe" in final_url:
            cache.set(article_url, None, None)
            return (None, None)

        soup = BeautifulSoup(r.text, "html.parser")

        # Image
        img = None
        for attrs in (
            {"property": "og:image"},
            {"property": "og:image:url"},
            {"name": "twitter:image"},
            {"name": "twitter:image:src"},
            {"name": "parsely-image-url"},
        ):
            tag = soup.find("meta", attrs=attrs)
            if tag and tag.get("content"):
                img = str(tag.get("content")).strip() or None
                break

        # JSON-LD fallback (common on modern sites, and sometimes present even when og:image isn't).
        if not img:
            for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
                try:
                    if not script.string:
                        continue
                    data = json.loads(script.string)
                except Exception:
                    continue

                # JSON-LD can be:
                # - a dict
                # - a list of dicts
                # - a dict with "@graph": [...]
                candidates: list[object] = []
                if isinstance(data, list):
                    candidates = data
                elif isinstance(data, dict):
                    if isinstance(data.get("@graph"), list):
                        candidates = list(data.get("@graph") or [])
                    else:
                        candidates = [data]

                def extract_image(obj: object) -> Optional[str]:
                    if not isinstance(obj, dict):
                        return None
                    im = obj.get("image")
                    if isinstance(im, str):
                        return im
                    if isinstance(im, list) and im and isinstance(im[0], str):
                        return im[0]
                    if isinstance(im, dict):
                        # Common schema: {"url": "..."} or {"@type":"ImageObject","url":"..."}
                        u = im.get("url")
                        if isinstance(u, str):
                            return u
                        if isinstance(u, list) and u and isinstance(u[0], str):
                            return u[0]
                    return None

                for obj in candidates:
                    found = extract_image(obj)
                    if found and isinstance(found, str) and found.startswith(("http://", "https://")):
                        img = found
                        break
                if img:
                    break

        # Some publishers (notably afp.com nodes) do not include og:image but do include
        # a representative image in the HTML. As a fallback, pick the first non-trivial <img>.
        if not img:
            for im in soup.find_all("img"):
                src = im.get("src") or im.get("data-src") or ""
                src = str(src).strip()
                if not src or src.startswith("data:"):
                    continue
                if not (src.startswith("http://") or src.startswith("https://")):
                    continue
                low = src.lower()
                if any(x in low for x in ("logo", ".svg", "doubleclick.net", "ct?id=bwnews")):
                    continue
                if not re.search(r"\.(jpg|jpeg|png|webp)(?:\\?|$)", low):
                    continue
                img = src
                break

        # Description
        desc = None
        for attrs in (
            {"property": "og:description"},
            {"name": "description"},
            {"name": "twitter:description"},
        ):
            tag = soup.find("meta", attrs=attrs)
            if tag and tag.get("content"):
                desc = _truncate_to_two_sentences(_strip_html(str(tag.get("content")))) or None
                break

        cache.set(article_url, img, desc)
        return (img, desc)
    except Exception:
        cache.set(article_url, None, None)
        return (None, None)


def _write_placeholder_images(dist_dir: Path) -> dict[str, str]:
    """
    Write simple local placeholder images for sources that are blocked for images.

    Returns: source -> relative path under dist/.
    """

    out_dir = dist_dir / "static" / "placeholders"
    out_dir.mkdir(parents=True, exist_ok=True)

    # Previously we used a placeholder image for the Economist.
    # The user now prefers text-only cards for sources that can't reliably show
    # photos, so we return no placeholders.
    return {}


def _fetch_weather(lat: float, lon: float) -> Optional[WeatherSummary]:
    """
    Fetch current + daily weather for a given lat/lon.

    Data source: Open-Meteo (no API key required).
    We request:
    - current temperature
    - today's high/low
    - today's max precipitation probability
    - weather code for a small icon
    """

    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        "&current=temperature_2m,weather_code"
        "&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code"
        "&temperature_unit=fahrenheit"
        "&timezone=America%2FNew_York"
    )

    try:
        r = requests.get(url, headers={"User-Agent": "NewsOfTheDay/1.0 (+local script; personal use)"}, timeout=20)
        r.raise_for_status()
        data = r.json()

        current = data.get("current") or {}
        daily = data.get("daily") or {}

        current_f = float(current.get("temperature_2m"))
        current_code = current.get("weather_code")
        current_code_int = int(current_code) if current_code is not None else None

        # daily values are arrays; index 0 corresponds to "today" in the chosen timezone.
        highs = daily.get("temperature_2m_max") or []
        lows = daily.get("temperature_2m_min") or []
        rain_probs = daily.get("precipitation_probability_max") or []
        codes = daily.get("weather_code") or []

        high_f = float(highs[0]) if highs else float("nan")
        low_f = float(lows[0]) if lows else float("nan")
        rain_probability_pct = int(rain_probs[0]) if rain_probs else None
        code_int = int(codes[0]) if codes else current_code_int

        # If daily highs/lows are missing, treat the whole weather widget as unavailable.
        if high_f != high_f or low_f != low_f:  # NaN check
            return None

        return WeatherSummary(
            current_f=current_f,
            high_f=high_f,
            low_f=low_f,
            rain_probability_pct=rain_probability_pct,
            weather_code=code_int,
        )
    except Exception:
        # Non-fatal. The page should still build if weather is unavailable.
        return None


def _default_location() -> LocationSpec:
    """Default location shown on first load (used if the user hasn't customized)."""

    return LocationSpec(name="Pittsburgh, PA", latitude=40.4406, longitude=-79.9959)


def _meteoblue_week_url(latitude: float, longitude: float) -> str:
    """
    Build a MeteoBlue "week" forecast URL that actually resolves to the coordinates.

    The simple query form (?lat=&lon=) is unreliable in practice (can ignore coords or
    follow stale site state). MeteoBlue's stable pattern encodes lat/lon in the path,
    e.g. ``40.441N79.996W0_UTC`` for Pittsburgh.
    """

    lat = float(latitude)
    lon = float(longitude)
    lat_part = f"{abs(lat):.3f}{'N' if lat >= 0 else 'S'}"
    lon_part = f"{abs(lon):.3f}{'E' if lon >= 0 else 'W'}"
    # ``0`` = elevation placeholder; ``UTC`` keeps the link generic without guessing IANA tz.
    return f"https://www.meteoblue.com/en/weather/week/{lat_part}{lon_part}0_UTC"


def _meteoblue_named_week_url(latitude: float, longitude: float, place_query: str) -> str:
    """
    Build a MeteoBlue week URL using their public location search, pinned to lat/lon.

    Coordinate-only ``/week/{lat}{lon}...`` links often resolve to the wrong town name
    on MeteoBlue's side. Their JSON search (same as the site search box) returns a
    stable slug path like ``pittsburgh_united-states_5206379``. We pick the result
    closest to our Open-Meteo coordinates so the page matches the widget.

    Falls back to ``_meteoblue_week_url`` if search fails or nothing is close enough.
    """

    q = (place_query or "").strip()
    if not q:
        return _meteoblue_week_url(latitude, longitude)
    try:
        r = requests.get(
            "https://www.meteoblue.com/en/server/search/query3",
            params={"query": q},
            headers={"User-Agent": "NewsOfTheDay/1.0 (+local script; personal use)"},
            timeout=12,
        )
        r.raise_for_status()
        data = r.json()
        results = data.get("results") or []
        latf = float(latitude)
        lonf = float(longitude)
        best: Optional[dict] = None
        best_d = float("inf")
        for item in results:
            if item.get("lat") is None or item.get("lon") is None or not item.get("url"):
                continue
            try:
                ilat = float(item["lat"])
                ilon = float(item["lon"])
            except (TypeError, ValueError):
                continue
            dlat = ilat - latf
            dlon = ilon - lonf
            d = dlat * dlat + dlon * dlon
            if d < best_d:
                best_d = d
                best = item
        if best is None:
            return _meteoblue_week_url(latitude, longitude)
        # ~0.25° is roughly 25–30 km; beyond that, keep the coordinate URL instead.
        if best_d > (0.25 * 0.25):
            return _meteoblue_week_url(latitude, longitude)
        return f"https://www.meteoblue.com/en/weather/week/{best['url']}"
    except Exception:
        return _meteoblue_week_url(latitude, longitude)


# Written after each successful full build; consumed by ``python build.py --use-cache``.
_BUILD_CACHE_VERSION = 1


def _build_cache_path(project_dir: Path) -> Path:
    return project_dir / "cache" / "build-cache.json"


def _headline_to_cache_dict(h: Headline) -> dict:
    return {
        "source": h.source,
        "title": h.title,
        "link": h.link,
        "summary": h.summary,
        "image_url": h.image_url,
        "image_path": h.image_path,
    }


def _headline_from_cache_dict(d: dict) -> Headline:
    return Headline(
        source=d["source"],
        title=d["title"],
        link=d["link"],
        summary=d.get("summary"),
        image_url=d.get("image_url"),
        image_path=d.get("image_path"),
    )


def _weather_to_cache_dict(w: WeatherSummary) -> dict:
    return {
        "current_f": w.current_f,
        "high_f": w.high_f,
        "low_f": w.low_f,
        "rain_probability_pct": w.rain_probability_pct,
        "weather_code": w.weather_code,
    }


def _weather_from_cache_dict(d: dict) -> WeatherSummary:
    return WeatherSummary(
        current_f=float(d["current_f"]),
        high_f=float(d["high_f"]),
        low_f=float(d["low_f"]),
        rain_probability_pct=d.get("rain_probability_pct"),
        weather_code=d.get("weather_code"),
    )


def _save_build_cache(
    project_dir: Path,
    headlines: list[Headline],
    weather: Optional[WeatherSummary],
) -> None:
    """Persist last full build so ``--use-cache`` can regenerate HTML quickly."""

    path = _build_cache_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Keep a version number so we can change the JSON shape later and old files are ignored safely.
    payload = {
        "version": _BUILD_CACHE_VERSION,
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "headlines": [_headline_to_cache_dict(h) for h in headlines],
        "weather": _weather_to_cache_dict(weather) if weather else None,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _load_build_cache(project_dir: Path) -> Optional[tuple[list[Headline], Optional[WeatherSummary]]]:
    """Return (headlines, weather) from the last successful full build, or None if missing/stale."""

    path = _build_cache_path(project_dir)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("version") != _BUILD_CACHE_VERSION:
            return None
        raw_h = payload.get("headlines") or []
        if not raw_h:
            return None
        headlines = [_headline_from_cache_dict(x) for x in raw_h]
        wraw = payload.get("weather")
        weather = _weather_from_cache_dict(wraw) if wraw else None
        return (headlines, weather)
    except Exception:
        return None


def _reconcile_cached_image_paths(dist_dir: Path, headlines: list[Headline]) -> list[Headline]:
    """
    Drop ``image_path`` when the file is missing (e.g. user cleared ``dist/`` but kept cache).

    Keeps the page valid without re-downloading during a ``--use-cache`` run.
    """

    out: list[Headline] = []
    for h in headlines:
        ip = h.image_path
        if ip:
            rel = ip.replace("/", os.sep)
            if not (dist_dir / rel).is_file():
                ip = None
        if ip != h.image_path:
            out.append(Headline(**{**h.__dict__, "image_path": ip}))
        else:
            out.append(h)
    return out


def _weather_icon_svg(weather_code: Optional[int]) -> str:
    """
    Return a small line-icon SVG for a given Open-Meteo weather code.

    We don't attempt to perfectly represent every code; we map codes into a few
    human-friendly categories (sun/cloud/rain/snow/storm/fog).
    """

    category = _weather_category_from_code(weather_code)

    # Simple, crisp line icons (stroke only). Sized to 22x22.
    # Note: we inline SVG to avoid extra assets and keep the output static.
    common = 'fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"'

    if category == "sun":
        return (
            f'<svg class="wicon" viewBox="0 0 24 24" aria-hidden="true">'
            f'<circle cx="12" cy="12" r="4" {common}/>'
            f'<path d="M12 2v3M12 19v3M2 12h3M19 12h3M4.5 4.5l2.1 2.1M17.4 17.4l2.1 2.1M19.5 4.5l-2.1 2.1M6.6 17.4l-2.1 2.1" {common}/>'
            f"</svg>"
        )
    if category == "fog":
        return (
            f'<svg class="wicon" viewBox="0 0 24 24" aria-hidden="true">'
            f'<path d="M5 9h14M4 12h16M6 15h12M7 18h10" {common}/>'
            f"</svg>"
        )
    if category == "snow":
        return (
            f'<svg class="wicon" viewBox="0 0 24 24" aria-hidden="true">'
            f'<path d="M7 14a5 5 0 0 1 9.7-1.6A4 4 0 1 1 17 20H8a4 4 0 0 1-1-6" {common}/>'
            f'<path d="M9 21l.5-1.2M12 21l.5-1.2M15 21l.5-1.2" {common}/>'
            f"</svg>"
        )
    if category == "storm":
        return (
            f'<svg class="wicon" viewBox="0 0 24 24" aria-hidden="true">'
            f'<path d="M7 14a5 5 0 0 1 9.7-1.6A4 4 0 1 1 17 20H8a4 4 0 0 1-1-6" {common}/>'
            f'<path d="M13 13l-3 5h3l-2 4" {common}/>'
            f"</svg>"
        )
    if category == "rain":
        return (
            f'<svg class="wicon" viewBox="0 0 24 24" aria-hidden="true">'
            f'<path d="M7 14a5 5 0 0 1 9.7-1.6A4 4 0 1 1 17 20H8a4 4 0 0 1-1-6" {common}/>'
            f'<path d="M9 20l-1 2M13 20l-1 2M17 20l-1 2" {common}/>'
            f"</svg>"
        )

    # cloud / default
    return (
        f'<svg class="wicon" viewBox="0 0 24 24" aria-hidden="true">'
        f'<path d="M7 14a5 5 0 0 1 9.7-1.6A4 4 0 1 1 17 20H8a4 4 0 0 1-1-6" {common}/>'
        f"</svg>"
    )

def _weather_category_from_code(weather_code: Optional[int]) -> str:
    """
    Map an Open-Meteo weather code to a small set of UI categories.

    We use this for:
    - icon selection
    - icon color theming
    """

    if weather_code is None:
        return "cloud"
    if weather_code == 0:
        return "sun"
    if weather_code in (1, 2, 3):
        return "cloud"
    if weather_code in (45, 48):
        return "fog"
    if weather_code in (51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82):
        return "rain"
    if weather_code in (71, 73, 75, 77, 85, 86):
        return "snow"
    if weather_code in (95, 96, 99):
        return "storm"
    return "cloud"


def _weather_brief_description(weather_code: Optional[int]) -> str:
    """
    Return a short human-friendly weather description for a given Open-Meteo code.

    We keep this intentionally brief because it appears in the header UI.
    """

    if weather_code is None:
        return "Partly cloudy"
    if weather_code == 0:
        return "Clear"
    if weather_code in (1, 2, 3):
        return "Partly cloudy"
    if weather_code in (45, 48):
        return "Fog"
    if weather_code in (51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82):
        return "Rain"
    if weather_code in (71, 73, 75, 77, 85, 86):
        return "Snow"
    if weather_code in (95, 96, 99):
        return "Thunderstorms"
    return "Partly cloudy"


def _download_image(url: str, out_path: Path, referer: Optional[str] = None, timeout_s: int = 25) -> bool:
    """
    Download an image to out_path.

    Returns True if download succeeded (or file already exists), else False.
    """

    out_path.parent.mkdir(parents=True, exist_ok=True)

    if out_path.exists() and out_path.stat().st_size > 0:
        return True

    headers = {
        "User-Agent": "NewsOfTheDay/1.0 (+local script; personal use)",
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        # Many publisher CDNs expect the *article page* as a referrer, not the image URL itself.
        "Referer": (referer or url),
    }

    try:
        with requests.get(url, headers=headers, timeout=timeout_s, stream=True) as r:
            r.raise_for_status()
            # Write in chunks so we don't load large images into memory.
            with open(out_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=1024 * 64):
                    if chunk:
                        f.write(chunk)
        return out_path.exists() and out_path.stat().st_size > 0
    except Exception:
        # We keep failures non-fatal: missing images shouldn't break the page.
        return False


def _download_source_icons(dist_dir: Path, *, local_only: bool = False) -> dict[str, str]:
    """
    Resolve a logo/icon image for each news source for the left banner.

    Priority:
    1) Files you place in the *repo* (tracked in git; no download):
       - static/logos/<name>.{png,svg,webp,jpg,jpeg,ico}
       - static/icons/<name>.{...}   (optional; same idea)
       These are copied into dist/static/logos/ so GitHub Pages always has them.
    2) Files already present in the build output (no download):
       - dist/static/logos/<name>.{...}
       - dist/static/icons/<name>.{...}   (legacy / favicon cache)
    3) If nothing is found locally, fall back to downloading a favicon from the
       site's homepage (best-effort).

    Put your logos under static/logos/ using the slug below, e.g.:
      the-economist.png
      the-guardian.svg

    Returns a mapping: source name -> relative path under dist/ (for HTML).

    Args:
        local_only: If True, only reuse/copy icons already on disk (no HTTP). Used for
            fast ``python build.py --use-cache`` rebuilds.
    """

    # "static/" here refers to a *tracked* folder in the git repo next to build.py.
    # Users can drop their preferred icons into static/logos and commit them, and
    # the build will copy them into dist/ so the published site always uses them.
    project_dir = Path(__file__).resolve().parent
    project_logos_dir = project_dir / "static" / "logos"
    project_icons_dir = project_dir / "static" / "icons"

    logos_dir = dist_dir / "static" / "logos"
    logos_dir.mkdir(parents=True, exist_ok=True)
    icons_dir = dist_dir / "static" / "icons"
    icons_dir.mkdir(parents=True, exist_ok=True)

    exts = (".png", ".svg", ".webp", ".jpg", ".jpeg", ".ico")

    # Make builds deterministic:
    # `dist/` is a build output and may contain stale files from a prior run.
    # If we re-use old downloaded favicons blindly, we can keep broken/incorrect
    # extensions around forever. Clearing only the downloaded icon cache makes
    # the build repeatable while still allowing user-provided repo icons
    # (`static/logos/*`) to take precedence.
    #
    # Fast ``--use-cache`` builds pass ``local_only=True`` so we neither wipe nor
    # re-download banner icons.
    if not local_only:
        for p in icons_dir.iterdir():
            if p.is_file() and p.suffix.lower() in exts:
                try:
                    p.unlink()
                except OSError:
                    pass

    # Some sites return tiny/blank favicons (e.g. a 1×1 pixel). If a *downloaded*
    # favicon is suspiciously small, we treat it as invalid and try a different fallback.
    #
    # Important: we do NOT want to hide user-provided local icons just because they're
    # small. A minimal ICO can legitimately be only a few hundred bytes.
    min_downloaded_icon_bytes = 64

    # Extra filename aliases for common branding filenames.
    _ALIASES: dict[str, list[str]] = {
        "The Economist": ["economist", "theeconomist", "the_economist"],
        "The Guardian": ["guardian", "theguardian", "the_guardian"],
        "Al Jazeera": ["al-jazeera", "aljazeera"],
        "El País": ["elpais", "el-pais"],
        "Le Monde": ["lemonde", "le-monde"],
        "France 24": ["france24", "france-24"],
        "AllAfrica": ["allafrica", "all-africa"],
        "EUobserver": ["euobserver", "eu-observer"],
        "BBC World Service": ["bbc-world-service", "world-service", "bbc-ws"],
        "The Diplomat": ["thediplomat", "the-diplomat", "diplomat"],
        "Financial Times": ["ft", "financial-times"],
        "USA Today": ["usa-today", "usatoday"],
        "Vox": ["vox"],
        "Axios": ["axios"],
        "Fox News": ["fox-news", "foxnews", "fox"],
        "WESA": ["wesa", "90-5-wesa"],
        "NEXTpittsburgh": ["nextpittsburgh", "next-pittsburgh"],
        "Pgh City Paper": ["pghcitypaper", "pgh-city-paper", "pittsburgh-city-paper", "city-paper"],
        "Pgh PublicSource": ["publicsource", "pgh-publicsource", "pittsburgh-publicsource"],
        "Trib|Live": ["triblive", "trib-live", "trib"],
        "South China Morning Post": ["scmp", "south-china-morning-post"],
        "The Jerusalem Post": ["jerusalem-post", "jpost"],
        "The New York Times": ["nyt", "new-york-times"],
        "The Washington Post": ["wapo", "washington-post"],
        "The Wall Street Journal": ["wsj", "wall-street-journal"],
        "BBC News": ["bbc"],
        "Associated Press": ["ap", "associated-press"],
        "Reuters": ["reuters"],
        "Politico": ["politico"],
        "Times of India": ["times-of-india", "toi", "timesofindia"],
        "Agence France-Presse": ["afp", "afp-com", "agence-france-presse"],
        "Deutsche Welle": ["dw", "deutsche-welle"],
        "Süddeutsche Zeitung": ["sz", "sueddeutsche", "sueddeutsche-zeitung", "sueddeutschezeitung"],
        "Tagesschau": ["tagesschau"],
        "RNZ": ["rnz", "rhein-neckar-zeitung"],
        "NZZ": ["nzz", "neue-zuercher-zeitung"],
    }

    # Per-source forced favicon endpoints (best-effort).
    #
    # BBC World Service's default page often redirects into BBC Sounds and can
    # produce a non-representative (or tiny) icon. We instead pin to BBC's main favicon.
    _FORCED_ICON_URL: dict[str, str] = {
        # UK sources:
        # - Some publishers block simple HTTP clients (403 HTML) or serve favicons that
        #   look odd when scaled down. Pinning to a small, widely-available favicon endpoint
        #   keeps the left banner icons consistent.
        "The Economist": "https://www.google.com/s2/favicons?domain=economist.com&sz=128",
        "The Guardian": "https://www.google.com/s2/favicons?domain=theguardian.com&sz=128",
        "BBC News": "https://www.google.com/s2/favicons?domain=bbc.com&sz=128",
        "Financial Times": "https://www.google.com/s2/favicons?domain=ft.com&sz=128",
        "Reuters": "https://www.google.com/s2/favicons?domain=reuters.com&sz=128",
        "Politico": "https://www.google.com/s2/favicons?domain=politico.com&sz=128",
        "Times of India": "https://www.google.com/s2/favicons?domain=indiatimes.com&sz=128",
        "Agence France-Presse": "https://www.google.com/s2/favicons?domain=afp.com&sz=128",
        "BBC World Service": "https://www.bbc.com/favicon.ico",
        "Deutsche Welle": "https://www.dw.com/favicon.ico",
        "RNZ": "https://www.rnz.de/favicon.ico",
        "Le Monde": "https://www.lemonde.fr/favicon.ico",
        "South China Morning Post": "https://www.scmp.com/favicon.ico",
        "The Diplomat": "https://thediplomat.com/favicon.ico",
        "The Jerusalem Post": "https://www.jpost.com/favicon.ico",
        "USA Today": "https://www.usatoday.com/favicon.ico",
        "Vox": "https://www.vox.com/static-assets/icons/favicon.ico",
        "Axios": "https://www.axios.com/favicon.ico",
        "The Atlantic": "https://www.theatlantic.com/favicon.ico",
        "Fox News": "https://www.google.com/s2/favicons?domain=foxnews.com&sz=128",
    }

    # Sources where we prefer the downloaded/pinned icon over any repo-local
    # `static/logos/*` file.
    #
    # Why:
    # - Some checked-in logos are intentionally simplified placeholders.
    # - For a few publishers, a favicon endpoint produces a clearer mark at 24×24.
    _DOWNLOAD_FIRST_ICON_SOURCES: set[str] = {
        "Financial Times",
    }

    def _find_local_file(slug: str) -> Optional[Path]:
        # Search order matters:
        # - repo static/ (tracked) first
        # - then dist/ (already-built artifacts)
        for base in (project_logos_dir, project_icons_dir, logos_dir, icons_dir):
            for ext in exts:
                p = base / f"{slug}{ext}"
                if p.exists() and p.stat().st_size > 0:
                    return p
        return None

    sources: list[tuple[str, str]] = [(s.source, s.homepage_url) for s in _feed_specs()]

    out: dict[str, str] = {}

    for source, homepage in sources:
        slug = re.sub(r"[^a-z0-9]+", "-", source.lower()).strip("-")
        candidates = [slug] + _ALIASES.get(source, [])

        found: Optional[Path] = None
        if source not in _DOWNLOAD_FIRST_ICON_SOURCES:
            for cand in candidates:
                found = _find_local_file(cand)
                if found:
                    break

        if found is not None:
            # If the icon lives in the repo (static/...), copy it into dist/static/logos/
            # so it becomes part of the published Pages artifact.
            try:
                rel_to_project = found.relative_to(project_dir)
            except ValueError:
                rel_to_project = None

            if rel_to_project is not None and rel_to_project.parts and rel_to_project.parts[0] == "static":
                # Keep the exact filename (it already uses the correct slug/alias).
                dest = logos_dir / found.name
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(found, dest)
                out[source] = f"static/logos/{dest.name}"
            else:
                # Already in dist/; just reference it.
                rel = found.relative_to(dist_dir)
                out[source] = rel.as_posix()
            continue

        if local_only:
            continue

        # Fallback: download a site icon into icons/ (only when no local asset).
        #
        # Many publishers do *not* serve a Windows `.ico` file at their icon URL.
        # They may serve PNG or SVG. If we always save as `.ico`, we can end up with
        # a valid PNG/SVG file that has the wrong extension, which some browsers
        # refuse to render in an <img>.
        icon_url: Optional[str] = None
        if source in _FORCED_ICON_URL:
            icon_url = _FORCED_ICON_URL[source]
        try:
            if not icon_url:
                html_text = _fetch_html(homepage, timeout_s=20)
                soup = BeautifulSoup(html_text, "html.parser")
                for rel in ("icon", "shortcut icon", "apple-touch-icon"):
                    tag = soup.find(
                        "link",
                        rel=lambda v: isinstance(v, (str, list)) and rel in (v if isinstance(v, str) else " ".join(v)),
                    )
                    if tag and tag.get("href"):
                        icon_url = _normalize_url(homepage, str(tag.get("href")))
                        break
        except Exception:
            icon_url = None

        if not icon_url:
            icon_url = _normalize_url(homepage, "/favicon.ico")

        def _sniff_icon_ext(content: bytes, content_type: Optional[str], url_hint: str) -> str:
            ct = (content_type or "").lower()
            hint = (url_hint or "").lower()

            # SVG (text) — allow for XML prolog or whitespace.
            stripped = content.lstrip()
            if "image/svg" in ct or hint.endswith(".svg") or stripped.startswith(b"<svg") or stripped.startswith(b"<?xml"):
                return ".svg"

            # PNG
            if "image/png" in ct or hint.endswith(".png") or content.startswith(b"\x89PNG\r\n\x1a\n"):
                return ".png"

            # ICO
            if (
                "image/x-icon" in ct
                or "image/vnd.microsoft.icon" in ct
                or hint.endswith(".ico")
                or content.startswith(b"\x00\x00\x01\x00")
            ):
                return ".ico"

            # WebP
            if "image/webp" in ct or hint.endswith(".webp") or (content.startswith(b"RIFF") and b"WEBP" in content[:16]):
                return ".webp"

            # JPEG
            if "image/jpeg" in ct or hint.endswith(".jpg") or hint.endswith(".jpeg") or content.startswith(b"\xff\xd8\xff"):
                return ".jpg"

            # Default to `.ico` as a broad fallback.
            return ".ico"

        def _looks_like_html(content: bytes, content_type: Optional[str]) -> bool:
            ct = (content_type or "").lower()
            if "text/html" in ct:
                return True
            head = (content or b"").lstrip()[:64].lower()
            return head.startswith(b"<!doctype html") or head.startswith(b"<html")

        def _domain_from_homepage(home: str) -> Optional[str]:
            try:
                from urllib.parse import urlparse

                u = urlparse(home)
                host = (u.hostname or "").lower()
                return host or None
            except Exception:
                return None

        def _fallback_icon_urls(home: str) -> list[str]:
            """
            When a publisher blocks direct favicon fetches (403 HTML) or returns an HTML
            error page, fall back to small third-party favicon endpoints.

            These are best-effort and meant for personal/homepage use only.
            """
            dom = _domain_from_homepage(home)
            if not dom:
                return []
            return [
                f"https://www.google.com/s2/favicons?domain={dom}&sz=128",
                f"https://icons.duckduckgo.com/ip3/{dom}.ico",
            ]

        def _try_download_icon(url: str) -> tuple[bytes, str]:
            r = requests.get(
                url,
                headers={"User-Agent": "NewsOfTheDay/1.0 (+local script; personal use)"},
                timeout=20,
            )
            r.raise_for_status()
            content = r.content or b""
            return content, (r.headers.get("Content-Type") or "")

        try:
            tried: list[str] = []
            content, ctype = b"", ""
            last_err: Optional[Exception] = None

            for candidate in [icon_url, *_fallback_icon_urls(homepage)]:
                if not candidate or candidate in tried:
                    continue
                tried.append(candidate)
                try:
                    content, ctype = _try_download_icon(candidate)
                    if _looks_like_html(content, ctype):
                        continue
                    ext = _sniff_icon_ext(content, ctype, candidate)
                    out_path = icons_dir / f"{slug}{ext}"
                    with open(out_path, "wb") as f:
                        f.write(content)

                    ok_size = out_path.suffix.lower() == ".svg" or (
                        out_path.exists() and out_path.stat().st_size >= min_downloaded_icon_bytes
                    )
                    if ok_size:
                        out[source] = f"static/icons/{out_path.name}"
                        break
                except Exception as e:
                    last_err = e
                    continue
            else:
                # If everything failed, swallow quietly (missing icon is non-fatal).
                if last_err:
                    raise last_err
        except Exception:
            continue

    return out


def _copy_site_static_assets(project_dir: Path, dist_dir: Path) -> None:
    """
    Copy user-provided, git-tracked site assets into dist/ so GitHub Pages serves them.

    Why this exists:
    - dist/ is intentionally ignored by git (it's a build output).
    - GitHub Actions builds the site and publishes the dist/ artifact.
    - Therefore any asset you want everyone to see must be committed *somewhere else*
      (tracked) and then copied into dist/ during the build.

    Place files under:
      static/favicon.svg            (works in modern browsers)
      static/favicon.ico            (recommended fallback for broad compatibility)
      static/favicon.png            (optional)
      static/apple-touch-icon.png   (optional)

    This function copies any that exist into dist/static/.
    """

    src_static = project_dir / "static"
    dst_static = dist_dir / "static"
    dst_static.mkdir(parents=True, exist_ok=True)

    for name in ("favicon.ico", "favicon.png", "favicon.svg", "apple-touch-icon.png"):
        src = src_static / name
        if src.exists() and src.is_file() and src.stat().st_size > 0:
            shutil.copy2(src, dst_static / name)


def _take_latest(headlines: Iterable[Headline], n: int) -> list[Headline]:
    """Return the first n items from an iterable."""

    out: list[Headline] = []
    for h in headlines:
        out.append(h)
        if len(out) >= n:
            break
    return out


def _is_trump_item(h: Headline) -> bool:
    """
    Heuristic check for whether an item is "about Trump".

    We intentionally keep this simple and transparent: if "trump" appears in the
    title or summary, we treat it as a Trump-focused item.
    """

    hay = f"{h.title} {h.summary or ''}".lower()
    return "trump" in hay


def _select_with_trump_cap(items: Iterable[Headline], n: int, trump_cap: int = 3) -> list[Headline]:
    """
    Select up to n items, but allow at most trump_cap Trump-focused items.

    If we skip an item because it's beyond the Trump cap, we keep scanning until
    we fill n items (or run out of candidates).
    """

    out: list[Headline] = []
    trump_count = 0
    for it in items:
        if _is_trump_item(it):
            if trump_count >= trump_cap:
                continue
            trump_count += 1
        out.append(it)
        if len(out) >= n:
            break
    return out


def _collect_headlines() -> list[Headline]:
    """
    Collect headlines from the four requested sources.

    We aim for 16 blocks total (4 sources × 4 headlines each).
    If a feed has fewer items, we just take what is available.
    """

    # Number of top items to show per source.
    per_source_target = 6

    feeds: list[FeedSpec] = _feed_specs()

    all_items: list[Headline] = []
    session = _new_http_session()

    for spec in feeds:
        # For most sources, RSS is "latest in a section", which is often *not*
        # the same as the site's top-of-homepage story mix. If enabled, scrape
        # the homepage to better approximate "top headlines".
        if spec.use_homepage_scrape and spec.source != "The Economist":
            try:
                scraped = _collect_top_headlines_from_homepage(spec, per_source_target)
                if scraped:
                    all_items.extend(scraped)
                    continue
            except Exception as e:
                print(f"[WARN] Homepage scrape failed for {spec.source}: {e}")

        # Special-case Economist: prefer homepage scrape so we get "headline news"
        # (and usually images). We still *try* RSS first for speed, but fall back
        # to scrape if RSS fails OR if it returns items without images.
        if spec.source == "The Economist" and spec.use_homepage_scrape:
            rss_items: list[Headline] = []
            rss_worked = False
            for url in spec.urls:
                try:
                    parsed = _fetch_feed(url)
                    rss_worked = True
                except Exception as e:
                    print(f"[WARN] Economist RSS fetch failed ({url}): {e}")
                    continue

                for entry in parsed.entries or []:
                    title = (entry.get("title") or "").strip()
                    link = (entry.get("link") or "").strip()
                    if not title or not link:
                        continue
                    rss_items.append(
                        Headline(
                            source=spec.source,
                            title=title,
                            link=link,
                            summary=_extract_entry_summary(entry),
                            image_url=_extract_image_url(entry),
                        )
                    )

                # If this RSS gave us enough items, stop trying more endpoints.
                if len(rss_items) >= per_source_target:
                    break

            rss_items = _take_latest(_dedupe_by_link(rss_items), per_source_target)
            rss_items_with_images = sum(1 for it in rss_items if it.image_url)

            # If RSS has no images (common for the Economist), scrape the homepage.
            if (not rss_worked) or rss_items_with_images == 0:
                try:
                    scraped = _collect_economist_headlines_from_homepage(per_source_target)
                    if scraped:
                        all_items.extend(scraped)
                        continue
                except Exception as e:
                    print(f"[WARN] Economist homepage scrape failed: {e}")

            # Otherwise, keep the RSS items.
            all_items.extend(rss_items)
            continue

        # Economist, but using RSS endpoints: we still enrich missing images by
        # pulling og:image from the article page (best-effort).
        if spec.source == "The Economist" and not spec.use_homepage_scrape:
            rss_items: list[Headline] = []
            for url in spec.urls:
                try:
                    parsed = _fetch_feed(url)
                except Exception as e:
                    print(f"[WARN] Economist RSS fetch failed ({url}): {e}")
                    continue

                for entry in parsed.entries or []:
                    title = (entry.get("title") or "").strip()
                    link = (entry.get("link") or "").strip()
                    if not title or not link:
                        continue
                    rss_items.append(
                        Headline(
                            source=spec.source,
                            title=title,
                            link=link,
                            summary=_extract_entry_summary(entry),
                            image_url=_extract_image_url(entry),
                        )
                    )

                if len(rss_items) >= per_source_target:
                    break

            rss_items = _dedupe_by_link(rss_items)
            rss_items = _select_with_trump_cap(rss_items, per_source_target, trump_cap=3)

            enriched: list[Headline] = []
            for it in rss_items:
                if it.image_url:
                    enriched.append(it)
                    continue

                og = _extract_og_image(it.link)
                enriched.append(Headline(**{**it.__dict__, "image_url": og}))

            all_items.extend(enriched)
            continue

        # Default path: RSS/Atom sources.
        parsed: Optional[feedparser.FeedParserDict] = None
        last_err: Optional[Exception] = None
        for url in spec.urls:
            try:
                parsed = _fetch_feed(url)
                last_err = None
                break
            except Exception as e:
                last_err = e

        if parsed is None:
            print(f"[WARN] Could not fetch feed for {spec.source}. Last error: {last_err}")
            continue

        items: list[Headline] = []
        for entry in parsed.entries or []:
            title = (entry.get("title") or "").strip()
            link = (entry.get("link") or "").strip()
            if not title or not link:
                continue

            image_url = _extract_image_url(entry)
            summary = _extract_entry_summary(entry)

            # Straits Times: their RSS does not include media tags at all, but the
            # article pages do include og:image, and they are fetchable (no CF block),
            # so we enrich missing images from the article HTML.
            if spec.source == "The Straits Times" and not image_url:
                image_url = _extract_og_image(link)

            items.append(Headline(source=spec.source, title=title, link=link, summary=summary, image_url=image_url))

        items = _dedupe_by_link(items)
        all_items.extend(_select_with_trump_cap(items, per_source_target, trump_cap=3))

    # Google News RSS items often point at `news.google.com/rss/articles/...` wrappers.
    # Those wrappers hide the publisher URL and commonly yield unusable/generic preview images.
    # After we've assembled all rows, rewrite those wrappers to publisher URLs in one pass.
    try:
        all_items = _rewrite_google_news_links(session, all_items)
    except Exception as e:
        print(f"[WARN] Google News link rewriting failed: {e}")

    # If one feed had too few items, we may have <16. That's okay.
    return all_items


def _render_html(
    cards: list[Headline],
    built_at: datetime,
    weather: Optional[WeatherSummary],
    source_icon_paths: dict[str, str],
) -> str:
    """
    Produce the full HTML page as a string.

    The HTML is intentionally self-contained:
    - CSS is embedded in the page
    - Images are referenced as local paths under dist/static/images/
    """

    def esc(s: str) -> str:
        return html.escape(s, quote=True)

    # We render the page as 4 rows (one per source). Each row starts with a
    # vertical banner, followed by up to 4 cards for that source.
    feed_specs_in_order = _feed_specs()
    sources_in_order = [s.source for s in feed_specs_in_order]
    homepage_for_source = {s.source: s.homepage_url for s in feed_specs_in_order}

    by_source: dict[str, list[Headline]] = {s: [] for s in sources_in_order}
    for c in cards:
        by_source.setdefault(c.source, []).append(c)

    def card_html(c: Headline, idx_in_source: int) -> str:
        is_text_only = c.source in _TEXT_ONLY_SOURCES

        has_image = (not is_text_only) and bool(c.image_path)
        img_html = f'<img class="thumb" src="{esc(c.image_path)}" alt=""/>' if has_image else ""
        summary_html = f'<div class="summary">{esc(c.summary)}</div>' if c.summary else ""

        media_html = ""
        card_class = "card"
        if idx_in_source == 0:
            card_class += " featured"
        if is_text_only or (not has_image):
            # Keep "featured" for the top story even when it's text-only.
            card_class = "card noMedia" + (" featured" if idx_in_source == 0 else "")
        else:
            media_html = f'<div class="media">{img_html}</div>'

        return (
            textwrap.dedent(
                f"""
                <a class="{card_class}" href="{esc(c.link)}" target="_blank" rel="noopener noreferrer">
                  {media_html}
                  <div class="meta">
                    <div class="title">{esc(c.title)}</div>
                    {summary_html}
                  </div>
                </a>
                """
            ).strip()
        )

    row_parts: list[str] = []
    for source in sources_in_order:
        region = _REGION_BY_SOURCE.get(source, "Int'l")
        cards_for_source = _take_latest(by_source.get(source, []), 6)
        if cards_for_source:
            cards_html = "\n".join(card_html(c, i) for i, c in enumerate(cards_for_source))
        else:
            # If a source is blocked (403/401) or their feed structure changes,
            # we still render the row so the user can see the source exists.
            cards_html = '<div class="empty">No headlines available right now.</div>'
        homepage = homepage_for_source.get(source, "#")
        icon_path = source_icon_paths.get(source)
        icon_html = f'<img class="bicon" src="{esc(icon_path)}" alt=""/>' if icon_path else ""
        row_parts.append(
            textwrap.dedent(
                f"""
                <section class="row" data-region="{esc(region)}" data-source="{esc(source)}">
                  <a class="banner" data-source="{esc(source)}" href="{esc(homepage)}" target="_blank" rel="noopener noreferrer">
                    {icon_html}
                    <div class="bannerText">{esc(source)}</div>
                  </a>
                  <div class="rowCards">
                    {cards_html}
                  </div>
                </section>
                """
            ).strip()
        )

    rows_html = "\n".join(row_parts)

    # "Top News" view: the five most widely reported topics across all sources.
    # These panels render as their own sections and are shown when the user
    # clicks the "Top News" pill.
    top_topics = _compute_top_topics(cards, k=5)
    top_parts: list[str] = []
    topic_nav_links: list[str] = []
    for topic_idx, topic in enumerate(top_topics):
        topic_id = f"topTopic{topic_idx + 1}"
        topic_nav_links.append(f'<a class="topicNavLink" href="#{esc(topic_id)}">{esc(topic.title)}</a>')
        items_html: list[str] = []
        for it in topic.items:
            has_img = bool(it.image_path) and (it.source not in _TEXT_ONLY_SOURCES)
            img = f'<div class="tmedia"><img class="tthumb" src="{esc(it.image_path)}" alt=""/></div>' if has_img else ""
            # Show the feed-provided subheader/summary in Top News cards when available.
            # Keep it short via CSS line-clamp so panel heights remain balanced.
            summary_html = f'<div class="tsummary">{esc(it.summary)}</div>' if it.summary else ""
            items_html.append(
                textwrap.dedent(
                    f"""
                    <a class="topicItem{' noMedia' if not has_img else ''}" href="{esc(it.link)}" target="_blank" rel="noopener noreferrer">
                      {img}
                      <div class="tmeta">
                        <div class="ttitle">{esc(it.title)}</div>
                        {summary_html}
                        <div class="tsource"><strong>{esc(it.source)}</strong></div>
                      </div>
                    </a>
                    """
                ).strip()
            )

        # Join outside the f-string: f-string `{...}` parts cannot contain `\n` literals.
        items_block = "\n".join(items_html)
        panel_class = "topicPanel topicPanelAlt" if (topic_idx % 2 == 1) else "topicPanel"
        top_parts.append(
            textwrap.dedent(
                f"""
                <section class="{panel_class}" id="{esc(topic_id)}" data-region="Top">
                  <div class="topicHeader">{esc(topic.title)}</div>
                  <div class="topicItems">
                    {items_block}
                  </div>
                </section>
                """
            ).strip()
        )

    topic_nav_html = ""
    if topic_nav_links:
        topic_nav_html = (
            '<section class="topicNav" data-region="Top">'
            + '<div class="topicNavInner">'
            + " ".join(topic_nav_links)
            + "</div></section>"
        )

    top_news_html = topic_nav_html + "\n" + "\n".join(top_parts)
    built_str = built_at.strftime("%Y-%m-%d %H:%M")
    # Example: "Sunday, April 26 2026"
    date_str = f"{built_at.strftime('%A')}, {built_at.strftime('%B')} {built_at.day} {built_at.year}"

    # Weather widget is rendered as a single line. Location is clickable and can be
    # changed in the browser (saved in localStorage). We still render server-fetched
    # weather as the initial values so the page looks good even without JS.
    loc = _default_location()
    weather_html = ""
    if weather is not None:
        rain = f"{weather.rain_probability_pct}%" if weather.rain_probability_pct is not None else "—"
        desc = _weather_brief_description(weather.weather_code)
        wcat = _weather_category_from_code(weather.weather_code)
        # Link to a human-friendly forecast webpage (not the raw API endpoint).
        # MeteoBlue: prefer named slug URLs from their public search API so the forecast
        # page title matches the place (coordinate paths alone are often mislabeled).
        forecast_href = _meteoblue_named_week_url(loc.latitude, loc.longitude, loc.name)
        display_city = (loc.name.split(",")[0].strip() if loc.name else "")
        cur_class = "tempHot" if weather.current_f >= 95 else ("tempCold" if weather.current_f <= 32 else "")
        high_class = "tempHot" if weather.high_f >= 95 else ("tempCold" if weather.high_f <= 32 else "")
        low_class = "tempHot" if weather.low_f >= 95 else ("tempCold" if weather.low_f <= 32 else "")
        weather_html = textwrap.dedent(
            f"""
            <div class="weather" title="Weather (Open-Meteo)" data-wcat="{html.escape(wcat)}">
              <div class="wrow">
                <div class="wleft">
                  {_weather_icon_svg(weather.weather_code)}
                  <span class="wtemp {cur_class}" id="wtemp">{weather.current_f:.0f}°F</span>
                </div>

                <div class="wright">
                  <div class="wline wtop">
                    <button class="wloc" id="wlocBtn" type="button"
                            data-lat="{loc.latitude}" data-lon="{loc.longitude}"
                            data-place="{html.escape(loc.name or "", quote=True)}">
                      {html.escape(display_city or loc.name)}
                    </button>
                    <span class="wdesc" id="wdesc">{html.escape(desc)}</span>
                  </div>

                  <div class="wline wbottom">
                    <a class="whilo" id="whiloLink" href="{html.escape(forecast_href)}" target="_blank" rel="noopener noreferrer"
                       title="Open forecast"
                       data-lat="{loc.latitude}" data-lon="{loc.longitude}"
                       data-place="{html.escape(loc.name or "", quote=True)}">
                      <span id="whilo">
                        H <span class="{high_class}" id="whigh">{weather.high_f:.0f}°</span>
                        / L <span class="{low_class}" id="wlow">{weather.low_f:.0f}°</span>
                      </span>
                    </a>
                    <span class="wsep">•</span>
                    <span class="wrain" id="wrain">Rain {html.escape(rain)}</span>
                  </div>
                </div>
              </div>
            </div>
            """
        ).strip()

    # Favicons / site icons:
    # Only reference files that exist in the repo's tracked `static/` folder,
    # because `build.py` copies those into `dist/static/` during the build.
    project_dir = Path(__file__).resolve().parent
    static_dir = project_dir / "static"
    favicon_links: list[str] = []
    if (static_dir / "favicon.svg").exists():
        favicon_links.append('<link rel="icon" href="static/favicon.svg" type="image/svg+xml" />')
    if (static_dir / "favicon.png").exists():
        favicon_links.append('<link rel="icon" href="static/favicon.png" type="image/png" />')
    if (static_dir / "favicon.ico").exists():
        favicon_links.append('<link rel="icon" href="static/favicon.ico" sizes="any" />')
    if (static_dir / "apple-touch-icon.png").exists():
        favicon_links.append('<link rel="apple-touch-icon" href="static/apple-touch-icon.png" />')

    favicon_links_html = "\n            ".join(favicon_links)

    return textwrap.dedent(
        f"""
        <!doctype html>
        <html lang="en">
          <head>
            <meta charset="utf-8" />
            <meta name="viewport" content="width=device-width, initial-scale=1" />
            {favicon_links_html}
            <title>Daily World News</title>
            <style>
              :root {{
                --bg: #0b0f17;
                --panel: rgba(255,255,255,0.06);
                --panel2: rgba(255,255,255,0.09);
                --text: rgba(255,255,255,0.92);
                --muted: rgba(255,255,255,0.70);
                /* Slightly stronger borders for outlines */
                --border: rgba(255,255,255,0.18);
                --shadow: 0 10px 30px rgba(0,0,0,0.35);
              }}

              * {{ box-sizing: border-box; }}
              body {{
                margin: 0;
                font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif;
                background: radial-gradient(1200px 600px at 15% 0%, #1a2a55 0%, transparent 60%),
                            radial-gradient(1000px 500px at 85% 10%, #2a1649 0%, transparent 55%),
                            var(--bg);
                color: var(--text);
              }}

              .wrap {{
                max-width: 1280px;
                margin: 0 auto;
                padding: 28px 18px 40px;
              }}

              header {{
                display: flex;
                gap: 14px;
                align-items: flex-start;
                justify-content: space-between;
                margin-bottom: 18px;
              }}

              .headerLeft {{
                display: grid;
                gap: 10px;
                min-width: 0;
              }}

              .headerRight {{
                display: flex;
                gap: 14px;
                align-items: center;
              }}

              h1 {{
                margin: 0;
                font-size: 28px;
                letter-spacing: 0.2px;
                white-space: nowrap;
              }}

              /* Give the title+controls more horizontal room on small screens */
              @media (max-width: 980px) {{
                header {{
                  flex-wrap: wrap;
                }}
                .headerRight {{
                  width: 100%;
                  justify-content: flex-start;
                }}
              }}
              .regionBar {{
                display: flex;
                gap: 6px;
                flex-wrap: nowrap;
                overflow-x: hidden;
                /* Gold frame on both region strips (header + footer) so they stand apart from news rows. */
                padding: 7px 9px;
                border-radius: 12px;
                border: 1px solid rgba(201, 162, 39, 0.5);
                background: rgba(201, 162, 39, 0.04);
                box-shadow:
                  0 0 0 1px rgba(255, 224, 130, 0.12) inset,
                  0 1px 10px rgba(201, 162, 39, 0.08);
              }}

              .regionBtn {{
                appearance: none;
                border: 1px solid var(--border);
                background: rgba(255,255,255,0.06);
                color: rgba(255,255,255,0.86);
                padding: 5px 7px;
                border-radius: 10px;
                font-size: 11px;
                cursor: pointer;
                transition: transform 120ms ease, border-color 120ms ease, background 120ms ease;
                display: inline-flex;
                gap: 6px;
                align-items: center;
                user-select: none;
              }}

              .regionBtn:hover {{
                transform: translateY(-1px);
                border-color: rgba(255,255,255,0.22);
                background: rgba(255,255,255,0.09);
              }}

              /* Active region: strong golden frame so it stands out from other chips and from the bar chrome. */
              .regionBtn[aria-pressed="true"] {{
                border-color: #c9a227;
                background: linear-gradient(
                  180deg,
                  rgba(212, 175, 55, 0.22),
                  rgba(201, 162, 39, 0.12)
                );
                color: rgba(255, 250, 235, 0.96);
                box-shadow:
                  0 0 0 2px rgba(212, 175, 55, 0.55),
                  0 0 16px rgba(255, 200, 60, 0.18),
                  inset 0 1px 0 rgba(255, 248, 220, 0.12);
              }}

              /*
                Responsive behavior for the region selector buttons:
                - On wide screens, the region code stays beside the flag (default inline-flex row).
                - On narrow screens (when the overall layout starts to reflow),
                  we stack the code under the flag for a cleaner, more compact pill.
              */
              @media (max-width: 560px) {{
                .regionBtn {{
                  flex-direction: column;
                  align-items: center;
                  gap: 4px;
                  padding: 6px 8px;
                }}
              }}

              .flag {{
                width: 16px;
                height: 12px;
                display: inline-block;
                flex: 0 0 auto;
              }}

              .flag svg {{
                width: 16px;
                height: 12px;
                display: block;
                border-radius: 2px;
                box-shadow: 0 0 0 1px rgba(255,255,255,0.10) inset;
              }}

              .date {{
                color: rgba(255,255,255,0.78);
                font-weight: 500;
                margin-left: 8px;
                font-size: 14px;
              }}

              .built {{
                color: var(--muted);
                font-size: 12px;
              }}

              .weather {{
                display: flex;
                gap: 10px;
                align-items: flex-start;
                padding: 8px 10px;
                border-radius: 12px;
                border: 1px solid var(--border);
                background: rgba(255,255,255,0.06);
              }}

              .wicon {{
                width: 30px;
                height: 30px;
                color: rgba(255,255,255,0.92);
                flex: 0 0 auto;
              }}

              .wrow {{
                display: flex;
                gap: 10px;
                align-items: center;
              }}

              .wleft {{
                display: grid;
                justify-items: center;
                gap: 2px;
                flex: 0 0 auto;
              }}

              .wright {{
                display: grid;
                gap: 4px;
                min-width: 0;
              }}

              .wline {{
                display: flex;
                align-items: baseline;
                gap: 8px;
                white-space: nowrap;
              }}

              .wtop {{
                align-items: center;
              }}

              .wbottom {{
                color: rgba(255,255,255,0.82);
                font-size: 13px;
              }}

              .wloc {{
                appearance: none;
                border: 0;
                background: transparent;
                color: rgba(255,255,255,0.92);
                font-weight: 700;
                padding: 0;
                margin: 0;
                cursor: pointer;
                text-decoration: underline;
                text-decoration-color: rgba(255,255,255,0.35);
                text-underline-offset: 3px;
                font-size: 12px;
                font-weight: 650;
              }}

              .wloc:hover {{
                text-decoration-color: rgba(255,255,255,0.70);
              }}

              .wtemp {{
                font-weight: 700;
                letter-spacing: 0.2px;
                font-size: 12px;
              }}

              .wdesc {{
                color: rgba(255,255,255,0.86);
                font-size: 14px;
                font-weight: 600;
                max-width: 220px;
                overflow: hidden;
                text-overflow: ellipsis;
              }}

              /* Weather icon theming by "predominant" category */
              .weather[data-wcat="sun"] .wicon {{ color: rgba(253, 224, 71, 0.95); }}   /* pale yellow */
              .weather[data-wcat="rain"] .wicon {{ color: rgba(147, 197, 253, 0.95); }} /* light blue */
              .weather[data-wcat="snow"] .wicon {{ color: rgba(249, 168, 212, 0.95); }} /* pale pink */

              /* Temperature threshold highlighting */
              .tempHot {{ color: rgba(252, 165, 165, 0.95) !important; }}   /* light red */
              .tempCold {{ color: rgba(249, 168, 212, 0.95) !important; }} /* light pink */

              .whilo {{
                color: rgba(255,255,255,0.82);
                font-size: 13px;
                text-decoration: underline;
                text-decoration-color: rgba(255,255,255,0.28);
                text-underline-offset: 3px;
              }}

              .whilo:hover {{
                text-decoration-color: rgba(255,255,255,0.65);
              }}

              .wrain {{
                color: rgba(255,255,255,0.82);
                font-size: 13px;
              }}

              .wsep {{
                color: rgba(255,255,255,0.35);
              }}

              .grid {{
                display: grid;
                gap: 14px;
              }}

              /* Top News (topic clusters) */
              .topicNav {{
                border-radius: 18px;
                border: 1px solid var(--border);
                background: linear-gradient(180deg, rgba(255,255,255,0.10), rgba(255,255,255,0.04));
                box-shadow: var(--shadow);
                padding: 12px 14px;
              }}

              .topicNavInner {{
                display: flex;
                flex-wrap: wrap;
                gap: 10px;
                align-items: center;
              }}

              .topicNavLink {{
                display: inline-flex;
                align-items: center;
                gap: 8px;
                padding: 8px 10px;
                border-radius: 999px;
                border: 1px solid rgba(255,255,255,0.14);
                background: rgba(255,255,255,0.06);
                color: rgba(255,255,255,0.90);
                text-decoration: none;
                font-size: 12px;
                font-weight: 650;
                letter-spacing: 0.2px;
                max-width: 100%;
              }}

              .topicNavLink:hover {{
                background: rgba(255,255,255,0.10);
                border-color: rgba(255,255,255,0.22);
              }}

              .topicNavLink:focus-visible {{
                outline: 2px solid rgba(255,255,255,0.35);
                outline-offset: 2px;
              }}

              .topicPanel {{
                border-radius: 18px;
                border: 1px solid var(--border);
                background: linear-gradient(180deg, rgba(255,255,255,0.10), rgba(255,255,255,0.04));
                box-shadow: var(--shadow);
                padding: 14px;
                display: grid;
                gap: 12px;
              }}

              /* Give the second Top News panel a distinct tint. */
              .topicPanelAlt {{
                background: linear-gradient(180deg, rgba(120,170,255,0.22), rgba(120,170,255,0.08));
              }}

              .topicHeader {{
                font-size: 16px;
                font-weight: 750;
                letter-spacing: 0.2px;
                color: rgba(255,255,255,0.92);
                white-space: nowrap;
                overflow: hidden;
                text-overflow: ellipsis;
              }}

              .topicItems {{
                display: grid;
                grid-template-columns: repeat(2, minmax(0, 1fr));
                gap: 10px;
                width: 100%;
              }}

              @media (max-width: 900px) {{
                .topicItems {{ grid-template-columns: 1fr; }}
              }}

              .topicItem {{
                display: grid;
                grid-template-columns: 84px 1fr;
                gap: 10px;
                align-items: stretch;
                text-decoration: none;
                color: inherit;
                border-radius: 12px;
                border: 1px solid rgba(255,255,255,0.16);
                background: rgba(0,0,0,0.18);
                overflow: hidden;
              }}

              .topicItem.noMedia {{
                grid-template-columns: 1fr;
              }}

              .tmedia {{
                background: rgba(0,0,0,0.22);
              }}

              .tthumb {{
                width: 100%;
                height: 100%;
                object-fit: cover;
                display: block;
              }}

              .tmeta {{
                padding: 10px 10px 10px 0;
                display: grid;
                gap: 6px;
                align-content: start;
              }}

              .ttitle {{
                font-size: 13px;
                line-height: 1.25;
                display: -webkit-box;
                -webkit-line-clamp: 3;
                -webkit-box-orient: vertical;
                overflow: hidden;
              }}

              .tsummary {{
                font-size: 12px;
                line-height: 1.3;
                color: rgba(255,255,255,0.74);
                display: -webkit-box;
                -webkit-line-clamp: 2;
                -webkit-box-orient: vertical;
                overflow: hidden;
              }}

              .tsource {{
                font-size: 12px;
                color: rgba(255,255,255,0.82);
              }}

              /* Each source section is: banner (left) + cards (right) */
              .row {{
                display: grid;
                grid-template-columns: 34px 1fr;
                gap: 14px;
                position: relative;
                padding: 12px;
                border-radius: 18px;
              }}

              /* Extend the source color behind the entire row (behind cards) */
              .row::before {{
                content: "";
                position: absolute;
                inset: 0;
                border-radius: 18px;
                border: 1px solid var(--border);
                background: linear-gradient(180deg, rgba(255,255,255,0.10), rgba(255,255,255,0.04));
                box-shadow: var(--shadow);
                z-index: 0;
              }}

              .banner, .rowCards {{
                position: relative;
                z-index: 1;
              }}

              .rowCards {{
                display: grid;
                /* New layout: 3 columns with a wider featured column */
                grid-template-columns: minmax(0, 1.5fr) minmax(0, 1fr) minmax(0, 1fr);
                gap: 14px;
                grid-auto-flow: dense;
              }}

              /* Wide screens: show 5 items even though we fetch 6 (mobile uses all 6). */
              .rowCards > a.card:nth-child(6) {{
                display: none;
              }}

              @media (max-width: 1100px) {{
                .row {{ grid-template-columns: 34px 1fr; }}
                .rowCards {{ grid-template-columns: minmax(0, 1.5fr) minmax(0, 1fr) minmax(0, 1fr); }}
              }}

              /* Narrow screens: switch back to the older vertical card layout. */
              @media (max-width: 720px) {{
                .row {{ grid-template-columns: 32px 1fr; padding: 10px; border-radius: 16px; }}
                .row::before {{ border-radius: 16px; }}
                .rowCards {{ grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }}
                .rowCards > a.card:nth-child(6) {{ display: block; }}

                /* Drop subheader for height (and to match the previous mobile layout). */
                .summary {{ display: none !important; }}

                /* Vertical card: image on top, text below. */
                .card {{
                  grid-template-columns: 1fr !important;
                  grid-template-rows: 96px auto !important;
                  min-height: 205px !important;
                }}

                .card.noMedia {{
                  grid-template-rows: auto !important;
                  min-height: 160px !important;
                }}

                /* Featured should stop spanning rows on narrow screens. */
                .card.featured {{
                  grid-row: auto !important;
                  grid-template-columns: 1fr !important;
                  grid-template-rows: 128px auto !important;
                  min-height: 255px !important;
                }}

                /* On narrow screens, revert typography to the default (no "hero" fonts). */
                .card.featured .title {{
                  font-size: 14px !important;
                  line-height: 1.25 !important;
                }}

                .card.featured .summary {{
                  font-size: 12px !important;
                  -webkit-line-clamp: 6 !important;
                }}

                .meta {{ padding: 10px 10px 12px; }}
              }}

              .banner {{
                text-decoration: none;
                border-radius: 14px;
                border: 0;
                background: transparent;
                box-shadow: none;
                display: flex;
                flex-direction: column;
                align-items: center;
                justify-content: flex-start;
                overflow: hidden;
                position: relative;
                min-height: 240px;
                transition: transform 120ms ease, border-color 120ms ease;
                padding-top: 10px;
                gap: 10px;
              }}

              .banner:hover {{
                transform: translateY(-2px);
                border-color: rgba(255,255,255,0.22);
              }}

              .bicon {{
                width: 24px;
                height: 24px;
                object-fit: contain;
                filter: drop-shadow(0 6px 18px rgba(0,0,0,0.35));
                opacity: 0.95;
              }}

              /* Vertical label */
              .bannerText {{
                writing-mode: vertical-rl;
                transform: rotate(180deg);
                letter-spacing: 0.4px;
                font-size: 19px;
                font-weight: 600;
                color: rgba(255,255,255,0.88);
                padding: 10px 0;
              }}

              /*
                Per-source banner colors (logo-inspired, but dark/subdued).

                We keep these intentionally dark so white text stays readable.
                Each is a subtle gradient so the banner still matches the page style.
              */
              .row[data-source="The Economist"]::before {{ background: linear-gradient(180deg, rgba(255, 59, 48, 0.32), rgba(255,255,255,0.06)); }}
              .row[data-source="The Guardian"]::before {{ background: linear-gradient(180deg, rgba(0, 170, 255, 0.30), rgba(255,255,255,0.06)); }}
              .row[data-source="Der Spiegel"]::before {{ background: linear-gradient(180deg, rgba(255, 0, 0, 0.30), rgba(255,255,255,0.06)); }}
              .row[data-source="Deutsche Welle"]::before {{ background: linear-gradient(180deg, rgba(0, 120, 255, 0.30), rgba(255,255,255,0.06)); }}
              .row[data-source="The Straits Times"]::before {{ background: linear-gradient(180deg, rgba(220, 38, 38, 0.28), rgba(255,255,255,0.06)); }}
              .row[data-source="Associated Press"]::before {{ background: linear-gradient(180deg, rgba(239, 68, 68, 0.30), rgba(255,255,255,0.06)); }}
              .row[data-source="BBC News"]::before {{ background: linear-gradient(180deg, rgba(255,255,255,0.18), rgba(255,255,255,0.06)); }}
              .row[data-source="Al Jazeera"]::before {{ background: linear-gradient(180deg, rgba(234, 179, 8, 0.30), rgba(255,255,255,0.06)); }}
              .row[data-source="El País"]::before {{ background: linear-gradient(180deg, rgba(20, 184, 166, 0.30), rgba(255,255,255,0.06)); }}
              .row[data-source="Le Monde"]::before {{ background: linear-gradient(180deg, rgba(148, 163, 184, 0.24), rgba(255,255,255,0.06)); }}
              .row[data-source="France 24"]::before {{ background: linear-gradient(180deg, rgba(37, 99, 235, 0.30), rgba(255,255,255,0.06)); }}
              .row[data-source="South China Morning Post"]::before {{ background: linear-gradient(180deg, rgba(20, 184, 166, 0.28), rgba(255,255,255,0.06)); }}
              .row[data-source="The Jerusalem Post"]::before {{ background: linear-gradient(180deg, rgba(37, 99, 235, 0.28), rgba(255,255,255,0.06)); }}
              .row[data-source="Times of India"]::before {{ background: linear-gradient(180deg, rgba(245, 158, 11, 0.22), rgba(255,255,255,0.06)); }}
              .row[data-source="AllAfrica"]::before {{ background: linear-gradient(180deg, rgba(22, 163, 74, 0.24), rgba(255,255,255,0.06)); }}
              .row[data-source="EUobserver"]::before {{ background: linear-gradient(180deg, rgba(37, 99, 235, 0.24), rgba(255,255,255,0.06)); }}
              .row[data-source="Agence France-Presse"]::before {{ background: linear-gradient(180deg, rgba(56, 189, 248, 0.22), rgba(255,255,255,0.06)); }}
              .row[data-source="BBC World Service"]::before {{ background: linear-gradient(180deg, rgba(148, 163, 184, 0.24), rgba(255,255,255,0.06)); }}
              .row[data-source="The Diplomat"]::before {{ background: linear-gradient(180deg, rgba(59, 130, 246, 0.24), rgba(255,255,255,0.06)); }}
              .row[data-source="Financial Times"]::before {{ background: linear-gradient(180deg, rgba(255, 59, 48, 0.24), rgba(255,255,255,0.06)); }}
              .row[data-source="Reuters"]::before {{ background: linear-gradient(180deg, rgba(248, 113, 113, 0.22), rgba(255,255,255,0.06)); }}
              .row[data-source="USA Today"]::before {{ background: linear-gradient(180deg, rgba(14, 165, 233, 0.24), rgba(255,255,255,0.06)); }}
              .row[data-source="Vox"]::before {{ background: linear-gradient(180deg, rgba(251, 146, 60, 0.24), rgba(255,255,255,0.06)); }}
              .row[data-source="Politico"]::before {{ background: linear-gradient(180deg, rgba(59, 130, 246, 0.22), rgba(255,255,255,0.06)); }}
              .row[data-source="Axios"]::before {{ background: linear-gradient(180deg, rgba(34, 197, 94, 0.22), rgba(255,255,255,0.06)); }}
              .row[data-source="Fox News"]::before {{ background: linear-gradient(180deg, rgba(0, 51, 102, 0.34), rgba(200, 16, 46, 0.18)); }}
              .row[data-source="WESA"]::before {{ background: linear-gradient(180deg, rgba(236, 72, 153, 0.22), rgba(255,255,255,0.06)); }}
              .row[data-source="NEXTpittsburgh"]::before {{ background: linear-gradient(180deg, rgba(168, 85, 247, 0.22), rgba(255,255,255,0.06)); }}
              .row[data-source="Pgh City Paper"]::before {{ background: linear-gradient(180deg, rgba(245, 158, 11, 0.22), rgba(255,255,255,0.06)); }}
              .row[data-source="Pgh PublicSource"]::before {{ background: linear-gradient(180deg, rgba(20, 184, 166, 0.22), rgba(255,255,255,0.06)); }}
              .row[data-source="Trib|Live"]::before {{ background: linear-gradient(180deg, rgba(239, 68, 68, 0.22), rgba(255,255,255,0.06)); }}
              .row[data-source="The New York Times"]::before {{ background: linear-gradient(180deg, rgba(255,255,255,0.16), rgba(255,255,255,0.06)); }}
              .row[data-source="The Washington Post"]::before {{ background: linear-gradient(180deg, rgba(255,255,255,0.16), rgba(255,255,255,0.06)); }}
              .row[data-source="The Wall Street Journal"]::before {{ background: linear-gradient(180deg, rgba(148, 163, 184, 0.24), rgba(255,255,255,0.06)); }}
              .row[data-source="Süddeutsche Zeitung"]::before {{ background: linear-gradient(180deg, rgba(30, 64, 175, 0.26), rgba(255,255,255,0.06)); }}
              .row[data-source="Tagesschau"]::before {{ background: linear-gradient(180deg, rgba(30, 64, 175, 0.26), rgba(255,255,255,0.06)); }}
              .row[data-source="RNZ"]::before {{ background: linear-gradient(180deg, rgba(234, 88, 12, 0.24), rgba(255,255,255,0.06)); }}
              .row[data-source="NZZ"]::before {{ background: linear-gradient(180deg, rgba(255,255,255,0.18), rgba(255,255,255,0.06)); }}

              .card {{
                /* Horizontal card: image left, text right */
                display: grid;
                grid-template-columns: 110px 1fr;
                align-items: stretch;
                text-decoration: none;
                color: inherit;
                background: linear-gradient(180deg, var(--panel2), var(--panel));
                border: 1px solid var(--border);
                border-radius: 14px;
                overflow: hidden;
                box-shadow: var(--shadow);
                transition: transform 120ms ease, border-color 120ms ease, background 120ms ease;
                min-height: 118px;
              }}

              /* Featured item: double height, bigger typography */
              .card.featured {{
                grid-row: span 2;
                grid-template-columns: 160px 1fr;
                min-height: 250px;
              }}

              .card.featured .title {{
                font-size: 20px;
                line-height: 1.18;
              }}

              .card.featured .summary {{
                font-size: 14px;
                -webkit-line-clamp: 6;
              }}

              .card.noMedia {{
                grid-template-columns: 1fr;
                min-height: 96px;
              }}

              /* Featured text-only card: still double height + larger typography */
              .card.noMedia.featured {{
                grid-row: span 2;
                min-height: 250px;
              }}

              .card:hover {{
                transform: translateY(-2px);
                border-color: rgba(255,255,255,0.22);
                background: linear-gradient(180deg, rgba(255,255,255,0.11), rgba(255,255,255,0.06));
              }}

              .media {{
                background: rgba(0,0,0,0.25);
              }}

              .thumb {{
                width: 100%;
                height: 100%;
                object-fit: cover;
                display: block;
              }}

              .meta {{
                padding: 12px 12px 12px;
                display: grid;
                gap: 8px;
                align-content: start;
              }}

              .empty {{
                border-radius: 14px;
                border: 1px dashed rgba(255,255,255,0.22);
                background: rgba(255,255,255,0.04);
                color: rgba(255,255,255,0.70);
                padding: 16px;
                min-height: 240px;
                display: grid;
                place-items: center;
                text-align: center;
              }}

              .title {{
                font-size: 14px;
                line-height: 1.25;
              }}

              .summary {{
                color: rgba(255,255,255,0.74);
                font-size: 12px;
                line-height: 1.25;
                display: -webkit-box;
                -webkit-line-clamp: 3;
                -webkit-box-orient: vertical;
                overflow: hidden;
              }}
            </style>
          </head>
          <body>
            <div class="wrap">
              <header>
                <div class="headerLeft">
                  <h1>Daily World News <span class="date">{html.escape(date_str)}</span></h1>
                  <div class="regionBar" role="group" aria-label="Select news region">
                    <button class="regionBtn" type="button" data-region="Top" aria-pressed="false">
                      <span class="flag" aria-hidden="true">
                        <!-- globe icon -->
                        <svg viewBox="0 0 16 12" xmlns="http://www.w3.org/2000/svg">
                          <rect width="16" height="12" fill="#0B0F17"/>
                          <circle cx="8" cy="6" r="4.6" fill="#2563EB"/>
                          <path d="M6.1 4.6c.8-1 2.7-1.3 3.7-.6.7.5.6 1.3.1 1.8-.6.6-1.6.8-2.2.6-.6-.2-1.1.1-1.4-.4-.3-.4-.3-.8-.2-1.4z" fill="#22C55E"/>
                          <path d="M7.6 2.0c.8.4 1.3 1.7 1.2 2.8-.1 1.2-.6 2.2-1.2 2.2-.6 0-1.1-1-1.2-2.2-.1-1.1.4-2.4 1.2-2.8z" fill="none" stroke="rgba(255,255,255,0.45)" stroke-width="0.7"/>
                          <path d="M3.4 6h9.2" stroke="rgba(255,255,255,0.45)" stroke-width="0.7"/>
                        </svg>
                      </span>
                      <span>Top News</span>
                    </button>
                    <button class="regionBtn" type="button" data-region="UK" aria-pressed="false">
                      <span class="flag" aria-hidden="true">
                        <svg viewBox="0 0 16 12" xmlns="http://www.w3.org/2000/svg">
                          <rect width="16" height="12" fill="#0A2A66"/>
                          <!-- diagonals (white) -->
                          <path d="M0 0 L2.2 0 L16 8.6 L16 12 L13.8 12 L0 3.4 Z" fill="#FFFFFF" opacity="0.95"/>
                          <path d="M16 0 L13.8 0 L0 8.6 L0 12 L2.2 12 L16 3.4 Z" fill="#FFFFFF" opacity="0.95"/>
                          <!-- diagonals (red) -->
                          <path d="M0 0 L1.3 0 L16 9.2 L16 12 L14.7 12 L0 2.8 Z" fill="#C8102E" opacity="0.95"/>
                          <path d="M16 0 L14.7 0 L0 9.2 L0 12 L1.3 12 L16 2.8 Z" fill="#C8102E" opacity="0.95"/>
                          <!-- central cross (white then red) -->
                          <rect x="6.2" width="3.6" height="12" fill="#FFFFFF"/>
                          <rect y="4.2" width="16" height="3.6" fill="#FFFFFF"/>
                          <rect x="6.8" width="2.4" height="12" fill="#C8102E"/>
                          <rect y="4.8" width="16" height="2.4" fill="#C8102E"/>
                        </svg>
                      </span>
                      <span>UK</span>
                    </button>
                    <button class="regionBtn" type="button" data-region="DE" aria-pressed="false">
                      <span class="flag" aria-hidden="true">
                        <svg viewBox="0 0 16 12" xmlns="http://www.w3.org/2000/svg">
                          <rect width="16" height="4" y="0" fill="#000000"/>
                          <rect width="16" height="4" y="4" fill="#DD0000"/>
                          <rect width="16" height="4" y="8" fill="#FFCE00"/>
                        </svg>
                      </span>
                      <span>DE</span>
                    </button>
                    <button class="regionBtn" type="button" data-region="EU" aria-pressed="false">
                      <span class="flag" aria-hidden="true">
                        <svg viewBox="0 0 16 12" xmlns="http://www.w3.org/2000/svg">
                          <rect width="16" height="12" fill="#003399"/>
                          <!-- simplified EU emblem -->
                          <circle cx="8" cy="6" r="2.3" fill="none" stroke="#FFCC00" stroke-width="0.9" opacity="0.95"/>
                        </svg>
                      </span>
                      <span>EU</span>
                    </button>
                    <button class="regionBtn" type="button" data-region="Int'l" aria-pressed="false">
                      <span class="flag" aria-hidden="true">
                        <svg viewBox="0 0 16 12" xmlns="http://www.w3.org/2000/svg">
                          <rect width="16" height="12" fill="#5DADE2"/>
                          <!-- simplified UN emblem -->
                          <circle cx="8" cy="6" r="2.6" fill="none" stroke="#FFFFFF" stroke-width="0.9" opacity="0.95"/>
                          <path d="M6.3 6h3.4" stroke="#FFFFFF" stroke-width="0.8" opacity="0.95"/>
                          <path d="M8 3.6v4.8" stroke="#FFFFFF" stroke-width="0.6" opacity="0.7"/>
                        </svg>
                      </span>
                      <span>Int'l</span>
                    </button>
                    <button class="regionBtn" type="button" data-region="US" aria-pressed="false">
                      <span class="flag" aria-hidden="true">
                        <svg viewBox="0 0 16 12" xmlns="http://www.w3.org/2000/svg">
                          <rect width="16" height="12" fill="#FFFFFF"/>
                          <g fill="#B22234">
                            <rect y="0" width="16" height="1"/>
                            <rect y="2" width="16" height="1"/>
                            <rect y="4" width="16" height="1"/>
                            <rect y="6" width="16" height="1"/>
                            <rect y="8" width="16" height="1"/>
                            <rect y="10" width="16" height="1"/>
                          </g>
                          <rect width="7" height="6" fill="#3C3B6E"/>
                          <g fill="#FFFFFF" opacity="0.9">
                            <circle cx="1.1" cy="1.0" r="0.25"/><circle cx="2.3" cy="1.0" r="0.25"/><circle cx="3.5" cy="1.0" r="0.25"/><circle cx="4.7" cy="1.0" r="0.25"/><circle cx="5.9" cy="1.0" r="0.25"/>
                            <circle cx="1.7" cy="2.0" r="0.25"/><circle cx="2.9" cy="2.0" r="0.25"/><circle cx="4.1" cy="2.0" r="0.25"/><circle cx="5.3" cy="2.0" r="0.25"/>
                            <circle cx="1.1" cy="3.0" r="0.25"/><circle cx="2.3" cy="3.0" r="0.25"/><circle cx="3.5" cy="3.0" r="0.25"/><circle cx="4.7" cy="3.0" r="0.25"/><circle cx="5.9" cy="3.0" r="0.25"/>
                            <circle cx="1.7" cy="4.0" r="0.25"/><circle cx="2.9" cy="4.0" r="0.25"/><circle cx="4.1" cy="4.0" r="0.25"/><circle cx="5.3" cy="4.0" r="0.25"/>
                            <circle cx="1.1" cy="5.0" r="0.25"/><circle cx="2.3" cy="5.0" r="0.25"/><circle cx="3.5" cy="5.0" r="0.25"/><circle cx="4.7" cy="5.0" r="0.25"/><circle cx="5.9" cy="5.0" r="0.25"/>
                          </g>
                        </svg>
                      </span>
                      <span>US</span>
                    </button>
                    <button class="regionBtn" type="button" data-region="PGH" aria-pressed="false">
                      <span class="flag" aria-hidden="true">
                        <!-- simple Pittsburgh skyline icon -->
                        <svg viewBox="0 0 16 12" xmlns="http://www.w3.org/2000/svg">
                          <rect width="16" height="12" fill="#111827"/>
                          <rect y="9.2" width="16" height="2.8" fill="#0B1220"/>
                          <g fill="#94A3B8" opacity="0.95">
                            <rect x="1.2" y="5.2" width="2.0" height="4.0" rx="0.2"/>
                            <rect x="3.6" y="3.8" width="2.2" height="5.4" rx="0.2"/>
                            <rect x="6.2" y="4.6" width="1.8" height="4.6" rx="0.2"/>
                            <rect x="8.4" y="2.8" width="2.4" height="6.4" rx="0.2"/>
                            <rect x="11.2" y="4.2" width="1.6" height="5.0" rx="0.2"/>
                            <rect x="13.2" y="5.6" width="1.6" height="3.6" rx="0.2"/>
                          </g>
                          <path d="M0 9.2 C 4 8.4, 7 10.0, 10 9.2 C 12.2 8.6, 13.4 8.8, 16 9.2 L16 12 L0 12 Z" fill="#0F172A" opacity="0.9"/>
                        </svg>
                      </span>
                      <span>PGH</span>
                    </button>
                  </div>
                </div>
                <div class="headerRight">
                  {weather_html}
                  <div class="built">Built {html.escape(built_str)} (local time)</div>
                </div>
              </header>
              <main class="grid">
                {top_news_html}
                {rows_html}
              </main>

              <!-- Bottom region selector (mirrors the top row) -->
              <div class="regionBar" role="group" aria-label="Select news region (bottom)" style="margin-top: 16px;">
                <button class="regionBtn" type="button" data-region="Top" aria-pressed="false">
                  <span class="flag" aria-hidden="true">
                    <!-- globe icon -->
                    <svg viewBox="0 0 16 12" xmlns="http://www.w3.org/2000/svg">
                      <rect width="16" height="12" fill="#0B0F17"/>
                      <circle cx="8" cy="6" r="4.6" fill="#2563EB"/>
                      <path d="M6.1 4.6c.8-1 2.7-1.3 3.7-.6.7.5.6 1.3.1 1.8-.6.6-1.6.8-2.2.6-.6-.2-1.1.1-1.4-.4-.3-.4-.3-.8-.2-1.4z" fill="#22C55E"/>
                      <path d="M7.6 2.0c.8.4 1.3 1.7 1.2 2.8-.1 1.2-.6 2.2-1.2 2.2-.6 0-1.1-1-1.2-2.2-.1-1.1.4-2.4 1.2-2.8z" fill="none" stroke="rgba(255,255,255,0.45)" stroke-width="0.7"/>
                      <path d="M3.4 6h9.2" stroke="rgba(255,255,255,0.45)" stroke-width="0.7"/>
                    </svg>
                  </span>
                  <span>Top News</span>
                </button>
                <button class="regionBtn" type="button" data-region="UK" aria-pressed="false">
                  <span class="flag" aria-hidden="true">
                    <svg viewBox="0 0 16 12" xmlns="http://www.w3.org/2000/svg">
                      <rect width="16" height="12" fill="#0A2A66"/>
                      <path d="M0 0 L2.2 0 L16 8.6 L16 12 L13.8 12 L0 3.4 Z" fill="#FFFFFF" opacity="0.95"/>
                      <path d="M16 0 L13.8 0 L0 8.6 L0 12 L2.2 12 L16 3.4 Z" fill="#FFFFFF" opacity="0.95"/>
                      <path d="M0 0 L1.3 0 L16 9.2 L16 12 L14.7 12 L0 2.8 Z" fill="#C8102E" opacity="0.95"/>
                      <path d="M16 0 L14.7 0 L0 9.2 L0 12 L1.3 12 L16 2.8 Z" fill="#C8102E" opacity="0.95"/>
                      <rect x="6.2" width="3.6" height="12" fill="#FFFFFF"/>
                      <rect y="4.2" width="16" height="3.6" fill="#FFFFFF"/>
                      <rect x="6.8" width="2.4" height="12" fill="#C8102E"/>
                      <rect y="4.8" width="16" height="2.4" fill="#C8102E"/>
                    </svg>
                  </span>
                  <span>UK</span>
                </button>
                <button class="regionBtn" type="button" data-region="DE" aria-pressed="false">
                  <span class="flag" aria-hidden="true">
                    <svg viewBox="0 0 16 12" xmlns="http://www.w3.org/2000/svg">
                      <rect width="16" height="4" y="0" fill="#000000"/>
                      <rect width="16" height="4" y="4" fill="#DD0000"/>
                      <rect width="16" height="4" y="8" fill="#FFCE00"/>
                    </svg>
                  </span>
                  <span>DE</span>
                </button>
                <button class="regionBtn" type="button" data-region="EU" aria-pressed="false">
                  <span class="flag" aria-hidden="true">
                    <svg viewBox="0 0 16 12" xmlns="http://www.w3.org/2000/svg">
                      <rect width="16" height="12" fill="#003399"/>
                      <circle cx="8" cy="6" r="2.3" fill="none" stroke="#FFCC00" stroke-width="0.9" opacity="0.95"/>
                    </svg>
                  </span>
                  <span>EU</span>
                </button>
                <button class="regionBtn" type="button" data-region="Int'l" aria-pressed="false">
                  <span class="flag" aria-hidden="true">
                    <svg viewBox="0 0 16 12" xmlns="http://www.w3.org/2000/svg">
                      <rect width="16" height="12" fill="#5DADE2"/>
                      <circle cx="8" cy="6" r="2.6" fill="none" stroke="#FFFFFF" stroke-width="0.9" opacity="0.95"/>
                      <path d="M6.3 6h3.4" stroke="#FFFFFF" stroke-width="0.8" opacity="0.95"/>
                      <path d="M8 3.6v4.8" stroke="#FFFFFF" stroke-width="0.6" opacity="0.7"/>
                    </svg>
                  </span>
                  <span>Int'l</span>
                </button>
                <button class="regionBtn" type="button" data-region="US" aria-pressed="false">
                  <span class="flag" aria-hidden="true">
                    <svg viewBox="0 0 16 12" xmlns="http://www.w3.org/2000/svg">
                      <rect width="16" height="12" fill="#FFFFFF"/>
                      <g fill="#B22234">
                        <rect y="0" width="16" height="1"/>
                        <rect y="2" width="16" height="1"/>
                        <rect y="4" width="16" height="1"/>
                        <rect y="6" width="16" height="1"/>
                        <rect y="8" width="16" height="1"/>
                        <rect y="10" width="16" height="1"/>
                      </g>
                      <rect width="7" height="6" fill="#3C3B6E"/>
                      <g fill="#FFFFFF" opacity="0.9">
                        <circle cx="1.1" cy="1.0" r="0.25"/><circle cx="2.3" cy="1.0" r="0.25"/><circle cx="3.5" cy="1.0" r="0.25"/><circle cx="4.7" cy="1.0" r="0.25"/><circle cx="5.9" cy="1.0" r="0.25"/>
                        <circle cx="1.7" cy="2.0" r="0.25"/><circle cx="2.9" cy="2.0" r="0.25"/><circle cx="4.1" cy="2.0" r="0.25"/><circle cx="5.3" cy="2.0" r="0.25"/>
                        <circle cx="1.1" cy="3.0" r="0.25"/><circle cx="2.3" cy="3.0" r="0.25"/><circle cx="3.5" cy="3.0" r="0.25"/><circle cx="4.7" cy="3.0" r="0.25"/><circle cx="5.9" cy="3.0" r="0.25"/>
                        <circle cx="1.7" cy="4.0" r="0.25"/><circle cx="2.9" cy="4.0" r="0.25"/><circle cx="4.1" cy="4.0" r="0.25"/><circle cx="5.3" cy="4.0" r="0.25"/>
                        <circle cx="1.1" cy="5.0" r="0.25"/><circle cx="2.3" cy="5.0" r="0.25"/><circle cx="3.5" cy="5.0" r="0.25"/><circle cx="4.7" cy="5.0" r="0.25"/><circle cx="5.9" cy="5.0" r="0.25"/>
                      </g>
                    </svg>
                  </span>
                  <span>US</span>
                </button>
                <button class="regionBtn" type="button" data-region="PGH" aria-pressed="false">
                  <span class="flag" aria-hidden="true">
                    <svg viewBox="0 0 16 12" xmlns="http://www.w3.org/2000/svg">
                      <rect width="16" height="12" fill="#111827"/>
                      <rect y="9.2" width="16" height="2.8" fill="#0B1220"/>
                      <g fill="#94A3B8" opacity="0.95">
                        <rect x="1.2" y="5.2" width="2.0" height="4.0" rx="0.2"/>
                        <rect x="3.6" y="3.8" width="2.2" height="5.4" rx="0.2"/>
                        <rect x="6.2" y="4.6" width="1.8" height="4.6" rx="0.2"/>
                        <rect x="8.4" y="2.8" width="2.4" height="6.4" rx="0.2"/>
                        <rect x="11.2" y="4.2" width="1.6" height="5.0" rx="0.2"/>
                        <rect x="13.2" y="5.6" width="1.6" height="3.6" rx="0.2"/>
                      </g>
                      <path d="M0 9.2 C 4 8.4, 7 10.0, 10 9.2 C 12.2 8.6, 13.4 8.8, 16 9.2 L16 12 L0 12 Z" fill="#0F172A" opacity="0.9"/>
                    </svg>
                  </span>
                  <span>PGH</span>
                </button>
              </div>
            </div>

            <script>
              // Optional dynamic refresh:
              // If this page is served by server.py (http://127.0.0.1:8000/),
              // we can fetch /api/news and refresh the visible cards without
              // re-running build.py.
              (function () {{
                // Region filter (Top / UK / DE / EU / Int'l / US / PGH).
                (function () {{
                  const KEY = "newsOfTheDay.region";
                  const buttons = Array.from(document.querySelectorAll(".regionBtn"));

                  function apply(region) {{
                    for (const btn of buttons) {{
                      btn.setAttribute("aria-pressed", btn.dataset.region === region ? "true" : "false");
                    }}
                    // Show/hide ONLY the content sections inside <main>
                    // (topic panels + normal source rows). Do NOT touch the header pills,
                    // which also use data-region.
                    document.querySelectorAll("main [data-region]").forEach((el) => {{
                      const r = el.getAttribute("data-region");
                      el.style.display = (r === region) ? "" : "none";
                    }});
                  }}

                  const saved = localStorage.getItem(KEY);
                  const initial = saved || "Top";
                  apply(initial);

                  for (const btn of buttons) {{
                    btn.addEventListener("click", () => {{
                      const region = btn.dataset.region;
                      localStorage.setItem(KEY, region);
                      apply(region);
                    }});
                  }}
                }})();

                async function tryRefreshNews() {{
                  try {{
                    const res = await fetch("/api/news", {{ cache: "no-store" }});
                    if (!res.ok) return;
                    const data = await res.json();
                    if (!data || !Array.isArray(data.items)) return;

                    const textOnly = new Set((data.text_only_sources || []));
                    const bySource = new Map();
                    for (const it of data.items) {{
                      if (!bySource.has(it.source)) bySource.set(it.source, []);
                      bySource.get(it.source).push(it);
                    }}

                    // Replace each row's cards.
                    document.querySelectorAll("section.row").forEach((row) => {{
                      const banner = row.querySelector(".banner");
                      const src = banner?.getAttribute("data-source");
                      const container = row.querySelector(".rowCards");
                      if (!src || !container) return;

                      const items = (bySource.get(src) || []).slice(0, 6);
                      if (!items.length) return;

                      const isTextOnly = textOnly.has(src);
                      function esc(s) {{
                        return String(s || "")
                          .replaceAll("&", "&amp;")
                          .replaceAll("<", "&lt;")
                          .replaceAll(">", "&gt;");
                      }}

                      container.innerHTML = items.map((x, idx) => {{
                        const title = esc(x.title);
                        const summary = esc(x.summary);
                        const link = x.link || "#";

                        const hasImage = (!isTextOnly && x.image_url);
                        const media = hasImage
                          ? `<div class="media"><img class="thumb" src="${{x.image_url}}" alt=""/></div>`
                          : "";
                        let cls = (isTextOnly || !hasImage) ? "card noMedia" : "card";
                        if (idx === 0) cls += " featured";
                        const summaryBlock = summary ? `<div class="summary">${{summary}}</div>` : "";

                        return (
                          `<a class="${{cls}}" href="${{link}}" target="_blank" rel="noopener noreferrer">` +
                          media +
                          `<div class="meta">` +
                          `<div class="title">${{title}}</div>` +
                          summaryBlock +
                          `</div>` +
                          `</a>`
                        );
                      }}).join("\\n");
                    }});

                    // Re-apply the current region filter after refresh.
                    const savedRegion = localStorage.getItem("newsOfTheDay.region") || "Int'l";
                    document.querySelectorAll("section.row").forEach((row) => {{
                      const r = row.getAttribute("data-region");
                      row.style.display = (r === savedRegion) ? "" : "none";
                    }});
                  }} catch (_) {{
                    // Ignore: page still works as static snapshot.
                  }}
                }}

                // Only attempt if page is served from http(s) (not file://).
                if (location.protocol === "http:" || location.protocol === "https:") {{
                  tryRefreshNews();
                }}
              }})();

              // Client-side weather refresh + user-selected location.
              // This keeps the page static but lets the viewer change location.
              (function () {{
                const STORAGE_KEY = "newsOfTheDay.weatherLocation";

                // Match Python ``_default_location()`` if coordinates are ever missing (must stay in sync).
                const METEOBLUE_FALLBACK_LAT = 40.4406;
                const METEOBLUE_FALLBACK_LON = -79.9959;

                // ``Number("")`` is 0 in JS — never use raw ``Number`` for HTML dataset values.
                function parseCoord(v) {{
                  if (v === undefined || v === null) return NaN;
                  const s = String(v).trim();
                  if (s === "") return NaN;
                  const x = parseFloat(s);
                  return Number.isFinite(x) ? x : NaN;
                }}

                // Path-style week URL only — never use bare ``/weather/week`` (MeteoBlue defaults that to Heidelberg / IP locale).
                function meteoblueCoordWeekUrl(lat, lon) {{
                  let la = parseCoord(lat);
                  let lo = parseCoord(lon);
                  if (!Number.isFinite(la)) la = METEOBLUE_FALLBACK_LAT;
                  if (!Number.isFinite(lo)) lo = METEOBLUE_FALLBACK_LON;
                  const latPart = Math.abs(la).toFixed(3) + (la >= 0 ? "N" : "S");
                  const lonPart = Math.abs(lo).toFixed(3) + (lo >= 0 ? "E" : "W");
                  return "https://www.meteoblue.com/en/weather/week/" + latPart + lonPart + "0_UTC";
                }}

                function syncForecastCoordsToLink(link, lat, lon, place) {{
                  if (!link) return;
                  if (lat !== undefined && lat !== null && String(lat).trim() !== "") {{
                    link.dataset.lat = String(lat);
                  }}
                  if (lon !== undefined && lon !== null && String(lon).trim() !== "") {{
                    link.dataset.lon = String(lon);
                  }}
                  if (place !== undefined) link.dataset.place = String(place || "");
                }}

                function syncForecastLinkFromButton() {{
                  const btn = document.getElementById("wlocBtn");
                  const link = document.getElementById("whiloLink");
                  if (!btn || !link) return;
                  const lat = btn.dataset.lat;
                  const lon = btn.dataset.lon;
                  if (lat !== undefined && lat !== "" && lon !== undefined && lon !== "") {{
                    syncForecastCoordsToLink(link, lat, lon, btn.dataset.place || "");
                    link.href = meteoblueCoordWeekUrl(lat, lon);
                  }}
                }}

                // MeteoBlue exposes the same JSON their search box uses; we match by lat/lon.
                async function resolveMeteoblueNamedWeekUrl(lat, lon, placeQuery) {{
                  const fallback = meteoblueCoordWeekUrl(lat, lon);
                  const q = String(placeQuery || "").trim();
                  if (!q) return fallback;
                  try {{
                    const res = await fetch(
                      "https://www.meteoblue.com/en/server/search/query3?query=" + encodeURIComponent(q)
                    );
                    if (!res.ok) return fallback;
                    const data = await res.json();
                    const results = (data && data.results) || [];
                    const la = parseCoord(lat);
                    const lo = parseCoord(lon);
                    if (!Number.isFinite(la) || !Number.isFinite(lo)) return fallback;
                    let best = null;
                    let bestD = Infinity;
                    for (let i = 0; i < results.length; i++) {{
                      const r = results[i];
                      if (!r || !r.url) continue;
                      const rlat = parseCoord(r.lat);
                      const rlon = parseCoord(r.lon);
                      if (!Number.isFinite(rlat) || !Number.isFinite(rlon)) continue;
                      const dlat = rlat - la;
                      const dlon = rlon - lo;
                      const d = dlat * dlat + dlon * dlon;
                      if (d < bestD) {{ bestD = d; best = r; }}
                    }}
                    if (!best) return fallback;
                    if (bestD > 0.25 * 0.25) return fallback;
                    return "https://www.meteoblue.com/en/weather/week/" + best.url;
                  }} catch (_) {{
                    return fallback;
                  }}
                }}

                async function onForecastLinkClick(e) {{
                  e.preventDefault();
                  const linkEl = e.currentTarget;
                  const btn = document.getElementById("wlocBtn");
                  let lat = parseCoord(linkEl.dataset.lat);
                  let lon = parseCoord(linkEl.dataset.lon);
                  let place = linkEl.dataset.place || "";
                  if (!Number.isFinite(lat) || !Number.isFinite(lon)) {{
                    lat = parseCoord(btn && btn.dataset.lat);
                    lon = parseCoord(btn && btn.dataset.lon);
                    place = (btn && btn.dataset.place) || place;
                  }}
                  const url = await resolveMeteoblueNamedWeekUrl(lat, lon, place);
                  window.open(url, "_blank", "noopener,noreferrer");
                }}

                function scheduleNamedForecastHref() {{
                  const btn = document.getElementById("wlocBtn");
                  const link = document.getElementById("whiloLink");
                  if (!btn || !link) return;
                  const lat = btn.dataset.lat;
                  const lon = btn.dataset.lon;
                  const place = btn.dataset.place || "";
                  syncForecastCoordsToLink(link, lat, lon, place);
                  resolveMeteoblueNamedWeekUrl(lat, lon, place).then((u) => {{ link.href = u; }});
                }}

                function setText(id, text) {{
                  const el = document.getElementById(id);
                  if (el) el.textContent = text;
                }}

                function setIconSvg(svg) {{
                  const icon = document.querySelector(".weather .wicon");
                  if (!icon) return;
                  // Replace the existing SVG element.
                  const wrapper = document.createElement("div");
                  wrapper.innerHTML = svg;
                  const newSvg = wrapper.firstElementChild;
                  if (newSvg && newSvg.tagName.toLowerCase() === "svg") {{
                    icon.replaceWith(newSvg);
                  }}
                }}

                function iconForCode(code) {{
                  // We mirror the Python mapping so the icon updates with the new location.
                  let category = "cloud";
                  if (code === null || code === undefined) category = "cloud";
                  else if (code === 0) category = "sun";
                  else if (code === 1 || code === 2 || code === 3) category = "cloud";
                  else if (code === 45 || code === 48) category = "fog";
                  else if ([51,53,55,56,57,61,63,65,66,67,80,81,82].includes(code)) category = "rain";
                  else if ([71,73,75,77,85,86].includes(code)) category = "snow";
                  else if ([95,96,99].includes(code)) category = "storm";

                  const common = 'fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"';
                  if (category === "sun") {{
                    return `<svg class="wicon" viewBox="0 0 24 24" aria-hidden="true">
                      <circle cx="12" cy="12" r="4" ${{common}}/>
                      <path d="M12 2v3M12 19v3M2 12h3M19 12h3M4.5 4.5l2.1 2.1M17.4 17.4l2.1 2.1M19.5 4.5l-2.1 2.1M6.6 17.4l-2.1 2.1" ${{common}}/>
                    </svg>`;
                  }}
                  if (category === "fog") {{
                    return `<svg class="wicon" viewBox="0 0 24 24" aria-hidden="true">
                      <path d="M5 9h14M4 12h16M6 15h12M7 18h10" ${{common}}/>
                    </svg>`;
                  }}
                  if (category === "snow") {{
                    return `<svg class="wicon" viewBox="0 0 24 24" aria-hidden="true">
                      <path d="M7 14a5 5 0 0 1 9.7-1.6A4 4 0 1 1 17 20H8a4 4 0 0 1-1-6" ${{common}}/>
                      <path d="M9 21l.5-1.2M12 21l.5-1.2M15 21l.5-1.2" ${{common}}/>
                    </svg>`;
                  }}
                  if (category === "storm") {{
                    return `<svg class="wicon" viewBox="0 0 24 24" aria-hidden="true">
                      <path d="M7 14a5 5 0 0 1 9.7-1.6A4 4 0 1 1 17 20H8a4 4 0 0 1-1-6" ${{common}}/>
                      <path d="M13 13l-3 5h3l-2 4" ${{common}}/>
                    </svg>`;
                  }}
                  if (category === "rain") {{
                    return `<svg class="wicon" viewBox="0 0 24 24" aria-hidden="true">
                      <path d="M7 14a5 5 0 0 1 9.7-1.6A4 4 0 1 1 17 20H8a4 4 0 0 1-1-6" ${{common}}/>
                      <path d="M9 20l-1 2M13 20l-1 2M17 20l-1 2" ${{common}}/>
                    </svg>`;
                  }}
                  return `<svg class="wicon" viewBox="0 0 24 24" aria-hidden="true">
                    <path d="M7 14a5 5 0 0 1 9.7-1.6A4 4 0 1 1 17 20H8a4 4 0 0 1-1-6" ${{common}}/>
                  </svg>`;
                }}

                function descForCode(code) {{
                  if (code === null || code === undefined) return "Partly cloudy";
                  if (code === 0) return "Clear";
                  if (code === 1 || code === 2 || code === 3) return "Partly cloudy";
                  if (code === 45 || code === 48) return "Fog";
                  if ([51,53,55,56,57,61,63,65,66,67,80,81,82].includes(code)) return "Rain";
                  if ([71,73,75,77,85,86].includes(code)) return "Snow";
                  if ([95,96,99].includes(code)) return "Thunderstorms";
                  return "Partly cloudy";
                }}

                async function geocode(name) {{
                  const url = "https://geocoding-api.open-meteo.com/v1/search?count=1&language=en&format=json&name=" + encodeURIComponent(name);
                  const res = await fetch(url);
                  if (!res.ok) throw new Error("Geocoding failed");
                  const data = await res.json();
                  const r = data && data.results && data.results[0];
                  if (!r) throw new Error("No results");
                  const label = [r.name, r.admin1, r.country_code].filter(Boolean).join(", ");
                  return {{ name: label, latitude: r.latitude, longitude: r.longitude }};
                }}

                async function fetchWeather(lat, lon) {{
                  const url = "https://api.open-meteo.com/v1/forecast"
                    + `?latitude=${{lat}}&longitude=${{lon}}`
                    + "&current=temperature_2m,weather_code"
                    + "&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code"
                    + "&temperature_unit=fahrenheit"
                    + "&timezone=auto";
                  const res = await fetch(url);
                  if (!res.ok) throw new Error("Weather fetch failed");
                  return await res.json();
                }}

                function applyWeather(loc, data) {{
                  const current = data.current || {{}};
                  const daily = data.daily || {{}};
                  const curF = Math.round(current.temperature_2m);
                  const code = current.weather_code;
                  const high = Math.round((daily.temperature_2m_max || [])[0]);
                  const low = Math.round((daily.temperature_2m_min || [])[0]);
                  const rain = (daily.precipitation_probability_max || [])[0];

                  const btn = document.getElementById("wlocBtn");
                  const link = document.getElementById("whiloLink");
                  if (btn) {{
                    const city = String(loc.name || "").split(",")[0].trim() || String(loc.name || "");
                    btn.textContent = city;
                    btn.dataset.lat = String(loc.latitude);
                    btn.dataset.lon = String(loc.longitude);
                    btn.dataset.place = String(loc.name || "");
                  }}
                  if (link) {{
                    link.dataset.lat = String(loc.latitude);
                    link.dataset.lon = String(loc.longitude);
                    link.dataset.place = String(loc.name || "");
                  }}

                  const weatherBox = document.querySelector(".weather");
                  if (weatherBox) {{
                    // Mirror Python category mapping for icon theming.
                    let wcat = "cloud";
                    if (code === null || code === undefined) wcat = "cloud";
                    else if (code === 0) wcat = "sun";
                    else if (code === 1 || code === 2 || code === 3) wcat = "cloud";
                    else if (code === 45 || code === 48) wcat = "fog";
                    else if ([51,53,55,56,57,61,63,65,66,67,80,81,82].includes(code)) wcat = "rain";
                    else if ([71,73,75,77,85,86].includes(code)) wcat = "snow";
                    else if ([95,96,99].includes(code)) wcat = "storm";
                    weatherBox.dataset.wcat = wcat;
                  }}

                  syncForecastLinkFromButton();

                  function setTempClass(id, valueF) {{
                    const el = document.getElementById(id);
                    if (!el) return;
                    el.classList.remove("tempHot", "tempCold");
                    if (valueF >= 95) el.classList.add("tempHot");
                    else if (valueF <= 32) el.classList.add("tempCold");
                  }}

                  setText("wtemp", `${{curF}}°F`);
                  setTempClass("wtemp", curF);
                  setText("wdesc", descForCode(code));
                  setText("whigh", `${{high}}°`);
                  setText("wlow", `${{low}}°`);
                  setTempClass("whigh", high);
                  setTempClass("wlow", low);
                  setText("wrain", `Rain ${{rain !== undefined && rain !== null ? (rain + "%") : "—"}}`);
                  setIconSvg(iconForCode(code));
                }}

                async function refreshFromStoredLocation() {{
                  const raw = localStorage.getItem(STORAGE_KEY);
                  if (!raw) return;
                  let loc;
                  try {{
                    loc = JSON.parse(raw);
                  }} catch (_) {{
                    return;
                  }}
                  try {{
                    // Even if weather fetch fails (e.g. file:// pages block fetch),
                    // still update city + forecast link from storage so H/L matches the saved place.
                    const btn = document.getElementById("wlocBtn");
                    const link = document.getElementById("whiloLink");
                    if (btn) {{
                      const city = String(loc.name || "").split(",")[0].trim() || String(loc.name || "");
                      btn.textContent = city;
                      btn.dataset.lat = String(loc.latitude);
                      btn.dataset.lon = String(loc.longitude);
                      btn.dataset.place = String(loc.name || "");
                    }}
                    if (link) {{
                      link.dataset.lat = String(loc.latitude);
                      link.dataset.lon = String(loc.longitude);
                      link.dataset.place = String(loc.name || "");
                    }}
                    syncForecastLinkFromButton();
                    scheduleNamedForecastHref();
                  }} catch (_) {{}}

                  try {{
                    const data = await fetchWeather(loc.latitude, loc.longitude);
                    applyWeather(loc, data);
                    scheduleNamedForecastHref();
                  }} catch (_) {{
                    // Keep server-rendered numbers if the browser cannot reach Open-Meteo.
                  }}
                }}

                async function onChangeLocation() {{
                  const currentName = document.getElementById("wlocBtn")?.textContent || "";
                  const input = prompt("Enter a city (e.g., 'Pittsburgh', 'Berlin', 'Singapore'):", currentName.trim());
                  if (!input) return;
                  try {{
                    const loc = await geocode(input);
                    localStorage.setItem(STORAGE_KEY, JSON.stringify(loc));
                    // Update city + forecast link immediately (even if weather fetch fails).
                    const btn = document.getElementById("wlocBtn");
                    const link = document.getElementById("whiloLink");
                    if (btn) {{
                      const city = String(loc.name || "").split(",")[0].trim() || String(loc.name || "");
                      btn.textContent = city;
                      btn.dataset.lat = String(loc.latitude);
                      btn.dataset.lon = String(loc.longitude);
                      btn.dataset.place = String(loc.name || "");
                    }}
                    if (link) {{
                      link.dataset.lat = String(loc.latitude);
                      link.dataset.lon = String(loc.longitude);
                      link.dataset.place = String(loc.name || "");
                    }}
                    syncForecastLinkFromButton();

                    const data = await fetchWeather(loc.latitude, loc.longitude);
                    applyWeather(loc, data);
                    scheduleNamedForecastHref();
                  }} catch (e) {{
                    alert("Sorry — I couldn't find that location or fetch weather for it.");
                  }}
                }}

                const btn = document.getElementById("wlocBtn");
                if (btn) btn.addEventListener("click", onChangeLocation);
                const whiloLink = document.getElementById("whiloLink");
                if (whiloLink) whiloLink.addEventListener("click", onForecastLinkClick);
                refreshFromStoredLocation();
              }})();
            </script>
          </body>
        </html>
        """
    ).strip() + "\n"


def main() -> int:
    """
    Main entry point.

    If you want different feeds, adjust _collect_headlines().
    If you want a different layout, adjust _render_html().
    """

    project_dir = Path(__file__).resolve().parent
    dist_dir = project_dir / "dist"
    images_dir = dist_dir / "static" / "images"

    dist_dir.mkdir(parents=True, exist_ok=True)
    images_dir.mkdir(parents=True, exist_ok=True)

    # Copy git-tracked static assets (favicons, touch icon, etc.) into dist/.
    _copy_site_static_assets(project_dir, dist_dir)

    use_cache = "--use-cache" in sys.argv
    if use_cache:
        cached = _load_build_cache(project_dir)
        if cached:
            headlines_cached, weather_cached = cached
            hydrated_fast = _reconcile_cached_image_paths(dist_dir, list(headlines_cached))
            weather_fast = weather_cached
            if weather_fast is None:
                loc_fb = _default_location()
                weather_fast = _fetch_weather(loc_fb.latitude, loc_fb.longitude)
            source_icon_paths_fast = _download_source_icons(dist_dir, local_only=True)
            html_text = _render_html(
                hydrated_fast,
                built_at=datetime.now(),
                weather=weather_fast,
                source_icon_paths=source_icon_paths_fast,
            )
            out_html = dist_dir / "index.html"
            out_html.write_text(html_text, encoding="utf-8")
            print("[INFO] Rebuilt HTML from cache (--use-cache); feeds and image downloads were skipped.")
            print(f"Wrote {out_html}")
            expected = 6 * len(_feed_specs())
            print(f"Cards: {len(hydrated_fast)} (aim is {expected})")
            return 0
        print("[WARN] No usable build cache found; run a full build once, then retry. Doing a full build now.")

    headlines = _collect_headlines()
    loc = _default_location()
    weather = _fetch_weather(loc.latitude, loc.longitude)
    # Download favicons used in the left-side banners (best-effort).
    source_icon_paths = _download_source_icons(dist_dir)

    # Quick diagnostic: how many items per source, and whether image URLs exist.
    per_source_counts: dict[str, int] = {}
    per_source_with_image_url: dict[str, int] = {}
    for h in headlines:
        per_source_counts[h.source] = per_source_counts.get(h.source, 0) + 1
        if h.image_url:
            per_source_with_image_url[h.source] = per_source_with_image_url.get(h.source, 0) + 1

    print("[INFO] Items collected per source:", per_source_counts)
    print("[INFO] Items with image URLs per source:", per_source_with_image_url)

    headlines = _enrich_missing_media(headlines)

    # Download images (best-effort) and store local relative paths for HTML.
    placeholder_paths = _write_placeholder_images(dist_dir)
    hydrated: list[Headline] = []
    for h in headlines:
        # Sources the user wants as text-only: never use/download images.
        if h.source in _TEXT_ONLY_SOURCES:
            hydrated.append(Headline(**{**h.__dict__, "image_url": None, "image_path": None}))
            continue

        if not h.image_url:
            # If we have a placeholder for this source, use it as a local image.
            ph = placeholder_paths.get(h.source)
            if ph:
                hydrated.append(Headline(**{**h.__dict__, "image_path": ph}))
            else:
                hydrated.append(h)
            continue

        # If the "image" is just a generic Google placeholder (common on Google News RSS),
        # do not download/use it. It's better to show no image (or a source placeholder)
        # than a misleading Google thumbnail.
        img_url_str = str(h.image_url or "")
        is_google_placeholder_img = (
            ("lh3.googleusercontent.com" in img_url_str)
            or ("googleusercontent.com" in img_url_str)
            or ("gstatic.com/images/branding" in img_url_str)
        )
        if is_google_placeholder_img:
            ph = placeholder_paths.get(h.source)
            if ph:
                hydrated.append(Headline(**{**h.__dict__, "image_url": None, "image_path": ph}))
            else:
                hydrated.append(Headline(**{**h.__dict__, "image_url": None, "image_path": None}))
            continue

        filename = _safe_filename_from_url(h.image_url)
        out_path = images_dir / filename
        ok = _download_image(h.image_url, out_path, referer=h.link)
        if ok:
            rel = f"static/images/{filename}"
            hydrated.append(Headline(**{**h.__dict__, "image_path": rel}))
        else:
            # If download fails but a placeholder exists, use it.
            ph = placeholder_paths.get(h.source)
            hydrated.append(Headline(**{**h.__dict__, "image_path": ph} if ph else h.__dict__))

    html_text = _render_html(hydrated, built_at=datetime.now(), weather=weather, source_icon_paths=source_icon_paths)
    out_html = dist_dir / "index.html"
    out_html.write_text(html_text, encoding="utf-8")

    print(f"Wrote {out_html}")
    # Expected card count is (sources * per-source target). If some sources are
    # temporarily blocked or return too few items, the actual count can be lower.
    expected = 6 * len(_feed_specs())
    print(f"Cards: {len(hydrated)} (aim is {expected})")
    _save_build_cache(project_dir, hydrated, weather)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

