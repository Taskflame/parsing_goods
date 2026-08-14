from procurement_search.query_normalizer import normalize_query

CATEGORIES = {
    "Гальванические_покрытия": {
        "synonyms": ["анодирование", "цинкование", "хромирование"],
        "okved": ["25.61"],
        "tnved": [],
        "registries": [],
    },
    "Лабораторные_услуги": {
        "synonyms": ["испытательная лаборатория", "аккредитованная лаборатория"],
        "okved": ["71.20"],
        "tnved": [],
        "registries": ["rosakkreditatsiya"],
    },
}


def test_matches_known_category_by_direct_name():
    result = normalize_query("гальванические покрытия", categories=CATEGORIES)
    assert result.category == "Гальванические_покрытия"
    assert result.okved == ["25.61"]
    assert "анодирование" in result.search_terms


def test_matches_known_category_by_synonym():
    result = normalize_query("нужна аккредитованная лаборатория", categories=CATEGORIES)
    assert result.category == "Лабораторные_услуги"
    assert result.registries == ["rosakkreditatsiya"]


def test_unknown_query_falls_back_to_raw_search():
    result = normalize_query("совершенно другой запрос про ракеты", categories=CATEGORIES)
    assert result.category is None
    assert result.search_terms == ["совершенно другой запрос про ракеты"]
