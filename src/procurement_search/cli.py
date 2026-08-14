"""CLI-точка входа: python -m procurement_search.cli --query "..." --output out.xlsx"""

from __future__ import annotations

import argparse
import logging

from procurement_search.pipeline import run_pipeline


def main() -> None:
    parser = argparse.ArgumentParser(description="Поиск поставщиков по текстовому запросу")
    parser.add_argument("--query", required=True, help="Запрос байера, напр. 'гальванические покрытия'")
    parser.add_argument("--output", default="suppliers.xlsx", help="Путь к выходному Excel-файлу")
    parser.add_argument("--verbose", action="store_true", help="Подробные логи")
    parser.add_argument(
        "--deep-relevance",
        action="store_true",
        help="Слой 2: краулить сайты top-N кандидатов и уточнять релевантность по реальному тексту (медленнее)",
    )
    parser.add_argument(
        "--relevance-llm-check",
        action="store_true",
        help="Слой 3: точечная LLM-проверка релевантности поверх --deep-relevance (нужен LLM_PROVIDER)",
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
    )
    print(f"Готово: {output}")


if __name__ == "__main__":
    main()
