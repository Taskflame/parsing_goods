"""Тесты stepper_probe.py — РЕАЛЬНЫЙ (не замоканный) прогон Playwright
против локальных HTML-заглушек: сама суть модуля — правильно ли работает
интерактив с реальным DOM/JS, это нельзя проверить моком браузера, только
настоящим. pytest.importorskip — playwright опциональная зависимость
(design-обсуждение в pipeline.py: не входит в requirements.txt), без него
эти тесты аккуратно пропускаются, а не падают."""

import pytest

pytest.importorskip("playwright")

from procurement_search.stepper_probe import probe_max_orderable_quantity  # noqa: E402

_STEPPER_HTML = """<!doctype html>
<html><body>
<div class="bx-touch-spin">
  <button type="button" class="qty-minus" onclick="dec()">-</button>
  <input type="text" id="qty" class="quantity-field" value="1" readonly>
  <button type="button" class="qty-plus" onclick="inc()">+</button>
</div>
<div id="error" style="display:none;"></div>
<script>
const MAX_STOCK = {max_stock};
window.__qty = 1;
function render() {{ document.getElementById('qty').value = window.__qty; }}
function inc() {{
  if (window.__qty + 1 > MAX_STOCK) {{
    document.getElementById('error').style.display = 'block';
    document.getElementById('error').textContent = 'Доступно только ' + MAX_STOCK + ' шт';
    return;
  }}
  document.getElementById('error').style.display = 'none';
  window.__qty += 1;
  render();
}}
function dec() {{ if (window.__qty > 1) {{ window.__qty -= 1; render(); }} }}
</script>
</body></html>"""

_NO_STEPPER_HTML = "<!doctype html><html><body><p>Просто страница без степпера.</p></body></html>"

# Степпера НЕТ в исходном DOM вообще — появляется только после клика по
# "В корзину" (реальный кейс с живой выдачи: см. _try_add_to_cart_then_find_stepper).
_TWO_STEP_HTML = """<!doctype html>
<html><body>
<div id="buy-block">
  <button type="button" id="cart-btn" onclick="addToCart()">В корзину</button>
</div>
<script>
const MAX_STOCK = {max_stock};
function addToCart() {{
  document.getElementById('buy-block').innerHTML = `
    <div class="bx-touch-spin">
      <button type="button" class="qty-minus" onclick="dec()">-</button>
      <input type="text" id="qty" class="quantity-field" value="1" readonly>
      <button type="button" class="qty-plus" onclick="inc()">+</button>
    </div>
    <div id="error" style="display:none;"></div>
  `;
  window.__qty = 1;
}}
function render() {{ document.getElementById('qty').value = window.__qty; }}
function inc() {{
  if (window.__qty + 1 > MAX_STOCK) {{
    document.getElementById('error').style.display = 'block';
    document.getElementById('error').textContent = 'Доступно только ' + MAX_STOCK + ' шт';
    return;
  }}
  document.getElementById('error').style.display = 'none';
  window.__qty += 1;
  render();
}}
function dec() {{ if (window.__qty > 1) {{ window.__qty -= 1; render(); }} }}
</script>
</body></html>"""

# Клик по кнопке уводит на другую страницу — эвристика должна остановиться,
# а не пытаться искать степпер уже на чужой странице.
_NAVIGATES_AWAY_HTML = """<!doctype html>
<html><body>
<a href="other.html"><button type="button">Купить</button></a>
</body></html>"""

# Ни один текст/класс не совпадает НИ С ОДНИМ известным паттерном шагов 1-2
# (не "+", не "в корзину", не "купить", не "добавить", класс не содержит
# "cart") — единственный способ найти степпер здесь — шаг 3 (LLM). Это и
# есть кейс, который реально сломал 7 из 7 кандидатов на живой выдаче.
_LLM_ONLY_HTML = """<!doctype html>
<html><body>
<div id="buy-block">
  <button type="button" class="xyz-weird-99" onclick="addToCart()">Нажми сюда чтобы забрать</button>
</div>
<script>
const MAX_STOCK = {max_stock};
function addToCart() {{
  document.getElementById('buy-block').innerHTML = `
    <div class="bx-touch-spin">
      <button type="button" class="qty-minus" onclick="dec()">-</button>
      <input type="text" id="qty" class="quantity-field" value="1" readonly>
      <button type="button" class="qty-plus" onclick="inc()">+</button>
    </div>
  `;
  window.__qty = 1;
}}
function render() {{ document.getElementById('qty').value = window.__qty; }}
function inc() {{ if (window.__qty + 1 <= MAX_STOCK) {{ window.__qty += 1; render(); }} }}
function dec() {{ if (window.__qty > 1) {{ window.__qty -= 1; render(); }} }}
</script>
</body></html>"""

# "Кнопка" — на самом деле <div> с onclick, не семантический <button>/<a>.
# Реальная находка на живой выдаче: узкий список _ADD_TO_CART_SELECTORS
# (только button:.../a:...) вообще не видел такой элемент как кандидата —
# теперь должен ловиться уже на бесплатном шаге 2, без LLM.
_DIV_ADD_TO_CART_HTML = """<!doctype html>
<html><body>
<div id="buy-block">
  <div class="add-to-cart-widget" onclick="addToCart()">В корзину</div>
</div>
<script>
const MAX_STOCK = {max_stock};
function addToCart() {{
  document.getElementById('buy-block').innerHTML = `
    <div class="bx-touch-spin">
      <button type="button" class="qty-minus" onclick="dec()">-</button>
      <input type="text" id="qty" class="quantity-field" value="1" readonly>
      <button type="button" class="qty-plus" onclick="inc()">+</button>
    </div>
  `;
  window.__qty = 1;
}}
function render() {{ document.getElementById('qty').value = window.__qty; }}
function inc() {{ if (window.__qty + 1 <= MAX_STOCK) {{ window.__qty += 1; render(); }} }}
function dec() {{ if (window.__qty > 1) {{ window.__qty -= 1; render(); }} }}
</script>
</body></html>"""


def _write_fixture(tmp_path, content: str, name: str = "fixture.html") -> str:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path.as_uri()


def test_probe_confirms_target_when_stock_is_enough(tmp_path):
    url = _write_fixture(tmp_path, _STEPPER_HTML.format(max_stock=12))

    result = probe_max_orderable_quantity(url, target_qty=5)

    assert result is not None
    assert result.target_confirmed is True
    assert result.max_orderable is None


def test_probe_finds_discovered_max_when_stock_runs_out(tmp_path):
    url = _write_fixture(tmp_path, _STEPPER_HTML.format(max_stock=12))

    result = probe_max_orderable_quantity(url, target_qty=20)

    assert result is not None
    assert result.target_confirmed is False
    assert result.max_orderable == 12.0
    assert result.evidence is not None and "12" in result.evidence


def test_probe_returns_none_when_no_stepper_found(tmp_path):
    url = _write_fixture(tmp_path, _NO_STEPPER_HTML, name="no_stepper.html")

    result = probe_max_orderable_quantity(url, target_qty=5)

    assert result is None


def test_probe_returns_none_when_page_unreachable():
    result = probe_max_orderable_quantity(
        "https://this-domain-definitely-does-not-exist-abc123.invalid/product", target_qty=5
    )

    assert result is None


def test_probe_reuses_shared_browser_across_calls(tmp_path):
    """pipeline._refine_relevance переиспользует один браузер на весь
    прогон (см. stepper_probe.open_browser) — проверяем, что передача
    готового browser= не ломает работу и не открывает второй экземпляр."""
    from procurement_search.stepper_probe import open_browser

    url = _write_fixture(tmp_path, _STEPPER_HTML.format(max_stock=12))
    pw_context, browser = open_browser()
    try:
        result1 = probe_max_orderable_quantity(url, target_qty=5, browser=browser)
        result2 = probe_max_orderable_quantity(url, target_qty=20, browser=browser)
    finally:
        browser.close()
        pw_context.stop()

    assert result1.target_confirmed is True
    assert result2.max_orderable == 12.0


def test_probe_finds_stepper_that_only_appears_after_add_to_cart_click(tmp_path):
    """Регрессия по реальному кейсу с живой выдачи: 7 из 7 кандидатов
    ответили "степпер не найден" при первой же проверке — самая вероятная
    причина, типовой двухшаговый UX (степпер появляется только после
    клика по "В корзину"). Эвристика должна дожать этот случай вторым
    шагом (_try_add_to_cart_then_find_stepper), не сдаваться сразу."""
    url = _write_fixture(tmp_path, _TWO_STEP_HTML.format(max_stock=8))

    result = probe_max_orderable_quantity(url, target_qty=5)

    assert result is not None
    assert result.target_confirmed is True


def test_probe_second_step_discovers_max_after_cart_click(tmp_path):
    url = _write_fixture(tmp_path, _TWO_STEP_HTML.format(max_stock=8))

    result = probe_max_orderable_quantity(url, target_qty=20)

    assert result is not None
    assert result.target_confirmed is False
    assert result.max_orderable == 8.0


def test_probe_aborts_when_cart_click_navigates_away(tmp_path):
    """Безопасность второго шага: если клик по кнопке увёл на другую
    страницу (типичный признак "оформить заказ", а не "добавить в
    корзину") — честное None, не продолжаем работать на чужой странице."""
    url = _write_fixture(tmp_path, _NAVIGATES_AWAY_HTML, name="navigates_away.html")
    _write_fixture(tmp_path, _NO_STEPPER_HTML, name="other.html")

    result = probe_max_orderable_quantity(url, target_qty=5)

    assert result is None


# --- Шаг 3 (LLM-подсказка) — сам браузер настоящий, LLM-вызов замокан:
# реального сетевого/платного вызова к YandexGPT в тестах быть не должно,
# но взаимодействие с DOM после того, как LLM "выбрала" элемент — да. ---


def test_probe_llm_step_finds_stepper_when_steps_1_and_2_fail(tmp_path, monkeypatch):
    """Регрессия по реальному кейсу: кнопка с нетиповым текстом/классом,
    не совпадающая ни с одним паттерном шагов 1-2 — единственный способ
    найти степпер тут — шаг 3. Единственный кликабельный элемент на
    странице до клика — эта самая кнопка, поэтому LLM должна вернуть
    element_index=0."""
    from procurement_search import yandexgpt_classifier
    from procurement_search.llm_schemas import ElementLocatorGuess

    monkeypatch.setattr(
        yandexgpt_classifier,
        "locate_cart_control_with_yandexgpt",
        lambda product_description, elements_text, **kwargs: ElementLocatorGuess(
            element_index=0, reasoning="единственная кнопка на странице"
        ),
    )

    url = _write_fixture(tmp_path, _LLM_ONLY_HTML.format(max_stock=8))

    result = probe_max_orderable_quantity(url, target_qty=5, product_description="труба стальная")

    assert result is not None
    assert result.target_confirmed is True


def test_probe_llm_step_respects_discovered_max(tmp_path, monkeypatch):
    from procurement_search import yandexgpt_classifier
    from procurement_search.llm_schemas import ElementLocatorGuess

    monkeypatch.setattr(
        yandexgpt_classifier,
        "locate_cart_control_with_yandexgpt",
        lambda product_description, elements_text, **kwargs: ElementLocatorGuess(
            element_index=0, reasoning="единственная кнопка на странице"
        ),
    )

    url = _write_fixture(tmp_path, _LLM_ONLY_HTML.format(max_stock=8))

    result = probe_max_orderable_quantity(url, target_qty=20, product_description="труба стальная")

    assert result is not None
    assert result.target_confirmed is False
    assert result.max_orderable == 8.0


def test_probe_llm_step_returns_none_when_llm_finds_nothing_relevant(tmp_path, monkeypatch):
    """LLM честно говорит 'ни один элемент не подходит' (element_index=null)
    — не угадываем клик наугад, итог None."""
    from procurement_search import yandexgpt_classifier
    from procurement_search.llm_schemas import ElementLocatorGuess

    monkeypatch.setattr(
        yandexgpt_classifier,
        "locate_cart_control_with_yandexgpt",
        lambda product_description, elements_text, **kwargs: ElementLocatorGuess(
            element_index=None, reasoning="ничего похожего на корзину/степпер"
        ),
    )

    url = _write_fixture(tmp_path, _LLM_ONLY_HTML.format(max_stock=8))

    result = probe_max_orderable_quantity(url, target_qty=5, product_description="труба стальная")

    assert result is None


def test_probe_llm_step_gracefully_degrades_when_llm_call_fails(tmp_path, monkeypatch):
    """LLM недоступна (сеть/ключ/пакет) — шаг 3 просто пропускается, весь
    probe_max_orderable_quantity не падает, возвращает честное None (шаги
    1-2 уже не сработали в этой заглушке по построению)."""
    from procurement_search import yandexgpt_classifier

    def raise_error(*args, **kwargs):
        raise RuntimeError("LLM недоступна")

    monkeypatch.setattr(yandexgpt_classifier, "locate_cart_control_with_yandexgpt", raise_error)

    url = _write_fixture(tmp_path, _LLM_ONLY_HTML.format(max_stock=8))

    result = probe_max_orderable_quantity(url, target_qty=5, product_description="труба стальная")

    assert result is None


def test_probe_steps_1_and_2_never_call_llm(tmp_path, monkeypatch):
    """Шаги 1-2 бесплатны — если степпер нашёлся сразу (или после клика по
    типовой кнопке "В корзину"), LLM-вызов не должен происходить вовсе."""
    from procurement_search import yandexgpt_classifier

    calls = []
    monkeypatch.setattr(
        yandexgpt_classifier,
        "locate_cart_control_with_yandexgpt",
        lambda *a, **k: calls.append(1),
    )

    url = _write_fixture(tmp_path, _STEPPER_HTML.format(max_stock=12))
    result = probe_max_orderable_quantity(url, target_qty=5, product_description="труба стальная")

    assert result is not None
    assert calls == []


def test_probe_finds_div_based_add_to_cart_button_without_llm(tmp_path, monkeypatch):
    """Регрессия по реальному кейсу с живой выдачи (electro-master.ru,
    kuvalda.ru): "кнопка" на самом деле <div> с onclick, не button/a —
    расширенный _ADD_TO_CART_SELECTORS должен найти её на бесплатном шаге
    2, не долетая до LLM (шаг 3) вовсе."""
    from procurement_search import yandexgpt_classifier

    calls = []
    monkeypatch.setattr(
        yandexgpt_classifier,
        "locate_cart_control_with_yandexgpt",
        lambda *a, **k: calls.append(1),
    )

    url = _write_fixture(tmp_path, _DIV_ADD_TO_CART_HTML.format(max_stock=8))
    result = probe_max_orderable_quantity(url, target_qty=5, product_description="генератор")

    assert result is not None
    assert result.target_confirmed is True
    assert calls == []  # шаг 2 справился сам, LLM не понадобилась
