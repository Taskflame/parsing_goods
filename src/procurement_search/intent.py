"""Intent, page-type and offer-match helpers.

This module is deliberately deterministic and cheap. LLM checks can still
refine the result later, but the pipeline needs a stable first pass that
separates product identity from order constraints and does not treat every
"listing" as the same kind of page.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from procurement_search.attribute_extractor import ExtractionResult, ParsedQuery, Quantity, extract_attributes
from procurement_search.config import load_units
from procurement_search.models import IntentType, PageType, ProductMatch, SearchIntent, ServiceMatch

_SERVICE_WORDS = (
    "услуг",
    "утилизац",
    "обезвреж",
    "подряд",
    "исполнитель",
    "компания по",
    "организация по",
    "обслуживан",
    "ремонт",
    "монтаж",
    "гальван",
    "покрыт",
    "цинкован",
)
_SERVICE_ACTION_WORDS = (
    "ремонт",
    "монтаж",
    "обслуживан",
    "утилизац",
    "обезвреж",
    "гальван",
    "цинкован",
)
_PRODUCT_WORDS = (
    "купить",
    "цена",
    "товар",
    "поставка",
    "поставщик",
    "генератор",
    "ноутбук",
    "насос",
    "кабель",
)
_PRODUCT_DETAIL_URL_WORDS = (
    "/product/",
    "/products/",
    "/tovar/",
    "/item/",
    "/shop/",
    "/catalog/product",
)
_CATEGORY_URL_WORDS = (
    "/catalog/",
    "/category/",
    "/categories/",
    "/produkciya/",
    "/products/",
)
_SERVICE_URL_WORDS = ("/uslugi/", "/services/", "/service/")
_DIRECTORY_DOMAINS = (
    "2gis.",
    "yell.",
    "pulscen.",
    "optlist.",
    "tiu.",
    "flamp.",
    "zoon.",
    "yandex.",
)
_CONTENT_URL_WORDS = ("/blog/", "/news/", "/article/", "/wiki/", "/video/")
_PRICE_RE = re.compile(r"\b\d[\d\s.,]{1,12}\s*(?:₽|руб\.?|р\.|eur|usd)\b", re.IGNORECASE)
_SKU_RE = re.compile(r"\b(?:sku|артикул|mpn|gtin|productid)\b", re.IGNORECASE)
_MODEL_TOKEN_RE = re.compile(r"\b(?=[a-zа-яё0-9-]*\d)[a-zа-яё][a-zа-яё0-9-]{1,}\b", re.IGNORECASE)
_STOCK_SIGNAL_RE = re.compile(r"\b(?:в наличии|нет в наличии|остаток|под заказ)\b", re.IGNORECASE)


def parse_intent(
    raw_query: str,
    parsed_query: ParsedQuery | None = None,
    extraction: ExtractionResult | None = None,
    brand: str | None = None,
) -> SearchIntent:
    """Builds the minimal normalized intent used by the current pipeline.

    `parsed_query.order_qty` stays a quantity constraint and is not folded
    into the product identity. Product/service detection is intentionally
    conservative: explicit service wording wins unless the query also has
    strong product markers such as a brand/model or physical specs.
    """
    lowered = raw_query.lower()
    has_service_words = any(w in lowered for w in _SERVICE_WORDS)
    has_service_action = any(w in lowered for w in _SERVICE_ACTION_WORDS)
    has_product_words = any(w in lowered for w in _PRODUCT_WORDS)
    has_specs = bool(parsed_query.specs if parsed_query else extraction and extraction.attributes)
    has_model = bool(parsed_query and parsed_query.model)
    has_brand = bool(brand or (parsed_query and parsed_query.brand))

    if has_service_action or (
        has_service_words and not (has_brand or has_model or has_specs or has_product_words)
    ):
        intent_type = IntentType.SERVICE
    else:
        intent_type = IntentType.PRODUCT

    if intent_type == IntentType.SERVICE:
        radius = _extract_radius_km(raw_query)
        hard_constraints = {"radius_km": radius} if radius is not None else {}
        service, subject = _split_service_action_subject(raw_query, parsed_query)
        return SearchIntent(
            type=IntentType.SERVICE,
            service=service,
            subject=subject,
            quantity=None,
            hard_constraints=hard_constraints,
        )

    entity = _guess_product_entity(parsed_query.product if parsed_query else raw_query)
    model = _guess_product_model(raw_query, parsed_query, brand)
    return SearchIntent(
        type=IntentType.PRODUCT,
        entity=entity,
        brand=brand or (parsed_query.brand if parsed_query else None),
        model=model,
        identity_text=_guess_identity_text(raw_query, parsed_query, brand, model),
        quantity=_effective_quantity(parsed_query),
        attributes=dict(parsed_query.specs) if parsed_query else {},
    )


def classify_page_type(url: str | None, title: str | None = None, text: str | None = None) -> PageType:
    """Classifies the landing page into a richer type than legacy is_listing."""
    haystack = " ".join(part for part in (url or "", title or "", text or "") if part).lower()
    parsed = urlparse(url or "")
    domain = parsed.netloc.lower()
    path = parsed.path.lower()

    if any(marker in domain for marker in _DIRECTORY_DOMAINS):
        return PageType.DIRECTORY
    if any(marker in path for marker in _CONTENT_URL_WORDS):
        return PageType.CONTENT

    has_product_schema = "schema.org/product" in haystack or '"@type":"product"' in haystack.replace(" ", "")
    has_item_list = "schema.org/itemlist" in haystack or '"@type":"itemlist"' in haystack.replace(" ", "")
    has_offer = "offers" in haystack or "availability" in haystack
    has_price = bool(_PRICE_RE.search(haystack))
    has_sku = bool(_SKU_RE.search(haystack))
    price_count = len(_PRICE_RE.findall(haystack))

    has_stock_signal = bool(_STOCK_SIGNAL_RE.search(haystack))

    if any(w in path for w in _SERVICE_URL_WORDS):
        return PageType.SERVICE_DETAIL if has_service_signal(haystack) else PageType.SERVICE_CATEGORY

    if (
        has_product_schema
        or has_sku
        or (any(w in path for w in _PRODUCT_DETAIL_URL_WORDS) and (has_price or has_offer or has_stock_signal))
        or (price_count == 1 and not has_item_list)
        or (has_stock_signal and not has_item_list and not any(w in path for w in _CATEGORY_URL_WORDS))
    ):
        return PageType.PRODUCT_DETAIL
    if has_item_list or price_count >= 3 or any(w in path for w in _CATEGORY_URL_WORDS):
        return PageType.PRODUCT_CATEGORY
    if path in ("", "/"):
        return PageType.COMPANY_HOME
    if has_service_signal(haystack):
        return PageType.SERVICE_DETAIL
    return PageType.UNKNOWN


def match_product(intent: SearchIntent, title: str | None = None, text: str | None = None) -> ProductMatch:
    if intent.type != IntentType.PRODUCT:
        return ProductMatch.MISMATCH
    haystack = " ".join(part for part in (title or "", text or "") if part).lower()
    if not haystack:
        return ProductMatch.UNKNOWN

    if intent.model:
        model = intent.model.lower()
        if _contains_phrase(haystack, model):
            return ProductMatch.EXACT
        if _has_conflicting_model_token(haystack, model):
            return ProductMatch.MISMATCH
        return ProductMatch.UNKNOWN

    if intent.attributes:
        for qty in intent.attributes.values():
            if not _quantity_appears(qty, haystack):
                if _explicit_quantity_conflict(qty, haystack):
                    return ProductMatch.MISMATCH
                return ProductMatch.UNKNOWN
        return ProductMatch.COMPATIBLE

    entity_tokens = [t for t in re.findall(r"[a-zа-яё0-9]+", intent.entity or "") if len(t) > 2]
    if entity_tokens and all(t in haystack for t in entity_tokens):
        return ProductMatch.COMPATIBLE
    return ProductMatch.UNKNOWN


def match_service(intent: SearchIntent, title: str | None = None, text: str | None = None) -> ServiceMatch:
    if intent.type != IntentType.SERVICE:
        return ServiceMatch.MISMATCH
    haystack = " ".join(part for part in (title or "", text or "") if part).lower()
    if not haystack:
        return ServiceMatch.UNKNOWN
    service_tokens = {t for t in re.findall(r"[а-яёa-z]{4,}", intent.service or "")}
    if not service_tokens:
        return ServiceMatch.UNKNOWN
    overlap = len(service_tokens & set(re.findall(r"[а-яёa-z]{4,}", haystack)))
    if overlap >= max(1, min(2, len(service_tokens))):
        return ServiceMatch.MATCH
    if has_service_signal(haystack):
        return ServiceMatch.PARTIAL
    return ServiceMatch.MISMATCH


def build_search_terms(raw_query: str, intent: SearchIntent, clean_query_text: str, brand: str | None, kernel: str | None) -> list[str]:
    terms: list[str] = []
    if intent.type == IntentType.PRODUCT:
        identity = _product_identity_text(intent, clean_query_text)
        if identity:
            terms.extend([f'"{identity}"', f'"{identity}" купить', f'"{identity}" цена', f'"{identity}" "в наличии"'])
        if brand:
            without_brand = re.sub(re.escape(brand), "", clean_query_text, flags=re.IGNORECASE)
            without_brand = re.sub(r"\s+", " ", without_brand).strip()
            terms.insert(0, f"{brand} {without_brand}".strip() if without_brand else brand)
        if intent.attributes:
            terms.extend(_attribute_search_terms(intent, clean_query_text))
    else:
        if kernel:
            terms.append(kernel)
        terms.append(clean_query_text or raw_query)

    terms.append(raw_query)
    if brand and not any(t.startswith(brand) for t in terms):
        without_brand = re.sub(re.escape(brand), "", clean_query_text, flags=re.IGNORECASE)
        without_brand = re.sub(r"\s+", " ", without_brand).strip()
        terms.append(f"{brand} {without_brand}".strip() if without_brand else brand)
    if clean_query_text:
        terms.append(clean_query_text)
    if kernel:
        terms.insert(0, kernel)
    return list(dict.fromkeys(t for t in terms if t))


def has_service_signal(text: str) -> bool:
    lowered = text.lower()
    return any(w in lowered for w in _SERVICE_WORDS)


def _extract_radius_km(raw_query: str) -> int | None:
    match = re.search(r"(?:радиус[ае]?|в пределах)\s+(\d+)\s*км", raw_query, re.IGNORECASE)
    return int(match.group(1)) if match else None


def _strip_quantity_text(raw_query: str, parsed_query: ParsedQuery | None) -> str:
    text = raw_query
    if parsed_query:
        for qty in (parsed_query.order_qty, parsed_query.order_length):
            if qty is not None:
                text = text.replace(qty.raw, " ")
    return re.sub(r"\s+", " ", text).strip()


def _split_service_action_subject(raw_query: str, parsed_query: ParsedQuery | None) -> tuple[str, str | None]:
    text = _strip_quantity_text(raw_query, parsed_query)
    lowered = text.lower()
    action_match = re.search(r"\b(ремонт|монтаж|обслуживание|утилизация|обезвреживание|цинкование)\b", lowered)
    if not action_match:
        return text, None
    action = text[action_match.start() : action_match.end()]
    subject = text[action_match.end() :].strip(" ,.-")
    if action.lower() not in {"ремонт", "монтаж", "обслуживание"}:
        return text, subject or None
    return action, subject or None


def _effective_quantity(parsed_query: ParsedQuery | None) -> Quantity | None:
    if parsed_query is None:
        return None
    return parsed_query.order_qty or parsed_query.order_length


def _guess_product_entity(product_text: str) -> str | None:
    tokens = re.findall(r"[а-яёa-z]{4,}", product_text.lower())
    return tokens[0] if tokens else None


def _guess_product_model(raw_query: str, parsed_query: ParsedQuery | None, brand: str | None) -> str | None:
    if parsed_query and parsed_query.model and not _looks_like_order_quantity(parsed_query.model, parsed_query):
        return parsed_query.model
    tokens = re.findall(r"[A-Za-zА-Яа-яЁё0-9-]+", raw_query)
    if brand:
        brand_lower = brand.lower()
        for idx, token in enumerate(tokens):
            if token.lower() == brand_lower:
                tail = tokens[idx + 1 : idx + 4]
                model_tokens = [t for t in tail if _MODEL_TOKEN_RE.fullmatch(t)]
                if model_tokens:
                    return " ".join(model_tokens)
    model_tokens = [t for t in tokens if _MODEL_TOKEN_RE.fullmatch(t)]
    return " ".join(model_tokens[:2]) if model_tokens else None


def _looks_like_order_quantity(model: str, parsed_query: ParsedQuery) -> bool:
    for qty in (parsed_query.order_qty, parsed_query.order_length):
        if qty is None:
            continue
        try:
            if float(model.replace(",", ".")) == qty.value:
                return True
        except ValueError:
            continue
    return False


def _contains_phrase(text: str, phrase: str) -> bool:
    return re.search(rf"(?<![a-zа-яё0-9]){re.escape(phrase)}(?![a-zа-яё0-9])", text, re.IGNORECASE) is not None


def _model_family(model: str) -> str | None:
    tokens = _MODEL_TOKEN_RE.findall(model)
    token = tokens[-1] if tokens else model
    match = re.match(r"([a-zа-яё]+)(\d+)", token, re.IGNORECASE)
    return match.group(1).lower() if match else None


def _has_conflicting_model_token(text: str, requested_model: str) -> bool:
    requested_family = _model_family(requested_model)
    if not requested_family:
        return False
    for token in _MODEL_TOKEN_RE.findall(text):
        token_lower = token.lower()
        if _contains_phrase(token_lower, requested_model.lower()):
            continue
        if _model_family(token_lower) == requested_family:
            return True
    return False


def _quantity_appears(qty: Quantity, text: str) -> bool:
    if qty.raw and qty.raw.lower() in text.lower():
        return True
    return any(_quantities_close(qty, found) for found in _compatible_quantities(qty, text))


def _explicit_quantity_conflict(qty: Quantity, text: str) -> bool:
    found = _compatible_quantities(qty, text)
    return bool(found) and not any(_quantities_close(qty, candidate) for candidate in found)


def _compatible_quantities(qty: Quantity, text: str) -> list[Quantity]:
    extraction = extract_attributes(text, units=load_units())
    result: list[Quantity] = []
    for attr in extraction.attributes:
        candidate = Quantity(float(attr.value), attr.unit, attr.raw_text)
        if _can_compare_units(qty.unit, candidate.unit):
            result.append(candidate)
    return result


def _can_compare_units(left: str, right: str) -> bool:
    if left == right:
        return True
    return {left, right} == {"квт", "вт"}


def _to_unit(value: float, from_unit: str, to_unit: str) -> float | None:
    if from_unit == to_unit:
        return value
    if from_unit == "вт" and to_unit == "квт":
        return value / 1000
    if from_unit == "квт" and to_unit == "вт":
        return value * 1000
    return None


def _quantities_close(expected: Quantity, found: Quantity) -> bool:
    converted = _to_unit(found.value, found.unit, expected.unit)
    if converted is None:
        return False
    tolerance = max(abs(expected.value) * 0.03, 0.01)
    return abs(converted - expected.value) <= tolerance


def _product_identity_text(intent: SearchIntent, fallback: str) -> str:
    if intent.identity_text:
        return intent.identity_text
    if intent.brand and intent.model:
        return f"{intent.brand} {intent.model}"
    if intent.model:
        return intent.model
    return fallback


def _guess_identity_text(
    raw_query: str,
    parsed_query: ParsedQuery | None,
    brand: str | None,
    model: str | None,
) -> str | None:
    text = raw_query
    text = _strip_quantity_text(text, parsed_query)
    if brand:
        brand_match = re.search(re.escape(brand), text, re.IGNORECASE)
        if brand_match:
            text = text[brand_match.start() :]
    if model and model.lower() not in text.lower():
        return f"{brand} {model}".strip() if brand else model
    return re.sub(r"\s+", " ", text).strip() or None


def _attribute_search_terms(intent: SearchIntent, clean_query_text: str) -> list[str]:
    terms = []
    for qty in intent.attributes.values():
        if qty.unit == "квт":
            comma_value = f"{qty.value:g}".replace(".", ",")
            dot_value = f"{qty.value:g}"
            watts = int(qty.value * 1000)
            terms.extend(
                [
                    f"{clean_query_text} {comma_value} кВт цена",
                    f"{clean_query_text} {dot_value} kW",
                    f"{clean_query_text} {watts} Вт",
                ]
            )
    return terms
