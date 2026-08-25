# Procurement Search — прототип ИИ-поиска поставщиков (этап 1)

Прототип к [docs/design_doc.md](docs/design_doc.md) — техническому дизайн-документу
по ТЗ "ИИ в закупках (PP)". Документ объясняет архитектурные решения и явно
перечисляет ограничения прототипа (раздел §11) — прочитайте его в первую
очередь, прежде чем разбираться в коде.

## Что это и что нет

Работает без сети (юнит-тесты, дедуп, скоринг, экспорт в Excel). Реальный
поиск кандидатов требует хотя бы один из платных источников ниже —
Google CSE, Yandex Search API или Yandex gen-search (см. "Источники
кандидатов и ключи API").

pulscen.ru/optlist.ru (CatalogSource, CSS-селекторы) и DuckDuckGo убраны из
проекта (design-обсуждение): у первых двух селекторы в `config/sources.yaml`
так и остались неоткалиброванными PLACEHOLDER'ами с самого начала — среда, где
писался этот код, не имела сетевого доступа к этим сайтам (design_doc §11),
поэтому реальных кандидатов они не давали никогда; DuckDuckGo упёрся в
JS-антибот-челлендж на html.duckduckgo.com (anomaly.js) — недоступен без
браузера, исполняющего JavaScript.

## Установка

```bash
pip install -r requirements.txt
```

Пакет использует `src`-layout, тесты и CLI подхватывают `src/` через
`pyproject.toml` (`tool.pytest.ini_options.pythonpath`). Для запуска CLI вне
pytest добавьте `src` в `PYTHONPATH`:

### LLM-fallback для запроса (опционально)

`search_and_score(..., use_llm_fallback=True)` подключает LLM в двух точках
Слоя 0, поверх дешёвого детерминированного пути, а не вместо него:
- `attribute_extractor.py` — один batch-вызов на все "голые" числа запроса,
  для которых словарь `config/units.yaml` не нашёл единицы измерения рядом
  (опечатки/сокращения/склонения — "киловат" вместо "киловатт");
- `brand_extractor.py` — извлекает бренд/производителя из запроса, если он
  явно упомянут, для отдельного целевого поискового запроса.

По умолчанию выключен и не требует ни сети, ни ключа.

Провайдер по умолчанию — `LLM_PROVIDER=yandexgpt`
(`src/procurement_search/yandexgpt_classifier.py`): [Yandex AI Studio](https://yandex.cloud/ru/services/ai-studio)
(Yandex Foundation Models), OpenAI-совместимый Chat Completions API
(`llm.api.cloud.yandex.net/v1`) поверх моделей YandexGPT Pro/Lite.

```bash
export LLM_PROVIDER=yandexgpt
export YANDEX_FM_API_KEY=...   # API-ключ Yandex AI Studio (НЕ YANDEX_SEARCH_API_KEY — другой сервис)
export YANDEX_FM_MODEL=yandexgpt   # или yandexgpt-lite
export YANDEX_FOLDER_ID=...    # нужен, если ещё не задан для Yandex Search API выше
```

Авторизация — обычный статический API-ключ (Bearer) через стандартный
`openai` SDK, `response_format: json_schema` для структурированного
вывода — тот же контракт, что и у cloud.ru ниже. Модель адресуется не
голым ID, а URI вида `gpt://<FOLDER_ID>/<YANDEX_FM_MODEL>/latest`,
который `yandexgpt_classifier.py` собирает сам из `YANDEX_FM_MODEL` +
folder_id. Получить ключ: [Yandex AI Studio](https://yandex.cloud/ru/services/ai-studio)
→ API-ключи → создать ключ.

**Ключ Yandex AI Studio из другого аккаунта, чем `YANDEX_SEARCH_API_KEY`?**
Search API и AI Studio — независимые сервисы Yandex Cloud, ничто не
мешает завести их в разных аккаунтах/каталогах. По умолчанию folder_id
для модели берётся из уже заданного `YANDEX_FOLDER_ID` (удобно для
типового случая "один аккаунт на всё") — но если у AI Studio реально
другой аккаунт, его folder_id туда не подойдёт (ошибка авторизации, как
у 403 "Project not found" у cloud.ru). Для этого случая — отдельная
переменная:

```bash
export YANDEX_FM_FOLDER_ID=...  # folder_id именно того аккаунта, где выпущен YANDEX_FM_API_KEY
```

**Запасной вариант на случай отката** — `LLM_PROVIDER=cloudru`
(`src/procurement_search/cloudru_classifier.py`):
[Cloud.ru Evolution Foundation Models](https://cloud.ru/products/evolution-foundation-models),
OpenAI-совместимый API поверх 20+ моделей (GLM, Qwen, DeepSeek, MiniMax,
GigaChat), регистрация без VPN и зарубежных карт, оплата по факту
использования.

```bash
export LLM_PROVIDER=cloudru
export CLOUDRU_API_KEY=...     # Key Secret из API-ключа (см. ниже, откуда взять)
export CLOUDRU_MODEL=...       # точный ID модели из каталога Cloud.ru
```

Авторизация — обычный статический API-ключ (Bearer) —
никакого обмена на IAM-токен не требуется, несмотря на то что так
выглядело по README пакета `evolution-openai`, который сначала
рассматривался для этой интеграции: на практике связка `key_id`/`secret`
через IAM (`evolution-openai`) отдала `401 Unauthorized`, а обычный
`openai` SDK с ключом из карточки модели — сработал. Получить ключ:
Evolution → Foundation Models → карточка нужной модели → "Использовать" →
создать API-ключ, оттуда взять Key Secret.

Регистрация без VPN и зарубежных карт — но сам API
(`foundation-models.api.cloud.ru`) может быть недоступен, если на вашей
машине включён VPN/прокси с выходом за пределы РФ: наблюдалось вживую —
`SSLEOFError`/обрыв TLS-рукопожатия при активном VPN (V2Ray/Xray-клиенты
вроде V2Box — популярный случай), нормальная работа сразу после его
отключения.

### Резолвинг в ЕГРЮЛ через Dadata — отсев неактуальных данных (опционально)

По умолчанию используется `NullEnricher` — карточки собираются напрямую из
того, что нашли источники, без проверки по реестру. `DadataEnricher`
(`src/procurement_search/enrichment.py`) резолвит компанию по названию
через Dadata suggest API и закрывает требование ТЗ п.4 "Отсев неактуальных
данных":

- **исключает ликвидированные компании** — `pipeline.py` жёстко убирает их
  из финальной выдачи (`DEAD_COMPANY_STATUSES`), а не просто занижает в
  скоринге;
- **обрабатывает смену названия** — в отчёте показывается ТЕКУЩЕЕ
  официальное название из ЕГРЮЛ, а не устаревшее имя со скрапинга;
- **добавляет проверенный юридический адрес** из ЕГРЮЛ с флагом
  "подтверждён", не подменяя адрес со скрапинга.

Включается переменной окружения — код трогать не нужно:

```bash
export DADATA_API_KEY=ваш-ключ
```

Ключ бесплатный — регистрация на [dadata.ru](https://dadata.ru), тариф
suggest API имеет дневной бесплатный лимит (тысячи запросов/день, точный
лимит проверьте в личном кабинете). Без переменной — по умолчанию
`NullEnricher`, как раньше, ничего не ломается.

**Известное ограничение:** матчинг по названию (не по ИНН) — для общих
названий вроде "Ромашка" возможны ложные срабатывания. Частично
компенсируется выбором варианта, чей адрес из ЕГРЮЛ ближе всего к адресу
со скрапинга (design_doc §11).

### Контроль актуальности контактов (включено по умолчанию)

`verify_contacts.py` делает HEAD-запрос на сайт каждой найденной компании
и помечает результат в колонке "Сайт": "подтверждён" (сайт отвечает — даже
кодом ошибки, это всё ещё живой домен) или "протух" (домен не резолвится,
соединение отклонено, таймаут). Бесплатно, без ключей, работает всегда —
отключить можно параметром `verify_websites=False` в `search_and_score()`
(например, для скорости в офлайн-тестах).

```bash
PYTHONPATH=src python3 -m procurement_search.cli --query "гальванические покрытия" --output out.xlsx --verbose
```

### Глобальный поиск и зарубежные источники (опционально)

Для ТЗ п.4 "работать по зарубежным источникам (в т.ч. Alibaba)" и общего
"поиска по всему интернету" используется `sources/google_cse.py` — Google
Custom Search JSON API, официальный канал, не подверженный капчам/антибот-
защите (в отличие от скрапинга — см. выше, почему pulscen.ru/optlist.ru/
DuckDuckGo убраны из проекта).

Включается переменными окружения — код трогать не нужно:

```bash
export GOOGLE_CSE_API_KEY=ваш-ключ
export GOOGLE_CSE_CX=ваш-search-engine-id
```

Обе бесплатные, без привязки карты (лимит — 100 запросов/день):

1. API-ключ: [console.cloud.google.com](https://console.cloud.google.com) →
   APIs & Services → Credentials → Create API key (включить Custom Search API
   в библиотеке API).
2. Search Engine ID (`cx`): [programmablesearchengine.google.com](https://programmablesearchengine.google.com)
   → создать поисковую систему → включить "Search the entire web" → скопировать
   Search engine ID.

Без переменных источник просто отсутствует в выдаче (лог: "GOOGLE_CSE_API_KEY/
GOOGLE_CSE_CX не заданы") — остальные источники продолжают работать как раньше.

**Известное ограничение:** на практике Google Cloud billing на части аккаунтов
требует привязать карту для верификации (даже без автосписаний) прежде чем
API-ключ реально заработает — см. `sources/yandex_search.py` про
альтернативу, если карта недоступна.

### Yandex Search API — альтернатива для RU-рынка (платно, принимает RU-карты)

Если карта для Google CSE недоступна (частая история для российских
аккаунтов — международные платёжные системы не принимают российские карты
на Google Cloud billing) — `sources/yandex_search.py` даёт тот же канал
"поиск по всему интернету" через Yandex Cloud. Свободного тарифа нет
(старый бесплатный Yandex.XML закрыт), но сервис российский и принимает
российские карты без проблем; для RU-рынка это к тому же более уместный
источник, чем Google.

```bash
export YANDEX_SEARCH_API_KEY=ваш-ключ
export YANDEX_FOLDER_ID=ваш-folder-id
```

Получение:
1. Зарегистрируйтесь на [console.yandex.cloud](https://console.yandex.cloud), создайте каталог (folder) — его ID и есть `YANDEX_FOLDER_ID`.
2. В каталоге подключите платёжный аккаунт (billing) — понадобится карта.
3. В сервисе **Search API** (или через **IAM → Сервисные аккаунты**) создайте API-ключ с ролью, дающей доступ к Search API — скопируйте значение как `YANDEX_SEARCH_API_KEY`.

**Известное ограничение:** этот модуль написан по документации Yandex Cloud
Search API без доступа к реальному ключу для живой проверки (формат ответа
у Yandex — асинхронная операция, отдающая base64-XML, а не прямой JSON, как
у Google) — при первом реальном запуске стоит свериться с фактическим
ответом API и поправить `_parse_xml` в `sources/yandex_search.py`, если
структура полей разойдётся с ожидаемой.

## Веб-GUI

Локальный веб-интерфейс поверх того же пайплайна — поле поиска, таблица
результатов с цветовой разметкой достоверности контактов (как в Excel:
зелёный/жёлтый/красный) и история отчётов со скачиванием `.xlsx`. Бэкенд —
FastAPI (`src/procurement_search/webapp.py`), фронтенд — один статический
файл без сборки (`static/index.html`, ванильный JS + `fetch`).

```bash
PYTHONPATH=src python3 -m procurement_search.webapp
```

Откройте `http://127.0.0.1:8000` в браузере. Каждый поиск сохраняет Excel
в `reports/` (в `.gitignore`, в репозиторий не попадает) и добавляет запись
в историю; `GET /api/reports` и `GET /api/reports/{filename}` — тот же
API, которым пользуется страница, можно дёргать и напрямую (например, из
другого внутреннего инструмента байеров).

**Тот же сетевой нюанс, что и у CLI**: если ни один из платных источников
(Google CSE/Yandex Search/Yandex gen-search) не настроен или недоступен из
этой сети, поиск в GUI вернёт 0 компаний, но не упадёт — увидите пустую
таблицу и предупреждение в консоли сервера.

## Тесты

```bash
pytest -q
```

Все тесты работают без сети — источники в тестах замоканы
(`tests/test_pipeline.py`, `tests/test_webapp.py`); оба LLM-провайдера,
Dadata suggest API (`tests/test_dadata_enricher.py`) и проверка живости
сайта (`tests/test_verify_contacts.py`) — тоже замоканы.

## Структура

```
docs/design_doc.md              — технический дизайн-документ (архитектура, источники, риски)
config/sources.yaml             — конфиг источников (google_cse/yandex_search/yandex_gen_search)
config/scoring_weights.yaml     — веса скоринга (профиль "default")
src/procurement_search/
  models.py                     — Candidate / Company / FieldValue
  query_normalizer.py           — шаг [1]: обёртка raw_query -> NormalizedQuery
  attribute_extractor.py        — Слой 0: числовые атрибуты запроса (единицы измерения из units.yaml)
  brand_extractor.py            — Слой 0: бренд/производитель из запроса (LLM, опционально)
  sources/base.py               — общая HTTP-инфраструктура источников: robots.txt, fetch_url(), regex-контактов
  sources/google_cse.py         — Google Custom Search JSON API (зарубежные источники, ТЗ п.4)
  sources/yandex_search.py      — Yandex Search API (альтернатива для RU-рынка)
  sources/yandex_gen_search.py  — генеративный ответ Yandex Search API (платно, опционально)
  dedup.py                      — шаг [5]: дедупликация по телефону/домену/имени
  enrichment.py                 — шаги [3]-[4]: NullEnricher (заглушка) и DadataEnricher (резолвинг в ЕГРЮЛ)
  verify_contacts.py            — контроль актуальности контактов: HTTP-проверка живости сайта
  scoring.py                    — шаг [6]: скоринг (релевантность/масштаб/надёжность)
  export.py                     — шаг [7]: экспорт в Excel с флагами достоверности
  llm_schemas.py                 — общие Pydantic-схемы structured output для LLM-провайдеров
  yandexgpt_classifier.py       — опциональные LLM-проверки (атрибуты, бренд, релевантность...) через Yandex AI Studio (боевой провайдер по умолчанию, LLM_PROVIDER=yandexgpt)
  cloudru_classifier.py         — те же проверки через Cloud.ru Foundation Models (запасной вариант, LLM_PROVIDER=cloudru)
  pipeline.py, cli.py           — оркестрация (в т.ч. отсев ликвидированных компаний), CLI
  webapp.py                     — FastAPI-бэкенд веб-GUI (/api/search, /api/reports)
static/index.html               — фронтенд веб-GUI (ванильный JS, без сборки)
reports/                        — сгенерированные Excel-отчёты + история (в .gitignore)
tests/                          — тесты, без сети
```

## Что делать дальше (по приоритету)

Порядок соответствует design_doc.md §11 — без этих шагов прототип не
заменяет ручной ресерч байера, только ускоряет его первый черновик:

1. Настроить хотя бы один платный источник (Google CSE или Yandex Search
   API, см. выше) — без ключей реальных кандидатов нет вообще.
2. Получить бесплатный `DADATA_API_KEY` и прогнать реальный поиск — без
   него `DadataEnricher` существует в коде, но не используется (сработает
   молчаливый `NullEnricher`).
3. Собрать golden set по 4 тестовым запросам из ТЗ и прогнать baseline через
   JayCopilot — без этого нечего сравнивать (design_doc §9).
4. При росте объёма — оценить платный тариф Dadata или переход на
   Контур.Фокус/СПАРК (бесплатный лимит suggest API ограничен, design_doc
   §11).
