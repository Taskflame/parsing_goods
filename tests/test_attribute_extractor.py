from procurement_search.attribute_extractor import extract_attributes
from procurement_search.llm_classifier import AttributeGuess

UNITS = {
    "квт": ["квт", "kw", "киловатт"],
    "квтч": ["квт*ч", "kwh"],
    "мм": ["мм", "mm"],
    "м": ["м", "метр", "метров"],
    "ду": ["ду", "dn"],
    "ру": ["ру", "pn"],
    "т": ["т", "тонна", "тонн"],
}


def test_extracts_number_with_comma_and_unit():
    result = extract_attributes("лампочки светодиодные 2,5 квт", units=UNITS)
    assert len(result.attributes) == 1
    attr = result.attributes[0]
    assert attr.value == "2.5"
    assert attr.unit == "квт"
    assert attr.source == "dict"
    assert result.clean_text == "лампочки светодиодные"


def test_longest_alias_wins_over_shorter_prefix():
    result = extract_attributes("генератор 5 квт*ч", units=UNITS)
    assert result.attributes == [
        _attr("5", "квтч", "5 квт*ч"),
    ]


def test_multiple_attributes_extracted_and_stripped():
    result = extract_attributes("стальные трубы 30 метров 5 мм толщина", units=UNITS)
    units_found = {(a.value, a.unit) for a in result.attributes}
    assert units_found == {("30", "м"), ("5", "мм")}
    assert result.clean_text == "стальные трубы толщина"


def test_du_ru_matched_unit_first_glued():
    result = extract_attributes("труба стальная ду50 ру16", units=UNITS)
    units_found = {(a.value, a.unit) for a in result.attributes}
    assert units_found == {("50", "ду"), ("16", "ру")}
    assert result.clean_text == "труба стальная"


def test_thread_marking_not_misread_as_meters():
    # "М10" — резьба, а не "10 метров". "м" не входит в allowlist
    # unit-first единиц именно чтобы не путать эти два случая.
    result = extract_attributes("болт м10", units=UNITS)
    assert result.attributes == []
    assert result.clean_text == "болт м10"


def test_no_llm_call_by_default_for_naked_number(monkeypatch):
    def fake_classify(*args, **kwargs):
        raise AssertionError("classify_attribute_with_llm не должен вызываться по умолчанию")

    monkeypatch.setattr(
        "procurement_search.llm_classifier.classify_attribute_with_llm", fake_classify
    )

    result = extract_attributes("перевозка груза 5000 без единицы", units=UNITS)

    assert result.attributes == []
    assert "5000" in result.clean_text


def test_llm_fallback_resolves_naked_number_when_enabled(monkeypatch):
    def fake_classify(raw_query, number, known_units, **kwargs):
        assert number == "5000"
        assert "т" in known_units
        return AttributeGuess(unit="т", reasoning="судя по контексту это тонны груза")

    monkeypatch.setattr(
        "procurement_search.llm_classifier.classify_attribute_with_llm", fake_classify
    )

    result = extract_attributes(
        "перевозка груза 5000 без единицы", units=UNITS, use_llm_fallback=True
    )

    assert result.attributes == [_attr("5000", "т", "5000", source="llm")]
    assert "5000" not in result.clean_text


def test_llm_fallback_unit_outside_dictionary_is_ignored(monkeypatch):
    def fake_classify(*args, **kwargs):
        return AttributeGuess(unit="дюйм", reasoning="ошибка модели")

    monkeypatch.setattr(
        "procurement_search.llm_classifier.classify_attribute_with_llm", fake_classify
    )

    result = extract_attributes("труба 5000 непонятная", units=UNITS, use_llm_fallback=True)

    assert result.attributes == []


def test_llm_fallback_survives_errors(monkeypatch):
    def fake_classify(*args, **kwargs):
        raise RuntimeError("network unreachable")

    monkeypatch.setattr(
        "procurement_search.llm_classifier.classify_attribute_with_llm", fake_classify
    )

    result = extract_attributes("труба 5000 непонятная", units=UNITS, use_llm_fallback=True)

    assert result.attributes == []


def test_llm_provider_ollama_used_when_selected(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "ollama")

    def fake_classify_anthropic(*args, **kwargs):
        raise AssertionError("должен вызываться ollama, а не anthropic-провайдер")

    def fake_classify_ollama(raw_query, number, known_units, **kwargs):
        return AttributeGuess(unit="т", reasoning="локальная модель")

    monkeypatch.setattr(
        "procurement_search.llm_classifier.classify_attribute_with_llm", fake_classify_anthropic
    )
    monkeypatch.setattr(
        "procurement_search.ollama_classifier.classify_attribute_with_ollama", fake_classify_ollama
    )

    result = extract_attributes(
        "перевозка груза 5000 без единицы", units=UNITS, use_llm_fallback=True
    )

    assert result.attributes == [_attr("5000", "т", "5000", source="llm")]


def _attr(value: str, unit: str, raw_text: str, source: str = "dict"):
    from procurement_search.attribute_extractor import ExtractedAttribute

    return ExtractedAttribute(value=value, unit=unit, raw_text=raw_text, source=source)
