"""CLI-точка входа: python -m procurement_search.cli --query "..." --output out.xlsx"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from dotenv import load_dotenv

from procurement_search.log_explainer import ExplainingLogHandler
from procurement_search.pipeline import run_pipeline

# Путь к .env вычисляется от расположения этого файла, а не через
# find_dotenv() по умолчанию (тот ищет по стеку вызовов/CWD — на практике
# ненадёжно: под `python -m` из разных терминалов иногда не находит файл,
# который лежит прямо в корне проекта). parents[2]: cli.py ->
# procurement_search -> src -> корень репозитория.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def main() -> None:
    # Подхватывает .env из корня проекта, если он есть —
    # явный `export` в шелле всё равно имеет приоритет (override=False по
    # умолчанию), .env только подставляет то, что ещё не задано.
    load_dotenv(_PROJECT_ROOT / ".env")

    parser = argparse.ArgumentParser(description="Поиск поставщиков по текстовому запросу")
    parser.add_argument(
        "--query",
        default=None,
        help="Запрос байера, напр. 'гальванические покрытия' (не требуется при --feedback-report)",
    )
    parser.add_argument("--output", default="suppliers.xlsx", help="Путь к выходному Excel-файлу")
    parser.add_argument("--verbose", action="store_true", help="Подробные логи")
    parser.add_argument(
        "--feedback-report",
        action="store_true",
        help=(
            "Режим отчёта по отзывам: собрать ВСЕ реакции/отзывы из feedback.db "
            "в Excel (лист 'Все отзывы' + 'Сводка по неделям'). Не требует --query. "
            "Опции --since/--until ограничивают диапазон дат (ISO)."
        ),
    )
    parser.add_argument(
        "--send-feedback-report",
        action="store_true",
        help=(
            "Собрать понедельничный отчёт по отзывам за ПРОШЛУЮ неделю и отправить "
            "на почту через рассылочную учётку проекта (EMAIL_FROM в .env; получатель — "
            "EMAIL_TO или aleksandr.smurov@systeme.ru). Не требует --query."
        ),
    )
    parser.add_argument(
        "--since",
        default=None,
        help="Для --feedback-report: включать отзывы с этой ISO-даты (например 2026-09-21)",
    )
    parser.add_argument(
        "--until",
        default=None,
        help="Для --feedback-report: включать отзывы по эту ISO-дату включительно",
    )
    parser.add_argument(
        "--deep-relevance",
        action="store_true",
        help=(
            "Слой 2: краулить сайты top-N кандидатов, уточнять релевантность по реальному "
            "тексту и сортировать выдачу по найденной цене товара (медленнее)"
        ),
    )
    parser.add_argument(
        "--relevance-llm-check",
        action="store_true",
        help="Слой 3: точечная LLM-проверка релевантности поверх --deep-relevance (нужен YANDEX_FM_API_KEY)",
    )
    parser.add_argument(
        "--use-trusted-suppliers",
        action="store_true",
        help=(
            "Слой 0: сначала искать среди уже проверенных поставщиков категории запроса "
            "(trusted_suppliers.py), глобальный поиск — только если их не хватило "
            "(нужен YANDEX_FM_API_KEY)"
        ),
    )
    parser.add_argument(
        "--check-availability",
        action="store_true",
        help=(
            "Слой 4: проверять остаток товара и сравнивать с запрошенным количеством "
            "(требует --deep-relevance и YANDEX_FM_API_KEY, отдельная от --relevance-llm-check "
            "и более дорогая LLM-проверка)"
        ),
    )
    parser.add_argument(
        "--probe-stepper",
        action="store_true",
        help=(
            "ПИЛОТ: интерактивная проверка остатка через степпер количества на странице "
            "(headless-браузер, требует --check-availability и `pip install playwright && "
            "playwright install chromium`) — эвристика, не гарантия, см. stepper_probe.py. "
            "Заметно медленнее (реальный браузер на кандидата), запускается только для "
            "кандидатов с неясным вердиктом Слоя 4"
        ),
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Вариант А (design-обсуждение) — детерминированный объяснитель
    # известных ошибок, без LLM (см. log_explainer.py). Всегда включён —
    # бесплатный, ничего не ломает, ничего не отправляет по сети.
    logging.getLogger().addHandler(ExplainingLogHandler())

    if args.send_feedback_report:
        from procurement_search.feedback_report import send_weekly_feedback_report

        if args.query:
            parser.error("--query не используется вместе с --send-feedback-report")
        sent = send_weekly_feedback_report()
        if sent:
            print("Понедельничный отчёт по отзывам отправлен на почту.")
        else:
            print(
                "Отчёт по отзывам НЕ отправлен: почта не настроена (EMAIL_FROM/PASSWORD) "
                "или сбой отправки. Подробности — в логах."
            )
        return

    if args.feedback_report:
        from procurement_search.feedback_report import build_feedback_report

        if args.query:
            parser.error("--query не используется вместе с --feedback-report")
        output = build_feedback_report(
            args.output,
            since=args.since,
            until=args.until,
        )
        print(f"Отчёт по отзывам сохранён: {output}")
        return

    if not args.query:
        parser.error("--query обязателен (или используйте --feedback-report)")

    output = run_pipeline(
        args.query,
        args.output,
        deep_relevance=args.deep_relevance,
        relevance_llm_check=args.relevance_llm_check,
        use_trusted_suppliers=args.use_trusted_suppliers,
        check_availability=args.check_availability,
        probe_stepper=args.probe_stepper,
    )
    print(f"Готово: {output}")


if __name__ == "__main__":
    main()
