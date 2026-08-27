"""Загрузка YAML-конфигов проекта."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


def _load_yaml(name: str) -> dict[str, Any]:
    path = CONFIG_DIR / name
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_sources_config() -> dict[str, Any]:
    return _load_yaml("sources.yaml")


def load_scoring_weights() -> dict[str, Any]:
    return _load_yaml("scoring_weights.yaml")


def load_units() -> dict[str, Any]:
    return _load_yaml("units.yaml")


def load_marketplace_domains() -> list[str]:
    path = CONFIG_DIR / "marketplace_domains.yaml"
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or []


def load_categories() -> dict[str, Any]:
    return _load_yaml("categories.yaml")
