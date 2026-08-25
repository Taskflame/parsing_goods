"""Тесты веб-API (без сети): источники подменяются, как в test_pipeline.py."""

from fastapi.testclient import TestClient

from procurement_search import pipeline, webapp
from procurement_search.models import Candidate, VerificationFlag


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
