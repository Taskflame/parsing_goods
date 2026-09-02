# Базовый образ веб-GUI (webapp.py) БЕЗ playwright/chromium — та же
# осознанная граница, что и в requirements.txt: пилот --probe-stepper
# тяжёлая опциональная зависимость (~275 МБ браузерного бинарника),
# не нужна большинству запусков. Если он понадобится в проде — см.
# закомментированный блок ниже, добавьте его в отдельный слой сборки.
FROM python:3.13-slim

WORKDIR /app

# Сначала только requirements.txt — слой с зависимостями кешируется
# Docker'ом отдельно от кода, пересборка при правке кода не переустанавливает
# пакеты заново.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY config/ ./config/
COPY src/ ./src/
COPY static/ ./static/

ENV PYTHONPATH=/app/src
# 0.0.0.0 обязателен внутри контейнера — см. webapp.py про то, почему
# 127.0.0.1 здесь не сработал бы даже с проброшенным портом.
ENV HOST=0.0.0.0
ENV PORT=8000

# data/ (trusted_suppliers.db) и reports/ (история Excel-отчётов) —
# персистентные каталоги, монтируются как volume в docker-compose.yml,
# не часть образа. Создаём здесь только чтобы приложение не упало, если
# volume ещё не примонтирован при самом первом запуске.
RUN mkdir -p data reports

EXPOSE 8000

CMD ["python3", "-m", "procurement_search.webapp"]

# --- Если понадобится --probe-stepper (headless-браузер) в проде ---
# RUN pip install --no-cache-dir playwright>=1.40 \
#     && playwright install --with-deps chromium
# (добавляет системные библиотеки для рендеринга + сам Chromium — образ
# заметно больше и дольше собирается, поэтому не включено по умолчанию)
