"""Тесты TrustedSupplierStore (trusted_suppliers.py) — SQLite в
временном файле на каждый тест, без сети."""

from procurement_search.trusted_suppliers import TrustedSupplierStore


def test_domains_for_category_empty_when_nothing_recorded(tmp_path):
    with TrustedSupplierStore(tmp_path / "test.db") as store:
        assert store.domains_for_category("F3") == []


def test_record_supplier_makes_domain_findable_by_category(tmp_path):
    with TrustedSupplierStore(tmp_path / "test.db") as store:
        store.record_supplier(
            domain="huterrussia.ru",
            name="Генератор бензиновый Huter DY3000L",
            category_code="F3",
            rank=1,
            source_query="генератор бензиновый",
        )

        assert store.domains_for_category("F3") == ["huterrussia.ru"]
        assert store.domains_for_category("K1") == []


def test_record_supplier_upserts_and_does_not_clobber_inn_with_none(tmp_path):
    """Второй прогон того же домена без ИНН не должен затирать уже
    известный ИНН из первого прогона — COALESCE в record_supplier."""
    with TrustedSupplierStore(tmp_path / "test.db") as store:
        store.record_supplier(
            domain="huterrussia.ru",
            name="Генератор бензиновый Huter DY3000L",
            category_code="F3",
            rank=1,
            source_query="генератор бензиновый",
            inn="7700000000",
        )
        store.record_supplier(
            domain="huterrussia.ru",
            name="Купить генераторы Huter — официальный сайт",
            category_code="F3",
            rank=2,
            source_query="генератор huter купить",
        )

        card = store.get_supplier("huterrussia.ru")
        assert card["inn"] == "7700000000"  # не затёрлось None-ом
        assert card["name"] == "Купить генераторы Huter — официальный сайт"  # имя обновилось


def test_record_supplier_keeps_full_history_not_just_latest(tmp_path):
    """category_suppliers — журнал (append-only): один и тот же домен,
    записанный дважды под одной категорией, не схлопывается в одну
    строку — это специально, чтобы потом можно было проверить, на каком
    ранге домен реально появлялся исторически (design-обсуждение: "топ-1
    не позволяет узнать, действительно ли байер выбирал первый вариант")."""
    with TrustedSupplierStore(tmp_path / "test.db") as store:
        store.record_supplier(
            domain="huterrussia.ru", name="A", category_code="F3", rank=1, source_query="q1"
        )
        store.record_supplier(
            domain="huterrussia.ru", name="A", category_code="F3", rank=2, source_query="q2"
        )

        cursor = store._conn.execute(
            "SELECT rank, source_query FROM category_suppliers WHERE domain = ? ORDER BY id",
            ("huterrussia.ru",),
        )
        rows = cursor.fetchall()
        assert rows == [(1, "q1"), (2, "q2")]


def test_domains_for_category_deduplicates_repeated_domain(tmp_path):
    with TrustedSupplierStore(tmp_path / "test.db") as store:
        store.record_supplier(
            domain="huterrussia.ru", name="A", category_code="F3", rank=1, source_query="q1"
        )
        store.record_supplier(
            domain="huterrussia.ru", name="A", category_code="F3", rank=3, source_query="q2"
        )

        assert store.domains_for_category("F3") == ["huterrussia.ru"]


def test_get_supplier_returns_none_for_unknown_domain(tmp_path):
    with TrustedSupplierStore(tmp_path / "test.db") as store:
        assert store.get_supplier("unknown.example") is None


def test_store_persists_across_reopen(tmp_path):
    db_path = tmp_path / "test.db"
    with TrustedSupplierStore(db_path) as store:
        store.record_supplier(
            domain="huterrussia.ru", name="A", category_code="F3", rank=1, source_query="q1"
        )

    with TrustedSupplierStore(db_path) as reopened:
        assert reopened.domains_for_category("F3") == ["huterrussia.ru"]
