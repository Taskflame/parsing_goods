from procurement_search.attribute_extractor import Quantity, classify_roles, extract_attributes, load_units
from procurement_search.llm_schemas import AttributeBatchGuess, AttributeBatchItem

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
        raise AssertionError("classify_attributes_batch_with_yandexgpt не должен вызываться по умолчанию")

    monkeypatch.setattr(
        "procurement_search.yandexgpt_classifier.classify_attributes_batch_with_yandexgpt", fake_classify
    )

    result = extract_attributes("перевозка груза 5000 без единицы", units=UNITS)

    assert result.attributes == []
    assert "5000" in result.clean_text


def test_llm_fallback_resolves_naked_number_when_enabled(monkeypatch):
    def fake_classify(raw_query, naked_numbers, units, **kwargs):
        assert naked_numbers == ["5000"]
        assert "т" in units
        return AttributeBatchGuess(
            attributes=[
                AttributeBatchItem(value="5000", unit="т", raw="5000", reasoning="судя по контексту это тонны груза")
            ]
        )

    monkeypatch.setattr(
        "procurement_search.yandexgpt_classifier.classify_attributes_batch_with_yandexgpt", fake_classify
    )

    result = extract_attributes(
        "перевозка груза 5000 без единицы", units=UNITS, use_llm_fallback=True
    )

    assert result.attributes == [_attr("5000", "т", "5000", source="llm")]
    assert "5000" not in result.clean_text


def test_llm_fallback_resolves_multiple_naked_numbers_in_one_batch_call(monkeypatch):
    """Проверяет, что для нескольких голых чисел делается ОДИН вызов LLM
    (не N), и что raw может быть шире одного числа — вырезается из
    clean_text дословной подстрокой, а не только по значению числа."""
    calls = []

    def fake_classify(raw_query, naked_numbers, units, **kwargs):
        calls.append(naked_numbers)
        return AttributeBatchGuess(
            attributes=[
                AttributeBatchItem(value="5.5", unit="квт", raw="5,5 киловат", reasoning="опечатка в киловатт"),
                AttributeBatchItem(value="2024", unit=None, raw="2024", reasoning="похоже на год модели"),
            ]
        )

    monkeypatch.setattr(
        "procurement_search.yandexgpt_classifier.classify_attributes_batch_with_yandexgpt", fake_classify
    )

    result = extract_attributes(
        "частотный преобразователь 5,5 киловат, модель 2024", units=UNITS, use_llm_fallback=True
    )

    assert len(calls) == 1  # один batch-вызов, а не по одному на число
    assert result.attributes == [_attr("5.5", "квт", "5,5 киловат", source="llm")]
    assert "5,5 киловат" not in result.clean_text
    assert "2024" in result.clean_text  # unit=null — не атрибут, остаётся в названии


def test_llm_fallback_unit_outside_dictionary_is_ignored(monkeypatch):
    def fake_classify(raw_query, naked_numbers, units, **kwargs):
        return AttributeBatchGuess(
            attributes=[AttributeBatchItem(value="5000", unit="дюйм", raw="5000", reasoning="ошибка модели")]
        )

    monkeypatch.setattr(
        "procurement_search.yandexgpt_classifier.classify_attributes_batch_with_yandexgpt", fake_classify
    )

    result = extract_attributes("труба 5000 непонятная", units=UNITS, use_llm_fallback=True)

    assert result.attributes == []


def test_llm_fallback_value_outside_naked_numbers_is_ignored(monkeypatch):
    def fake_classify(raw_query, naked_numbers, units, **kwargs):
        # Модель вернула значение, которого не было в списке запрошенных —
        # не должно попасть в результат.
        return AttributeBatchGuess(
            attributes=[AttributeBatchItem(value="9999", unit="т", raw="9999", reasoning="додумала")]
        )

    monkeypatch.setattr(
        "procurement_search.yandexgpt_classifier.classify_attributes_batch_with_yandexgpt", fake_classify
    )

    result = extract_attributes("труба 5000 непонятная", units=UNITS, use_llm_fallback=True)

    assert result.attributes == []


def test_llm_fallback_survives_errors(monkeypatch):
    def fake_classify(*args, **kwargs):
        raise RuntimeError("network unreachable")

    monkeypatch.setattr(
        "procurement_search.yandexgpt_classifier.classify_attributes_batch_with_yandexgpt", fake_classify
    )

    result = extract_attributes("труба 5000 непонятная", units=UNITS, use_llm_fallback=True)

    assert result.attributes == []


def _attr(value: str, unit: str, raw_text: str, source: str = "dict"):
    from procurement_search.attribute_extractor import ExtractedAttribute

    return ExtractedAttribute(value=value, unit=unit, raw_text=raw_text, source=source)


# --- Роль-классификация (classify_roles) ---

ROLE_UNITS = {
    "квт": ["квт", "kw", "киловатт", "киловат"],
    "л": ["л", "литр", "литров"],
    "в": ["в", "вольт"],
    "м": ["м", "метр", "метра", "метров"],  # все формы — как в реальном config/units.yaml
    "шт": ["шт", "штук", "штука"],
    "компл": ["компл", "комплект", "комплектов"],
    "упак": ["упак", "упаковка", "упаковок"],
    "пара": ["пара", "пары", "пар"],
    "мм2": ["мм2", "мм²"],
}

SPEC_RANGES = {
    "генератор_бензиновый": {
        "name": "бензогенератор",
        "keywords": ["генератор", "бензогенератор"],
        "ranges": {
            "квт": {"min": 0.5, "max": 30, "label": "мощность"},
            "л": {"min": 3, "max": 60, "label": "объём бака"},
        },
    }
}


def _roles(raw_query, brand=None, spec_ranges=None):
    extraction = extract_attributes(raw_query, units=ROLE_UNITS)
    return classify_roles(extraction, raw_query, brand, ROLE_UNITS, spec_ranges=spec_ranges)


def test_classify_roles_generator_with_ambiguous_liters_and_model_collision():
    """Обязательный тест-кейс 1: "3000 литров" — одновременно specs (есть
    единица "литров") и подозрение на индекс модели Hunter 3000 — conflicts
    непустой."""
    parsed = _roles(
        "генератор на бензине hunter 3000 литров 2,5 киловат 11 штук",
        brand="Hunter",
        spec_ranges=SPEC_RANGES,
    )
    assert parsed.order_qty == Quantity(11.0, "шт", "11 штук")
    assert parsed.specs["квт"] == Quantity(2.5, "квт", "2,5 киловат")
    assert parsed.model == "3000"
    assert parsed.conflicts != []


def test_classify_roles_generator_without_liters_no_conflict():
    """Обязательный тест-кейс 2: без "литров" число 3000 — только индекс
    модели, в specs не попадает вообще -> conflicts пуст."""
    parsed = _roles("генератор Hunter 3000, 2.5 кВт, 11 шт", brand="Hunter", spec_ranges=SPEC_RANGES)
    assert parsed.order_qty == Quantity(11.0, "шт", "11 шт")
    assert parsed.specs["квт"] == Quantity(2.5, "квт", "2.5 кВт")
    assert "л" not in parsed.specs
    assert parsed.model == "3000"
    assert parsed.conflicts == []


def test_classify_roles_marker_based_order_qty_defaults_to_kompl():
    """Обязательный тест-кейс 3: "нужно 5 компрессоров" — order_qty без
    явной единицы, определяется маркером "нужно", единица по умолчанию
    "компл"."""
    parsed = _roles("нужно 5 компрессоров 380В")
    assert parsed.order_qty == Quantity(5.0, "компл", "5")
    assert parsed.specs["в"] == Quantity(380.0, "в", "380В")
    assert parsed.model is None
    assert parsed.conflicts == []


def test_classify_roles_cable_cross_section_not_mistaken_for_order_qty():
    """Обязательный тест-кейс 4: "3х2,5" — сечение кабеля, не количество;
    "500 метров" — метраж закупки (order_length, не order_qty/не specs):
    единственная величина длины в запросе, order_qty (счётное количество)
    не найден — типовой случай "купить 500 метров кабеля", не "деталь
    длиной 500 метров"."""
    parsed = _roles("кабель ВВГ 3х2,5 500 метров")
    assert parsed.order_qty is None
    assert parsed.specs["мм2"] == Quantity(2.5, "мм2", "3х2,5")
    assert "м" not in parsed.specs
    assert parsed.order_length == Quantity(500.0, "м", "500 метров")
    assert parsed.model is None
    assert parsed.conflicts == []


def test_classify_roles_no_matching_category_skips_range_check():
    """Обязательный тест-кейс 5: категории "СОЖ" нет в spec_ranges.yaml —
    проверка диапазона молча пропускается, не ошибка."""
    parsed = _roles("СОЖ для вытяжки 200 л", spec_ranges=SPEC_RANGES)
    assert parsed.order_qty is None
    assert parsed.specs["л"] == Quantity(200.0, "л", "200 л")
    assert parsed.model is None
    assert parsed.conflicts == []


def test_classify_roles_real_units_yaml_has_new_count_units():
    """Smoke-тест на реальный units.yaml (не мок-словарь) — ловит опечатки
    в конфиге, которые тесты на ROLE_UNITS не увидят."""
    units = load_units()
    for code in ("шт", "компл", "упак", "пара", "мм2"):
        assert code in units, f"{code!r} должен быть в config/units.yaml"


# --- order_length ("метраж закупки") — см. ParsedQuery/LENGTH_UNITS ---


def test_classify_roles_marker_based_order_length():
    """Явный маркер ('нужно 50 метров кабеля') побеждает независимо от
    того, есть ли order_qty (в этом запросе его нет)."""
    parsed = _roles("нужно 50 метров кабеля ВВГ")
    assert parsed.order_length == Quantity(50.0, "м", "50 метров")
    assert parsed.order_qty is None
    assert "м" not in parsed.specs
    # Само слово-маркер "нужно" не вырезается из product — вырезается
    # только сам матч order_length ("50 метров"), тот же принцип, что и у
    # маркерного order_qty (см. test_classify_roles_marker_based_order_qty_defaults_to_kompl,
    # который тоже не трогает "нужно" в исходном тексте).
    assert parsed.product == "нужно кабеля ВВГ"


def test_classify_roles_length_stays_spec_when_order_qty_already_found():
    """'труба 3/4 1.5 метра 20 штук' — order_qty (20 шт) уже найден шагом 1,
    поэтому 1.5 метра остаётся характеристикой ОДНОЙ трубы (specs), а не
    становится order_length — это разные вещи: длина сегмента и количество
    сегментов."""
    parsed = _roles("труба 3/4 1.5 метра 20 штук")
    assert parsed.order_qty == Quantity(20.0, "шт", "20 штук")
    assert parsed.order_length is None
    assert parsed.specs["м"] == Quantity(1.5, "м", "1.5 метра")


def test_classify_roles_marker_wins_even_with_order_qty_present():
    """Явный маркер у метража побеждает, даже если order_qty (count) тоже
    найден в другой части запроса — оба поля независимы (см. ParsedQuery)."""
    parsed = _roles("нужно 6 комплектов, требуется 50 метров кабеля к ним")
    # "6 комплектов" — словарное совпадение (unit=компл уже есть в ROLE_UNITS),
    # не через маркерный путь для голых чисел — raw включает слово единицы целиком.
    assert parsed.order_qty == Quantity(6.0, "компл", "6 комплектов")
    assert parsed.order_length == Quantity(50.0, "м", "50 метров")


def test_classify_roles_multiple_marked_lengths_is_a_conflict():
    parsed = _roles("нужно 50 метров кабеля и требуется 30 метров провода")
    assert parsed.order_length is not None
    assert parsed.conflicts != []


def test_classify_roles_multiple_unmarked_lengths_stay_specs_no_promotion():
    """Две величины длины без маркера, без order_qty — неоднозначно (какая
    из двух метраж, какая характеристика?), поэтому НИ одна не становится
    order_length, обе остаются specs (тот же принцип "не угадывать")."""
    parsed = _roles("кабель 1 метр в бухте, длина бухты 100 метров")
    assert parsed.order_length is None
