"""CLI-точка входа: python -m procurement_search.cli --query "..." --output out.xlsx"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from dotenv import load_dotenv

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
    parser.add_argument("--query", required=True, help="Запрос байера, напр. 'гальванические покрытия'")
    parser.add_argument("--output", default="suppliers.xlsx", help="Путь к выходному Excel-файлу")
    parser.add_argument("--verbose", action="store_true", help="Подробные логи")
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
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    output = run_pipeline(
        args.query,
        args.output,
        deep_relevance=args.deep_relevance,
        relevance_llm_check=args.relevance_llm_check,
        use_trusted_suppliers=args.use_trusted_suppliers,
    )
    print(f"Готово: {output}")


if __name__ == "__main__":
    main()
