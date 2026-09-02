"""Тесты веб-API (без сети): источники подменяются, как в test_pipeline.py."""

from fastapi.testclient import TestClient

from procurement_search import pipeline, webapp
from procurement_search.models import Candidate, VerificationFlag
from procurement_search.trusted_suppliers import TrustedSupplierStore


class _FakeSource:
    """См. test_pipeline.py._FakeSource — pulscen.ru/optlist.ru/DuckDuckGo
    убраны из активных источников, тесты подменяют build_yandex_search."""

    def __init__(self, name: str, search_fn):
        self.name = name
        self._search_fn = search_fn

    def search(self, query: str) -> list[Candidate]:
        return self._search_fn(query)


def _patch_sources(monkeypatch, search_fn, *, name: str = "yandex_search") -> None:
    monkeypatch.setattr(pipeline, "build_yandex_search", lambda cfg: _FakeSource(name, search_fn))
    monkeypatch.setattr(pipeline, "build_google_cse", lambda cfg: None)
    monkeypatch.setattr(pipeline, "build_yandex_gen_search", lambda cfg: None)


def _fake_candidates(source_name: str) -> list[Candidate]:
    return [
        Candidate(
            source=source_name,
            source_url=f"https://{source_name}.example/company/1",
            name_raw="ООО Гальванические покрытия",
            phone_raw="+7 900 111 11 11",
            email_raw="info@galvanika.ru",
            address_raw="г. Москва",
            description_raw="цинкование хромирование",
        ),
    ]


def _client(monkeypatch, tmp_path) -> TestClient:
    monkeypatch.setattr(webapp, "REPORTS_DIR", tmp_path / "reports")
    monkeypatch.setattr(webapp, "INDEX_PATH", tmp_path / "reports" / "index.json")
    _patch_sources(monkeypatch, lambda query: _fake_candidates("yandex_search"))
    return TestClient(webapp.app)


def test_search_returns_companies_and_creates_report(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)

    resp = client.post("/api/search", json={"query": "гальванические покрытия"})

    assert resp.status_code == 200
    data = resp.json()
    assert len(data["companies"]) == 1
    assert data["companies"][0]["name"] == "ООО Гальванические покрытия"
    assert data["companies"][0]["phone"]["value"] == "+7 900 111 11 11"
    assert data["companies"][0]["phone"]["confidence"] == "не проверен"
    assert data["companies"][0]["stock_status"] == "не проверено"
    assert data["report"]["query"] == "гальванические покрытия"
    assert (tmp_path / "reports" / data["report"]["filename"]).exists()


def test_empty_query_returns_400(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)

    resp = client.post("/api/search", json={"query": "   "})

    assert resp.status_code == 400


def test_reports_list_and_download_roundtrip(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)

    search_resp = client.post("/api/search", json={"query": "гальванические покрытия"})
    filename = search_resp.json()["report"]["filename"]

    list_resp = client.get("/api/reports")
    assert list_resp.status_code == 200
    assert any(r["filename"] == filename for r in list_resp.json())

    download_resp = client.get(f"/api/reports/{filename}")
    assert download_resp.status_code == 200
    assert download_resp.content[:2] == b"PK"  # сигнатура zip/xlsx


def test_download_rejects_path_traversal(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)

    resp = client.get("/api/reports/..%2F..%2Fetc%2Fpasswd")

    assert resp.status_code == 404


def test_search_reports_required_quantity_and_availability_summary(monkeypatch, tmp_path):
    """check_availability=False (по умолчанию) — required_quantity/
    availability_summary всё равно вычисляются из Слоя 0.6 (order_qty
    распознаётся детерминированно, без LLM), а сама сводка по остаткам —
    None, потому что ни у одной компании ещё нет company.availability."""
    client = _client(monkeypatch, tmp_path)

    resp = client.post("/api/search", json={"query": "цинкование 11 шт"})

    assert resp.status_code == 200
    data = resp.json()
    assert data["required_quantity"] == "11 шт"
    assert data["availability_summary"] is None
    assert data["companies"][0]["availability"] is None
    assert data["companies"][0]["availability_verdict"] is None


def test_search_includes_website_liveness_field(monkeypatch, tmp_path):
    def fake_candidates_with_website(query: str) -> list[Candidate]:
        return [
            Candidate(
                source="yandex_search",
                source_url="https://yandex_search.example/company/1",
                name_raw="ООО Гальванические покрытия",
                website="https://galvanika.ru",
            ),
        ]

    monkeypatch.setattr(webapp, "REPORTS_DIR", tmp_path / "reports")
    monkeypatch.setattr(webapp, "INDEX_PATH", tmp_path / "reports" / "index.json")
    _patch_sources(monkeypatch, fake_candidates_with_website)
    monkeypatch.setattr(
        pipeline, "check_website_liveness", lambda url, **kwargs: VerificationFlag.CONFIRMED
    )

    client = TestClient(webapp.app)
    resp = client.post("/api/search", json={"query": "гальванические покрытия"})

    assert resp.status_code == 200
    website = resp.json()["companies"][0]["website"]
    assert website["value"] == "https://galvanika.ru"
    assert website["confidence"] == "подтверждён"


# --- /api/trusted-suppliers — просмотр + удаление БД доверенных поставщиков ---


def test_list_trusted_suppliers_groups_by_category_with_readable_name(monkeypatch, tmp_path):
    """Ответ сгруппирован по категории (см. TrustedSupplierStore.list_by_category)
    — под древовидный UI, не плоский список — и обогащён человекочитаемым
    category_name из config/categories.yaml (LLM/байер видят код "F3" не
    напрямую, а с названием рядом)."""
    from procurement_search.config import load_categories

    db_path = tmp_path / "trusted.db"
    monkeypatch.setattr(webapp, "TrustedSupplierStore", lambda *a, **k: TrustedSupplierStore(db_path))
    with TrustedSupplierStore(db_path) as store:
        store.record_supplier(
            domain="huterrussia.ru", name="Huter", category_code="F3", rank=1, source_query="q"
        )
        store.record_supplier(
            domain="clasta.ru", name="Clasta", category_code="E1", rank=1, source_query="q"
        )

    client = TestClient(webapp.app)
    resp = client.get("/api/trusted-suppliers")

    assert resp.status_code == 200
    groups = resp.json()
    assert [g["category_code"] for g in groups] == ["E1", "F3"]

    real_names = load_categories()
    f3 = next(g for g in groups if g["category_code"] == "F3")
    assert f3["category_name"] == real_names["F3"]["name"]
    assert [s["domain"] for s in f3["suppliers"]] == ["huterrussia.ru"]


def test_list_trusted_suppliers_empty_when_nothing_recorded(monkeypatch, tmp_path):
    db_path = tmp_path / "trusted.db"
    monkeypatch.setattr(webapp, "TrustedSupplierStore", lambda *a, **k: TrustedSupplierStore(db_path))

    client = TestClient(webapp.app)
    resp = client.get("/api/trusted-suppliers")

    assert resp.status_code == 200
    assert resp.json() == []


def test_delete_trusted_supplier_removes_it(monkeypatch, tmp_path):
    db_path = tmp_path / "trusted.db"
    monkeypatch.setattr(webapp, "TrustedSupplierStore", lambda *a, **k: TrustedSupplierStore(db_path))
    with TrustedSupplierStore(db_path) as store:
        store.record_supplier(
            domain="junk.example", name="Junk", category_code="F3", rank=1, source_query="q"
        )

    client = TestClient(webapp.app)
    resp = client.delete("/api/trusted-suppliers/junk.example")

    assert resp.status_code == 200
    assert resp.json() == {"deleted": "junk.example"}
    with TrustedSupplierStore(db_path) as store:
        assert store.get_supplier("junk.example") is None


def test_delete_trusted_supplier_unknown_domain_returns_404(monkeypatch, tmp_path):
    db_path = tmp_path / "trusted.db"
    monkeypatch.setattr(webapp, "TrustedSupplierStore", lambda *a, **k: TrustedSupplierStore(db_path))

    client = TestClient(webapp.app)
    resp = client.delete("/api/trusted-suppliers/unknown.example")

    assert resp.status_code == 404


def test_list_categories_returns_full_reference_including_empty_ones(monkeypatch, tmp_path):
    """В отличие от /api/trusted-suppliers (только категории с данными),
    /api/categories отдаёт ВЕСЬ справочник — иначе байер не смог бы
    вручную завести первого поставщика в ещё пустую категорию."""
    from procurement_search.config import load_categories

    client = TestClient(webapp.app)
    resp = client.get("/api/categories")

    assert resp.status_code == 200
    rows = resp.json()
    real_categories = load_categories()
    assert len(rows) == len(real_categories)
    assert {"code": "F3", "name": real_categories["F3"]["name"]} in rows


def test_add_trusted_supplier_manually(monkeypatch, tmp_path):
    db_path = tmp_path / "trusted.db"
    monkeypatch.setattr(webapp, "TrustedSupplierStore", lambda *a, **k: TrustedSupplierStore(db_path))

    client = TestClient(webapp.app)
    resp = client.post(
        "/api/trusted-suppliers",
        json={
            "domain": "https://www.huterrussia.ru/catalog",
            "name": "Huter Russia",
            "category_code": "F3",
            "phone": "+7 900 000 00 00",
        },
    )

    assert resp.status_code == 200
    # Схема/www/путь должны быть отрезаны при нормализации домена.
    assert resp.json() == {"domain": "huterrussia.ru", "category_code": "F3"}

    list_resp = client.get("/api/trusted-suppliers")
    groups = list_resp.json()
    f3 = next(g for g in groups if g["category_code"] == "F3")
    assert f3["suppliers"][0]["domain"] == "huterrussia.ru"
    assert f3["suppliers"][0]["phone"] == "+7 900 000 00 00"
    assert f3["suppliers"][0]["best_rank"] == 1


def test_add_trusted_supplier_rejects_unknown_category(monkeypatch, tmp_path):
    db_path = tmp_path / "trusted.db"
    monkeypatch.setattr(webapp, "TrustedSupplierStore", lambda *a, **k: TrustedSupplierStore(db_path))

    client = TestClient(webapp.app)
    resp = client.post(
        "/api/trusted-suppliers",
        json={"domain": "example.ru", "name": "Тест", "category_code": "НЕСУЩЕСТВУЮЩАЯ"},
    )

    assert resp.status_code == 400


def test_add_trusted_supplier_rejects_empty_domain_or_name(monkeypatch, tmp_path):
    db_path = tmp_path / "trusted.db"
    monkeypatch.setattr(webapp, "TrustedSupplierStore", lambda *a, **k: TrustedSupplierStore(db_path))

    client = TestClient(webapp.app)
    resp = client.post(
        "/api/trusted-suppliers",
        json={"domain": "   ", "name": "Тест", "category_code": "F3"},
    )

    assert resp.status_code == 400
