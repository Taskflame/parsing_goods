"""Тесты PHONE_RE/EMAIL_RE/ADDRESS_RE (sources/base.py) — реальные форматы,
которые раньше не матчились (design-обсуждение, проверено вживую на
реальных сайтах): пробел перед скобкой в телефоне, название улицы перед
типом ("Кутузовский проспект"), номер дома без слова "дом"/"д."."""

from procurement_search.sources.base import ADDRESS_RE, EMAIL_RE, PHONE_RE, first_match


def test_phone_matches_space_before_parenthesis():
    # Раньше не матчилось: между "+7" и кодом допускался только один
    # символ-разделитель, а тут их два подряд (пробел, потом скобка).
    assert first_match(PHONE_RE, "звоните: +7 (495) 256-16-36 многоканальный") == "+7 (495) 256-16-36"


def test_phone_matches_various_real_formats():
    cases = {
        "8-800-555-73-08": "8-800-555-73-08",
        "8(495)123-45-67": "8(495)123-45-67",
        "+74951234567": "+74951234567",
        "8 800 555 73 08": "8 800 555 73 08",
    }
    for text, expected in cases.items():
        assert first_match(PHONE_RE, text) == expected


def test_email_matches_standard_address():
    assert first_match(EMAIL_RE, "пишите на info@company.ru по вопросам") == "info@company.ru"


def test_address_matches_name_before_street_type():
    # "Кутузовский проспект" — название ПЕРЕД типом улицы, не после, как у
    # "ул. Полковая" — раньше матчился только порядок "тип сначала".
    result = first_match(ADDRESS_RE, "г. Москва, Кутузовский проспект, 45")
    assert result == "г. Москва, Кутузовский проспект, 45"


def test_address_matches_house_number_without_dom_marker():
    # Раньше номер дома обязательно требовал слово "д."/"дом" перед собой.
    assert first_match(ADDRESS_RE, "просп. Мира 145, оф. 3") == "просп. Мира 145, оф. 3"


def test_address_still_matches_classic_format():
    assert first_match(ADDRESS_RE, "г. Москва, ул. Бибиревская, д.2 корп.1") == (
        "г. Москва, ул. Бибиревская, д.2 корп.1"
    )


def test_address_does_not_match_unrelated_text():
    assert first_match(ADDRESS_RE, "ИНН 9717068746 (торг.ооо) Вход Регистрация") is None


def test_address_still_matches_full_word_street_type_without_period():
    assert first_match(ADDRESS_RE, "ш. Энтузиастов, д. 5") == "ш. Энтузиастов, д. 5"


def test_address_does_not_match_letters_inside_unrelated_words():
    # Реальный баг с живой выдачи: короткие сокращения ("ул", "ш" и т.п.)
    # без точки/границы слова матчились как начало адреса, если внутри
    # ДРУГОГО слова случайно встречалась та же буквенная последовательность,
    # а дальше по строке находились хоть какие-то цифры (после того как
    # "дом"/"д." перед номером стал необязателен).
    cases = [
        "Кабель для прогрева бетона 40",
        "специалист Закрыть окно Почта для заявок 1",
        "город Москва Сравнение 0",
        "регулятор 500623",
        "ваши запросы сюда 0",
        "шт 432",
    ]
    for text in cases:
        assert first_match(ADDRESS_RE, text) is None, f"ложное срабатывание на {text!r}"
