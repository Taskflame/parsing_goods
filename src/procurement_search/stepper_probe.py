"""Пилотная надстройка над Слоем 4 (availability.py) — интерактивная проверка
остатка через степпер количества ("−  1  +") на карточке товара, когда
текстовое извлечение (extract_availability, LLM) не дало числа
(status=in_stock/unknown без quantity).

ПОЧЕМУ ЭТО ОТДЕЛЬНЫЙ МОДУЛЬ, А НЕ ЧАСТЬ availability.py: другой класс
технологии. Весь остальной пайплайн — HTTP-текст (requests) + LLM-
интерпретация (yandexgpt_classifier.py), без исполнения JS. Степпер —
JS-виджет: клик по "+" не перезагружает страницу, а меняет DOM через AJAX,
это НЕ видно чистому HTTP-клиенту. Единственный способ прочитать реакцию —
реально исполнить JS, то есть настоящий браузерный движок (Playwright).
Число мы всегда читаем напрямую из атрибута input, не просим модель его
угадать — LLM (см. ниже, шаг 3) участвует только в том, ГДЕ кликнуть,
никогда в том, СКОЛЬКО осталось на складе.

ЭВРИСТИКА, НЕ ГАРАНТИЯ (design-обсуждение при выборе этого пути): нет
единого стандарта вёрстки степпера — разные сайты (даже на одной и той же
CMS, вроде 1С-Битрикс) верстают его по-разному, единого CSS-класса не
существует. Три шага по нарастанию цены (design-обсуждение реального
кейса с живой выдачи: 7 из 7 кандидатов подряд не нашлись на шаге 1 —
слишком системно для случайности):
  1. `_find_stepper` — искать "+"-подобный кликабельный элемент, брать
     поле ввода количества как СОСЕДА по DOM (в одном родительском
     контейнере) — бесплатно, без LLM.
  2. `_try_add_to_cart_then_find_stepper` — если шаг 1 не сработал: у
     многих сайтов степпера физически нет в DOM до первого клика по
     "В корзину"/"Купить" (JS дорисовывает его после) — кликаем по
     типовой кнопке один раз, ищем степпер заново. Всё ещё без LLM,
     список кнопок — фиксированный (см. _ADD_TO_CART_SELECTORS).
  3. `_try_llm_guided_click_then_find_stepper` — если и шаг 2 не помог:
     последний, самый дорогой шаг — LLM (YandexGPT, та же инфраструктура,
     что и везде в проекте) смотрит на текстовый список кликабельных
     элементов страницы и сама решает, какой из них похож на "+"/"В
     корзину". Требует check_availability уже включённого LLM-провайдера
     — если он недоступен, шаг просто пропускается (тот же graceful
     degradation, что и везде в проекте), шаги 1-2 остаются рабочими и
     без LLM вовсе.

Если ничего из трёх шагов не помогло — результат None, "нет данных", не
выдумываем (тот же принцип, что и везде в scoring.py/availability.py).

Не все сайты вообще проверяют остаток на фронте (многие пускают вбить
любое число и отклоняют только на checkout) — отсутствие ошибки/потолка
НЕ означает "остатка точно хватит", это отдельный, более слабый сигнал,
чем явное число со страницы (см. StepperProbeResult.target_confirmed).

ЧТО ЭТОТ МОДУЛЬ НАМЕРЕННО НЕ ДЕЛАЕТ (design-обсуждение): только читает
состояние, никогда не завершает покупку. Каждый клик, включая шаг 3,
проверяет, не увёл ли он на другую страницу (см. url_before/url после
клика) — если увёл, останавливаемся, не продолжаем на чужой странице.
Дальше "положить в корзину" ничего не идёт: корзина не открывается,
заказ не оформляется, формы оплаты/доставки не трогаются. Это осознанное
ограничение по прямой просьбе пользователя, не техническое — идея
пойти дальше (реестр адаптеров, replay перехваченных запросов к
внутреннему API корзины, автоматическое оформление реальных заказов)
рассматривалась и отклонена: другой порядок риска (реальные деньги/
заказы у третьей стороны без её ведома) и другой масштаб инфраструктуры,
не то, что нужно для локального прототипа поиска поставщиков.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Безопасный потолок кликов — не более этого числа попыток на одного
# кандидата, даже если target_qty огромен (опечатка байера, "10000 шт") —
# страховка от зависания на одном сайте вместо честного "не подтвердилось".
_MAX_CLICKS = 200

# Селекторы "+"-подобного элемента — приоритетный список, первый матч
# побеждает. Явные классы (bx-touch-spin — типовой для 1С-Битрикс, самой
# распространённой CMS в рунете для B2B/промышленной торговли) идут
# первыми, текстовый fallback ("+" на любом кликабельном теге) — последним,
# он самый широкий и самый шумный.
_PLUS_SELECTORS = [
    ".bx-touch-spin .bx-spin-up",
    "[class*='quantity' i] [class*='plus' i]",
    "[class*='qty' i] [class*='plus' i]",
    "[class*='spin' i] [class*='up' i]",
    "[aria-label*='увелич' i]",
    "[aria-label*='plus' i]",
    "button:has-text('+')",
    "[role='button']:has-text('+')",
    "span:has-text('+')",
]

_INPUT_SELECTORS = [
    "input[type='number']",
    "input[type='text']",
    "input",
]

# Кнопка "добавить в корзину" — второй шаг эвристики (см.
# _try_add_to_cart_then_find_stepper): реальный вывод с живой выдачи —
# на многих сайтах степпера количества НЕТ в исходном DOM вообще, он
# появляется только ПОСЛЕ добавления товара в корзину (типовой UX:
# сначала одна кнопка "Купить"/"В корзину", степпер подрисовывается JS-ом
# только по клику). Порядок — от специфичных формулировок к общим,
# "Купить" последним: он же может означать "сразу оформить", а не просто
# "положить в корзину", у него выше риск не то что нужно.
#
# ВАЖНО: не только button/a — вторая живая находка (design-обсуждение):
# на реальных сайтах "кнопка" сплошь и рядом на самом деле <div>/<span> со
# своим обработчиком клика, стилизованный CSS в кнопку, а не семантический
# <button>. Первая версия списка ловила только button/a и поэтому не
# находила такие кнопки вообще — не то что не кликала мимо, а даже не
# видела кандидата. [onclick]/[role='button']/[class*='btn'|'button' i] —
# самые частые способы навесить кликабельность на не-семантический тег.
_ADD_TO_CART_SELECTORS = [
    "button:has-text('В корзину')",
    "a:has-text('В корзину')",
    "[onclick]:has-text('В корзину')",
    "[role='button']:has-text('В корзину')",
    "[class*='cart' i] button",
    "[class*='cart' i] a",
    "[class*='cart' i][onclick]",
    "button:has-text('Добавить в корзину')",
    "[onclick]:has-text('Добавить в корзину')",
    "button:has-text('Добавить')",
    "button:has-text('Купить')",
    "a:has-text('Купить')",
    "[onclick]:has-text('Купить')",
    "[role='button']:has-text('Купить')",
    "[class*='btn' i]:has-text('Купить')",
    "[class*='button' i]:has-text('Купить')",
]


@dataclass
class StepperProbeResult:
    """target_confirmed=True — довели поле до target_qty без явной ошибки
    и без остановки роста значения (слабый сигнал: страница просто не
    проверяет остаток на фронте могла быть причиной, не обязательно
    гарантия наличия). max_orderable — число, на котором рост
    остановился/появилась ошибка (если остановился раньше target_qty).
    evidence — текст ошибки, если она появилась на странице."""

    target_confirmed: bool
    max_orderable: float | None
    evidence: str | None


def _find_stepper(page):
    """Возвращает (plus_element, input_element) первой найденной пары или
    (None, None), если ни один из известных паттернов вёрстки не подошёл."""
    for selector in _PLUS_SELECTORS:
        try:
            plus_el = page.query_selector(selector)
        except Exception:
            continue
        if plus_el is None or not plus_el.is_visible():
            continue
        # Поле количества — сосед "+" по DOM: поднимаемся к ближайшему
        # разумному контейнеру (сам "+" почти никогда не родитель input,
        # оба — дети одного блока-степпера) и ищем input внутри него.
        container = plus_el.evaluate_handle(
            "el => el.closest('div, span, form') || el.parentElement"
        ).as_element()
        if container is None:
            continue
        for input_selector in _INPUT_SELECTORS:
            input_el = container.query_selector(input_selector)
            if input_el is not None and input_el.is_visible():
                return plus_el, input_el
    return None, None


def _try_add_to_cart_then_find_stepper(page):
    """Второй шаг эвристики (design-обсуждение реального кейса с живой
    выдачи: степпер не нашёлся НИ НА ОДНОМ из 7 сайтов подряд — слишком
    системно для случайности). Кликает по кнопке "В корзину"/"Купить" —
    ОДИН раз на первую подходящую — и ищет степпер заново на обновившемся
    DOM: типовой UX многих магазинов — степпер количества физически не
    существует в разметке до этого клика, JS дорисовывает его только
    после добавления в корзину.

    Безопасность: если клик увёл на другую страницу (URL изменился —
    признак того, что это была не "добавить в корзину" на месте, а
    переход в оформление заказа/корзину отдельной страницей) — НЕ
    продолжаем работать на чужой странице, сразу (None, None). Дальше
    клика по одной кнопке ничего не идёт: не открываем корзину, не
    оформляем заказ, не отправляем форм."""
    url_before = page.url
    for selector in _ADD_TO_CART_SELECTORS:
        try:
            btn = page.query_selector(selector)
        except Exception:
            continue
        if btn is None or not btn.is_visible():
            continue
        try:
            btn.click(timeout=2000)
        except Exception:
            continue
        page.wait_for_timeout(400)  # дать AJAX-добавлению в корзину отработать
        if page.url != url_before:
            return None, None
        plus_el, input_el = _find_stepper(page)
        if plus_el is not None and input_el is not None:
            return plus_el, input_el
    return None, None


# Кандидаты на LLM-выбор (шаг 3) — широкий охват тегов (в отличие от узких
# _PLUS_SELECTORS/_ADD_TO_CART_SELECTORS шагов 1-2, которые ищут ТОЛЬКО
# известные паттерны) — здесь наоборот: отдаём модели всё, что похоже на
# кликабельное, пусть сама решает, что из этого релевантно. [onclick]/
# [class*='btn'|'cart' i] добавлены по той же живой находке, что и в
# _ADD_TO_CART_SELECTORS — не-семантические <div>/<span>-"кнопки" иначе
# вообще не попадают в список, который видит модель (нельзя выбрать то,
# чего нет в списке кандидатов).
_LLM_CANDIDATE_SELECTOR = (
    "button, a, input, [role='button'], [onclick], [class*='btn' i], [class*='cart' i]"
)
# Урезаем список ДО отправки в LLM — не только ради цены запроса: длинный
# промпт с полусотней нерелевантных пунктов меню/футера/хлебных крошек
# статистически ухудшает точность выбора (модели труднее выделить нужный
# среди шума), не только дороже стоит. Поднят с 40 до 60 при расширении
# селектора выше — иначе более широкий охват тегов рисковал вытеснить из
# списка настоящую кнопку кандидатами из шапки/подвала раньше, чем она
# успевала попасть в первые 40 (список строится в порядке DOM, товарная
# карточка обычно не в самом начале документа).
_MAX_LLM_CANDIDATES = 60


def _collect_interactive_elements(page):
    """Возвращает (elements, descriptions) — elements[i] можно кликнуть
    напрямую (Playwright ElementHandle), descriptions[i] — его текстовое
    описание с тем же индексом i, для промпта LLM. Оба списка строятся из
    одних и тех же handle'ов за один проход — индексы гарантированно
    совпадают (в отличие от похода в JS через page.evaluate() и обратно,
    где пришлось бы отдельно сопоставлять результат с элементами)."""
    try:
        handles = page.query_selector_all(_LLM_CANDIDATE_SELECTOR)
    except Exception:
        return [], []

    elements = []
    descriptions = []
    for handle in handles:
        if len(elements) >= _MAX_LLM_CANDIDATES:
            break
        try:
            if not handle.is_visible():
                continue
            tag = handle.evaluate("el => el.tagName.toLowerCase()")
            text = (handle.inner_text() or "").strip()
            if not text:
                text = (
                    handle.get_attribute("aria-label")
                    or handle.get_attribute("value")
                    or handle.get_attribute("placeholder")
                    or ""
                ).strip()
        except Exception:
            continue
        elements.append(handle)
        descriptions.append(f"[{len(descriptions)}] <{tag}> {text[:60]!r}")
    return elements, descriptions


def _try_llm_guided_click_then_find_stepper(page, product_description: str | None):
    """Шаг 3 (последний, самый дорогой) — см. докстринг модуля. Требует
    YandexGPT (тот же провайдер, что и везде в проекте) — при недоступном
    пакете/ключе тихо пропускается (None, None), шаги 1-2 остаются рабочими
    без него. Тот же принцип защиты от навигации, что и в шаге 2 (см.
    _try_add_to_cart_then_find_stepper) — если клик увёл на другую
    страницу, останавливаемся, не продолжаем на чужой."""
    try:
        from procurement_search.yandexgpt_classifier import locate_cart_control_with_yandexgpt
    except ImportError:
        return None, None

    elements, descriptions = _collect_interactive_elements(page)
    if not elements:
        return None, None

    try:
        guess = locate_cart_control_with_yandexgpt(product_description or "", "\n".join(descriptions))
    except Exception:
        logger.info("_try_llm_guided_click_then_find_stepper: LLM-запрос не сработал", exc_info=True)
        return None, None

    if guess.element_index is None or not (0 <= guess.element_index < len(elements)):
        return None, None

    url_before = page.url
    try:
        elements[guess.element_index].click(timeout=2000)
    except Exception:
        return None, None
    page.wait_for_timeout(400)
    if page.url != url_before:
        return None, None
    return _find_stepper(page)


def _read_numeric_value(input_el) -> float | None:
    raw = (input_el.input_value() or "").strip().replace(",", ".")
    try:
        return float(raw)
    except ValueError:
        return None


def open_browser():
    """Поднимает playwright + headless Chromium для переиспользования между
    несколькими вызовами probe_max_orderable_quantity подряд (см.
    pipeline._refine_relevance) — запуск браузера сам по себе стоит
    секунды, на каждого кандидата это было бы на порядок дороже.

    Возвращает (playwright_context, browser) — ОБА должны быть закрыты
    вызывающим кодом (browser.close(), playwright_context.stop()), в
    finally, даже если ни один пробинг не вызывался успешно. Бросает
    ImportError, если пакет playwright не установлен — тот же сигнал,
    на который реагирует вызывающий код (см. pipeline.py)."""
    from playwright.sync_api import sync_playwright

    pw_context = sync_playwright().start()
    browser = pw_context.chromium.launch(headless=True)
    return pw_context, browser


def probe_max_orderable_quantity(
    url: str,
    target_qty: float,
    browser=None,
    timeout: float = 15.0,
    product_description: str | None = None,
) -> StepperProbeResult | None:
    """Открывает `url` в headless-браузере и пытается довести степпер
    количества до `target_qty` кликами по "+". None — playwright не
    установлен, страница не открылась, или степпер не найден ни одним из
    трёх шагов эвристики (см. докстринг модуля) — вызывающий код
    (availability.py/pipeline.py) не должен падать и не должен трактовать
    это как "товара нет", только как "не удалось уточнить".

    `browser` — уже запущенный playwright Browser для переиспользования
    между кандидатами одного поиска (запуск Chromium — самая дорогая часть,
    ~секунды; переиспользование экономит это на каждом следующем
    кандидате). Если не передан, поднимается и закрывается свой экземпляр
    — удобно для одиночного вызова/теста, но не для пайплайна на много
    кандидатов.

    `product_description` — нужен только шагу 3 (LLM-подсказка, какой
    товар ищем), шаги 1-2 его не используют. None — шаг 3 просто получит
    пустое описание товара в промпте, не откажет полностью."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.warning(
            "probe_max_orderable_quantity: пакет playwright не установлен "
            "(pip install playwright && playwright install chromium) — пробинг пропущен"
        )
        return None

    if browser is not None:
        return _probe_with_browser(browser, url, target_qty, timeout, product_description)

    try:
        with sync_playwright() as p:
            chromium = p.chromium.launch(headless=True)
            try:
                return _probe_with_browser(chromium, url, target_qty, timeout, product_description)
            finally:
                chromium.close()
    except Exception:
        logger.exception("probe_max_orderable_quantity: не удалось запустить браузер для %r", url)
        return None


def _probe_with_browser(
    browser, url: str, target_qty: float, timeout: float, product_description: str | None = None
) -> StepperProbeResult | None:
    page = browser.new_page()
    try:
        page.goto(url, timeout=timeout * 1000)
    except Exception as exc:
        logger.info("probe_max_orderable_quantity: не удалось открыть %r (%s)", url, exc)
        page.close()
        return None

    plus_el, input_el = _find_stepper(page)
    if plus_el is None or input_el is None:
        plus_el, input_el = _try_add_to_cart_then_find_stepper(page)
    if plus_el is None or input_el is None:
        plus_el, input_el = _try_llm_guided_click_then_find_stepper(page, product_description)
    if plus_el is None or input_el is None:
        logger.info("probe_max_orderable_quantity: степпер количества не найден на %r (все 3 шага)", url)
        page.close()
        return None

    max_clicks = min(int(target_qty) + 2, _MAX_CLICKS)
    last_value = _read_numeric_value(input_el)
    if last_value is None:
        page.close()
        return None

    evidence: str | None = None
    for _ in range(max_clicks):
        if last_value >= target_qty:
            break
        try:
            plus_el.click(timeout=2000)
        except Exception:
            break
        page.wait_for_timeout(150)  # дать AJAX-обработчику клика отработать
        new_value = _read_numeric_value(input_el)
        if new_value is None or new_value <= last_value:
            # Значение перестало расти — это и есть потолок, даже без
            # явного текста ошибки на странице (не все сайты его
            # показывают, но это не значит "ошибки не было").
            evidence = _find_visible_error_text(page)
            break
        last_value = new_value

    page.close()
    if last_value >= target_qty:
        return StepperProbeResult(target_confirmed=True, max_orderable=None, evidence=None)
    return StepperProbeResult(target_confirmed=False, max_orderable=last_value, evidence=evidence)


def _find_visible_error_text(page) -> str | None:
    """Best-effort: типовые классы всплывающих ошибок/уведомлений. Не
    гарантирован — многие сайты просто молча не дают полю расти дальше,
    без текста вообще; в этом случае возвращается None, а max_orderable
    (уже известный к этому моменту) остаётся единственным сигналом."""
    for selector in (
        "[class*='error' i]",
        "[id*='error' i]",
        "[class*='warning' i]",
        "[id*='warning' i]",
        "[role='alert']",
    ):
        try:
            el = page.query_selector(selector)
        except Exception:
            continue
        if el is not None and el.is_visible():
            text = el.inner_text().strip()
            if text:
                return text
    return None
