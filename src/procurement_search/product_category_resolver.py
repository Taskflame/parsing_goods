"""Resolve product category pages into candidate product-detail links.

Search engines often return a category URL before a concrete product card.
This module does a cheap local pass over category HTML: collect same-domain
links, rank them by product identity signals, and return them as normal
Candidate objects for the existing dedup/scoring pipeline.
"""

from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from procurement_search.models import Candidate, SearchIntent
from procurement_search.sources.base import fetch_url

_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_DETAIL_URL_MARKERS = (
    "/product/",
    "/products/",
    "/tovar/",
    "/item/",
    "/shop/",
    "/catalog/product",
)
_CATEGORY_URL_MARKERS = ("/catalog/", "/category/", "/categories/", "/produkciya/")


def resolve_product_category_links(
    category: Candidate,
    intent: SearchIntent,
    clean_query_text: str,
    *,
    top_k: int = 5,
    timeout: float = 10.0,
    delay: float = 0.2,
    user_agent: str = _DEFAULT_UA,
    respect_robots: bool = True,
    session: requests.Session | None = None,
) -> list[Candidate]:
    """Return likely product-card links found on a product category page."""
    category_url = category.website or category.source_url
    parsed_category = urlparse(category_url)
    if not parsed_category.scheme or not parsed_category.netloc:
        return []

    html = fetch_url(
        category_url,
        user_agent=user_agent,
        timeout=timeout,
        delay=delay,
        respect_robots=respect_robots,
        session=session,
    )
    if not html:
        return []

    soup = BeautifulSoup(html, "lxml")
    ranked: list[tuple[int, str, str, str | None]] = []
    seen_urls: set[str] = set()

    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href") or "").strip()
        absolute_url = _normalize_internal_url(category_url, href)
        if not absolute_url or absolute_url in seen_urls:
            continue
        if _domain(absolute_url) != _domain(category_url):
            continue
        seen_urls.add(absolute_url)

        anchor_text = anchor.get_text(" ", strip=True)
        context_text = _nearby_text(anchor)
        haystack = " ".join(part for part in (anchor_text, context_text, absolute_url) if part)
        score = _link_score(haystack, absolute_url, intent, clean_query_text)
        if score <= 0:
            continue
        ranked.append((score, absolute_url, anchor_text, context_text))

    ranked.sort(key=lambda item: (-item[0], len(item[1])))
    return [
        Candidate(
            source=f"{category.source}:category_resolver",
            source_url=category_url,
            name_raw=anchor_text or absolute_url,
            description_raw=context_text,
            website=absolute_url,
        )
        for _, absolute_url, anchor_text, context_text in ranked[:top_k]
    ]


def _normalize_internal_url(base_url: str, href: str) -> str | None:
    if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
        return None
    absolute = urljoin(base_url, href)
    parsed = urlparse(absolute)
    if not parsed.scheme or not parsed.netloc:
        return None
    clean = parsed._replace(fragment="").geturl()
    return clean.rstrip("/")


def _domain(url: str) -> str:
    return urlparse(url).netloc.removeprefix("www.").lower()


def _nearby_text(anchor) -> str | None:
    parent = anchor.find_parent(["article", "li", "div", "section"]) or anchor.parent
    if parent is None:
        return None
    text = parent.get_text(" ", strip=True)
    return text[:700] if text else None


def _link_score(haystack: str, url: str, intent: SearchIntent, clean_query_text: str) -> int:
    lowered = haystack.lower()
    path = urlparse(url).path.lower()
    score = 0

    if intent.model and _contains_phrase(lowered, intent.model.lower()):
        score += 100
    elif intent.model:
        score -= 40

    if intent.brand and intent.brand.lower() in lowered:
        score += 30

    for quantity in intent.attributes.values():
        if quantity.raw.lower() in lowered:
            score += 20
        if quantity.unit == "квт":
            watt_variant = f"{int(quantity.value * 1000)} вт".lower()
            if watt_variant in lowered:
                score += 20

    score += _token_overlap_score(clean_query_text, lowered)

    if any(marker in path for marker in _DETAIL_URL_MARKERS):
        score += 15
    if any(marker in path for marker in _CATEGORY_URL_MARKERS):
        score -= 20

    return score


def _contains_phrase(text: str, phrase: str) -> bool:
    return re.search(rf"(?<![a-zа-яё0-9]){re.escape(phrase)}(?![a-zа-яё0-9])", text, re.IGNORECASE) is not None


def _token_overlap_score(query: str, text: str) -> int:
    tokens = [token for token in re.findall(r"[a-zа-яё0-9]+", query.lower()) if len(token) > 2]
    if not tokens:
        return 0
    return min(20, 5 * sum(1 for token in set(tokens) if token in text))
