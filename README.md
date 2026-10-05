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

Единственный LLM-провайдер — `src/procurement_search/yandexgpt_classifier.py`:
[Yandex AI Studio](https://yandex.cloud/ru/services/ai-studio)
(Yandex Foundation Models), OpenAI-совместимый Chat Completions API
(`llm.api.cloud.yandex.net/v1`) поверх моделей YandexGPT Pro/Lite.

```bash
export YANDEX_FM_API_KEY=...   # API-ключ Yandex AI Studio (НЕ YANDEX_SEARCH_API_KEY — другой сервис)
export YANDEX_FM_MODEL=yandexgpt   # или yandexgpt-lite
export YANDEX_FOLDER_ID=...    # нужен, если ещё не задан для Yandex Search API выше
```

Авторизация — обычный статический API-ключ (Bearer) через стандартный
`openai` SDK, `response_format: json_schema` для структурированного
вывода. Модель адресуется не голым ID, а URI вида
`gpt://<FOLDER_ID>/<YANDEX_FM_MODEL>/latest`,
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

### Рассылка отчётов на почту и сбор обратной связи (опционально)

Каждый сформированный в веб-ГУИ Excel-отчёт (`/api/search` и `/api/photo-search`)
автоматически отправляется на почту через SMTP Yandex, а в самом письме есть
HTML-кнопки 👍/👎 — по клику сотрудник через ссылку `/api/feedback/submit`
попадает в форму оценки прямо из письма. Оценки и комментарии сохраняются в
записях истории отчётов (`index.json`) и видны в веб-ГУИ (кнопка «💬 Отзывы»).

Включается переменными окружения — код трогать не нужно:

```bash
export EMAIL_FROM=no-reply@company.ru        # аккаунт, с которого отправляем (SMTP-логин)
export EMAIL_FROM_PASSWORD=пароль-приложения  # пароль приложения, не основной! (см. ниже)
export EMAIL_TO=buyer@company.ru              # на какой адрес слать отчёты (можно список через запятую)
# Опционально:
export EMAIL_FROM_NAME="Systeme Electric Закупки"   # подпись/имя отправителя
export EMAIL_SMTP_HOST=smtp.yandex.ru               # по умолчанию Yandex
export EMAIL_SMTP_PORT=465                          # по умолчанию 465 (SSL); для 587 включается STARTTLS
export APP_BASE_URL=https://ваш-домен               # публичный адрес приложения — базовая часть ссылок оценки в письме
```

Пояснения:

- Для отправки через Яндекс используйте **пароль приложения** (а не основной
  пароль аккаунта), сгенерированный в [id.yandex.ru](https://id.yandex.ru/)
  → «Безопасность» → «Пароли приложений»; самому аккаунту нужно разрешить
  доступ по IMAP/SMTP (сервер `smtp.yandex.ru`, порт `465` SSL).
- `EMAIL_TO` может содержать несколько адресов, разделённых запятой.
- **Персональная рассылка**: в веб-ГУИ в форме поиска есть поле «Ваш email для
  отчёта». Если его заполнить — отчёт уйдёт именно на эту почту (удобно, когда
  каждый тестирующий хочет получить результат сразу на свой ящик), иначе — на
  `EMAIL_TO`. То же поле принимается в `/api/search` (поле `email`) и в
  `/api/photo-search` (multipart-поле `email`).
- `APP_BASE_URL` нужен, чтобы ссылки оценки в письме вели на ваш развёрнутый
  экземпляр (и `report_key` — это просто имя `.xlsx`, по нему отчёт находится
  в истории). Без `APP_BASE_URL` ссылки будут с относительным путём и в почте
  не откроются.
- Если почта не настроена (нет `EMAIL_FROM`/`EMAIL_FROM_PASSWORD`/`EMAIL_TO`)
  или отправка не удалась — поиск завершается нормально, просто не отправляется
  письмо (сбой почты не ломает отдачу результата, логируется в `logs/webapp.log`).

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

## Деплой (Docker)

`Dockerfile`/`docker-compose.yml` в корне репозитория — образ веб-GUI
(`webapp.py`), без `playwright`/Chromium по умолчанию (та же осознанная
граница, что и в `requirements.txt` — см. `--probe-stepper` выше, тяжёлая
опциональная зависимость, включается отдельным слоем сборки, закомментирован
в конце `Dockerfile`).

Локально проверено сборкой и запуском: `docker compose build && docker compose up -d`
поднимает контейнер, слушающий `0.0.0.0:8000` внутри (снаружи — порт, который
вы укажете в `docker-compose.yml`), с персистентными `./data`
(`trusted_suppliers.db`) и `./reports` (история Excel-отчётов) — оба смонтированы
как volume, переживают пересборку контейнера.

### Что нужно на сервере

```bash
git clone <репозиторий> && cd buyer_SE   # или git pull, если код уже там
```

Создать `.env` рядом с `docker-compose.yml` (тот же набор переменных, что и
для локального запуска — `YANDEX_SEARCH_API_KEY`, `YANDEX_FM_API_KEY` и т.д.,
см. разделы выше). Файл в `.gitignore`, в репозиторий не попадает — переносится
на сервер отдельно, руками.

```bash
docker compose up -d --build
```

Проверить:

```bash
docker compose ps                    # STATUS должен быть Up, не Restarting/Exited
curl http://localhost:8000/          # должен вернуть HTML главной страницы
```

Порт `8000` (левая часть `"8000:8000"` в `docker-compose.yml`) — тот самый
"свободный локальный порт", который дальше публикуется через reverse-proxy
(nginx/caddy/traefik) на HTTPS-домен; сама настройка reverse-proxy/сертификата
— уже инфраструктура сервера, вне этого репозитория.

**Известное ограничение при сборке на почти заполненном диске**: Docker
Desktop (да и любой Docker-демон) может повредить внутреннее состояние VM/
storage-driver при `no space left on device` в момент сборки — если после
такого сбоя `docker info`/сборка зависают без явной ошибки, помогает
перезапуск демона; если не помогает — сброс Docker к заводским настройкам
(Docker Desktop → Troubleshoot → Reset to factory defaults; на Linux-сервере
без Desktop — `docker system prune -a --volumes` или пересоздание storage-driver
директории), но это удаляет ВСЕ образы/контейнеры на машине, не только этот.

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
  llm_schemas.py                 — общие Pydantic-схемы structured output для LLM-провайдера
  yandexgpt_classifier.py       — опциональные LLM-проверки (атрибуты, бренд, релевантность...) через Yandex AI Studio
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
