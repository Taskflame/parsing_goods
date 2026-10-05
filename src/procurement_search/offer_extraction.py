"""Offer extraction from a confirmed landing page.

Company enrichment answers "who is this supplier?". Offer extraction answers
"what exact product/service was confirmed, on which page, and with what
evidence?". The functions here intentionally accept already-fetched landing
text and already-decided match/page-type values: crawling, intent parsing and
ranking stay in pipeline.py.
"""

from __future__ import annotations

import re

from procurement_search.attribute_extractor import Quantity
from procurement_search.models import (
    Availability,
    Evidence,
    PageType,
    ProductMatch,
    ProductOffer,
    SearchIntent,
    ServiceMatch,
    ServiceOffer,
    StockStatus,
)
from procurement_search.sources.base import PRICE_RE, first_match

_CURRENCY_RE = re.compile(r"(₽|руб\.?|р\.|eur|usd)", re.IGNORECASE)


def parse_price_value(raw: str | None) -> float | None:
    if not raw:
        return None
    cleaned = re.sub(r"[^\d.,]", "", raw)
    if not cleaned:
        return None
    if "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    elif "," in cleaned:
        decimals = len(cleaned) - cleaned.rindex(",") - 1
        cleaned = cleaned.replace(",", ".") if decimals <= 2 else cleaned.replace(",", "")
    try:
        value = float(cleaned)
    except ValueError:
        return None
    return value if value > 0 else None


def price_currency(raw: str | None) -> str | None:
    if not raw:
        return None
    match = _CURRENCY_RE.search(raw)
    if not match:
        return None
    token = match.group(1).lower()
    if token in {"₽", "р", "р.", "руб", "руб."}:
        return "RUB"
    return token.upper()


def extract_product_offer(
    intent: SearchIntent,
    landing_url: str,
    page_type: PageType,
    product_match: ProductMatch,
    landing_text: str,
    *,
    company_id: str | None = None,
    title: str | None = None,
    price_raw: str | None = None,
    stock_status: StockStatus = StockStatus.NOT_CHECKED,
    stock_quote: str | None = None,
    availability: Availability | None = None,
) -> ProductOffer:
    price_raw = price_raw or first_match(PRICE_RE, landing_text)
    evidence: list[Evidence] = [
        Evidence(
            field="product_match",
            value=product_match.value,
            source_url=landing_url,
            source_text=_short_text(title or landing_text),
        )
    ]
    if intent.model:
        evidence.append(
            Evidence("model", intent.model, landing_url, _snippet_around(landing_text, intent.model))
        )
    if price_raw:
        evidence.append(Evidence("price", price_raw, landing_url, _snippet_around(landing_text, price_raw)))
    if stock_quote:
        evidence.append(Evidence("stock_status", stock_status.value, landing_url, stock_quote))
    if availability and availability.evidence:
        evidence.append(
            Evidence("availability", availability.status.value, availability.source_url, availability.evidence)
        )

    return ProductOffer(
        company_id=company_id,
        landing_url=landing_url,
        page_type=page_type,
        product_match=product_match,
        product_name=title,
        brand=intent.brand,
        model=intent.model,
        attributes={unit: _quantity_to_dict(qty) for unit, qty in intent.attributes.items()},
        price=parse_price_value(price_raw),
        currency=price_currency(price_raw),
        stock_status=stock_status,
        stock_quantity=int(availability.quantity.value) if availability and availability.quantity else None,
        evidence=evidence,
    )


def extract_service_offer(
    intent: SearchIntent,
    landing_url: str,
    page_type: PageType,
    service_match: ServiceMatch,
    landing_text: str,
    *,
    company_id: str | None = None,
    title: str | None = None,
    price_raw: str | None = None,
) -> ServiceOffer:
    price_raw = price_raw or first_match(PRICE_RE, landing_text)
    evidence = [
        Evidence(
            field="service_match",
            value=service_match.value,
            source_url=landing_url,
            source_text=_short_text(title or landing_text),
        )
    ]
    if price_raw:
        evidence.append(Evidence("price", price_raw, landing_url, _snippet_around(landing_text, price_raw)))

    return ServiceOffer(
        company_id=company_id,
        landing_url=landing_url,
        page_type=page_type,
        service_match=service_match,
        service_name=intent.service,
        description=_short_text(landing_text, limit=300),
        constraints=dict(intent.hard_constraints),
        price=parse_price_value(price_raw),
        currency=price_currency(price_raw),
        evidence=evidence,
    )


def _quantity_to_dict(quantity: Quantity) -> dict[str, object]:
    return {"value": quantity.value, "unit": quantity.unit, "raw": quantity.raw}


def _short_text(text: str | None, limit: int = 160) -> str | None:
    if not text:
        return None
    compact = re.sub(r"\s+", " ", text).strip()
    return compact[:limit]


def _snippet_around(text: str, needle: str, radius: int = 80) -> str | None:
    idx = text.lower().find(needle.lower())
    if idx == -1:
        return _short_text(text)
    start = max(0, idx - radius)
    end = min(len(text), idx + len(needle) + radius)
    return _short_text(text[start:end], limit=radius * 2 + len(needle))
