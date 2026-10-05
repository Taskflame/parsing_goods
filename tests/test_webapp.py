"""Тесты веб-API (без сети): источники подменяются, как в test_pipeline.py."""

from fastapi.testclient import TestClient

from procurement_search import photo_search, pipeline, webapp
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


def _wait_job(client, resp) -> dict:
    """After POST returns 202 {job_id}, poll /api/jobs/{id} until done,
    then return job['result']."""
    import time
    assert resp.status_code == 202, resp.status_code
    job_id = resp.json()["job_id"]
    for _ in range(100):
        j = client.get(f"/api/jobs/{job_id}").json()
        if j["status"] == "done":
            return j["result"]
        if j["status"] == "error":
            raise AssertionError(f"job error: {j.get('error')}")
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def _search_and_wait(client, payload: dict) -> dict:
    return _wait_job(client, client.post("/api/search", json=payload))


def _photo_and_wait(client, **post_kwargs) -> dict:
    """POST /api/photo-search (multipart), затем ждём done и возвращаем result."""
    return _wait_job(client, client.post("/api/photo-search", **post_kwargs))


def test_search_returns_companies_and_creates_report(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)

    data = _search_and_wait(client, {"query": "гальванические покрытия"})

    assert len(data["companies"]) == 1
    assert data["companies"][0]["name"] == "ООО Гальванические покрытия"
    assert data["companies"][0]["phone"]["value"] == "+7 900 111 11 11"
    assert data["companies"][0]["phone"]["confidence"] == "не проверен"
    assert data["companies"][0]["stock_status"] == "не проверено"
    assert data["report"]["query"] == "гальванические покрытия"
    assert (tmp_path / "reports" / data["report"]["filename"]).exists()

    # Продолжительность поиска сохраняется в запись истории (только длительность,
    # без времён начала/конца) и видна в /api/reports.
    assert isinstance(data["report"].get("duration_seconds"), (int, float))
    reports = client.get("/api/reports").json()
    # Фейковый источник отвечает мгновенно, поэтому duration может быть 0 — важно
    # лишь, что поле присутствует и неотрицательно (технически >0 в реале, но тут
    # fake-source не тратит времени).
    assert any(isinstance(r.get("duration_seconds"), (int, float)) and r["duration_seconds"] >= 0 for r in reports)


def test_empty_query_returns_400(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)

    resp = client.post("/api/search", json={"query": "   "})

    assert resp.status_code == 400


def test_reports_list_and_download_roundtrip(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)

    data = _search_and_wait(client, {"query": "гальванические покрытия"})
    filename = data["report"]["filename"]

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


def test_delete_report_removes_from_index_and_disk(monkeypatch, tmp_path):
    """DELETE /api/reports/{filename} убирает запись из истории и удаляет .xlsx."""
    client = _client(monkeypatch, tmp_path)

    data = _search_and_wait(client, {"query": "гальванические покрытия"})
    filename = data["report"]["filename"]
    file_path = tmp_path / "reports" / filename
    assert file_path.exists()

    resp = client.delete(f"/api/reports/{filename}")
    assert resp.status_code == 200
    assert resp.json()["deleted"] == filename

    # Запись исчезла из списка.
    list_resp = client.get("/api/reports").json()
    assert not any(r["filename"] == filename for r in list_resp)
    # Файл удалён с диска.
    assert not file_path.exists()


def test_delete_report_missing_returns_404(monkeypatch, tmp_path):
    """Удаление несуществующего отчёта — 404."""
    client = _client(monkeypatch, tmp_path)
    resp = client.delete("/api/reports/never_existed.xlsx")
    assert resp.status_code == 404


def test_search_reports_required_quantity_and_availability_summary(monkeypatch, tmp_path):
    """check_availability=False (по умолчанию) — required_quantity/
    availability_summary всё равно вычисляются из Слоя 0.6 (order_qty
    распознаётся детерминированно, без LLM), а сама сводка по остаткам —
    None, потому что ни у одной компании ещё нет company.availability."""
    client = _client(monkeypatch, tmp_path)

    data = _search_and_wait(client, {"query": "цинкование 11 шт"})

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
    data = _search_and_wait(client, {"query": "гальванические покрытия"})
    website = data["companies"][0]["website"]
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


# --- /api/photo-search — поиск по фото (изолированный модуль) ---


def _fake_photo_desc(monkeypatch, query: str):
    """Подменяет describe_product_from_image, чтобы эндпоинт работал без
    реальной сети (мультимодальная модель). Возвращает итоговый query."""
    monkeypatch.setattr(photo_search, "describe_product_from_image", lambda img, mime: query)
    return query


def test_photo_search_runs_separate_endpoint_and_returns_query(monkeypatch, tmp_path):
    """OCR-запрос по фото (query) уходит в search_and_score, результат отдаёт
    query_used. Файл отправляется через multipart, поиск асинхронный."""
    client = _client(monkeypatch, tmp_path)
    _fake_photo_desc(monkeypatch, "Кабель ВВГ 3х2,5 ГОСТ")

    data = _photo_and_wait(
        client,
        files={"file": ("photo.jpg", b"fake-jpeg-bytes", "image/jpeg")},
    )

    assert data["query_used"] == "Кабель ВВГ 3х2,5 ГОСТ"
    assert data["report"]["query"] == "Кабель ВВГ 3х2,5 ГОСТ"
    assert len(data["companies"]) == 1
    assert data["companies"][0]["name"] == "ООО Гальванические покрытия"
    assert (tmp_path / "reports" / data["report"]["filename"]).exists()


def test_photo_search_fills_search_query(monkeypatch, tmp_path):
    """OCR/ключевые слова вернули запрос — поиск идёт по нему."""
    client = _client(monkeypatch, tmp_path)
    _fake_photo_desc(monkeypatch, "Насос дренажный")

    data = _photo_and_wait(
        client,
        files={"file": ("n.jpg", b"fake", "image/jpeg")},
    )

    assert data["query_used"] == "Насос дренажный"
    assert data["report"]["query"] == "Насос дренажный"


def test_photo_search_empty_ocr_returns_422(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    _fake_photo_desc(monkeypatch, "  ")

    resp = client.post(
        "/api/photo-search",
        files={"file": ("blank.jpg", b"", "image/jpeg")},
    )

    assert resp.status_code == 422


def test_photo_search_passes_flags_from_multipart_to_search(monkeypatch, tmp_path):
    """Галочки, пришедшие как multipart-form (deep_relevance/check_availability),
    должны дойти до search_and_score. Раньше без Form(...) флаги терялись (False)."""
    captured = {}

    def _fake_search(query, **kwargs):
        captured["query"] = query
        captured["kwargs"] = kwargs
        return pipeline.search_and_score(query, **kwargs)

    monkeypatch.setattr(webapp, "search_and_score", _fake_search)
    client = _client(monkeypatch, tmp_path)
    _fake_photo_desc(monkeypatch, "Мастика ТехноНИКОЛЬ №20")

    _photo_and_wait(
        client,
        files={"file": ("p.jpg", b"fake", "image/jpeg")},
        data={"deep_relevance": "true", "check_availability": "true"},
    )

    assert captured["kwargs"]["deep_relevance"] is True
    assert captured["kwargs"]["check_availability"] is True


def test_photo_search_sends_report_email(monkeypatch, tmp_path):
    """/api/photo-search тоже должен автоматически рассылать сформированный
    Excel-отчёт на EMAIL_TO (тот же механизм, что в /api/search)."""
    from procurement_search import feedback as feedback_mod

    sent = {}

    def _fake_send(path, query, to=None):
        sent["path"] = path
        sent["query"] = query
        sent["to"] = to
        return True

    # webapp импортирует try_send_report_email внутри функции
    # (from ... import), поэтому патчим именно модуль feedback.
    monkeypatch.setattr(feedback_mod, "try_send_report_email", _fake_send)
    client = _client(monkeypatch, tmp_path)
    _fake_photo_desc(monkeypatch, "Насос дренажный")

    _photo_and_wait(client, files={"file": ("n.jpg", b"fake", "image/jpeg")})

    assert sent.get("query") == "Насос дренажный"
    assert sent.get("path") and str(sent["path"]).endswith(".xlsx")
    # Без персонального email в форме — рассылка на EMAIL_TO (to=None).
    assert sent.get("to") in (None, "")


# --- Обратная связь по отчётам (feedback.py, SQLite feedback.db) ---

def _patch_feedback_db(monkeypatch, tmp_path):
    """Направляем SQLite-хранилище отзывов во временный файл, не трогая реальный."""
    from procurement_search import feedback as feedback_mod
    monkeypatch.setattr(feedback_mod, "DEFAULT_FEEDBACK_DB", tmp_path / "feedback.db")
    return feedback_mod


def test_feedback_save_and_list(monkeypatch, tmp_path):
    """Отзыв сохраняется в SQLite и появляется в списке /api/feedback."""
    _patch_feedback_db(monkeypatch, tmp_path)
    client = _client(monkeypatch, tmp_path)
    report_key = "20260101_x.xlsx"

    post = client.post("/api/feedback", json={
        "report_key": report_key,
        "name": "Иван",
        "email": "ivan@example.ru",
        "rating": "up",
        "comment": "Помогло!",
    })
    assert post.status_code == 200
    assert post.json()["ok"] is True

    rows = client.get("/api/feedback").json()
    assert any(r["report_key"] == report_key and r["rating"] == "up"
               and r["comment"] == "Помогло!" and r["email"] == "ivan@example.ru"
               for r in rows)


def test_feedback_text_comment_keeps_email_and_query(monkeypatch, tmp_path):
    """Текстовый отзыв (rating='text') сохраняет email и запрос."""
    _patch_feedback_db(monkeypatch, tmp_path)
    client = _client(monkeypatch, tmp_path)

    client.post("/api/feedback", json={
        "report_key": "r1.xlsx", "name": "Анна",
        "email": "anna@example.ru", "rating": "text",
        "comment": "Хочу, чтобы добавили колонку с ценой",
    })

    rows = client.get("/api/feedback").json()
    mine = [r for r in rows if r["email"] == "anna@example.ru"]
    assert len(mine) >= 1
    assert mine[0]["rating"] == "text"
    assert mine[0]["comment"] == "Хочу, чтобы добавили колонку с ценой"


def test_feedback_anonymous_reaction_without_name_email(monkeypatch, tmp_path):
    """Отзыв можно оставить без имени/email — сохраняется анонимно (None)."""
    _patch_feedback_db(monkeypatch, tmp_path)
    client = _client(monkeypatch, tmp_path)

    resp = client.post("/api/feedback", json={
        "report_key": "anon.xlsx",
        "name": "",
        "email": "",
        "rating": "down",
        "comment": "",
    })
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    rows = client.get("/api/feedback").json()
    mine = [r for r in rows if r["report_key"] == "anon.xlsx"]
    assert len(mine) == 1
    assert mine[0]["rating"] == "down"
    assert mine[0]["name"] is None
    assert mine[0]["email"] is None


def test_feedback_with_email_but_no_name_saves(monkeypatch, tmp_path):
    """Можно указать только email (без имени) — отзыв привязывается к email."""
    _patch_feedback_db(monkeypatch, tmp_path)
    client = _client(monkeypatch, tmp_path)

    resp = client.post("/api/feedback", json={
        "report_key": "onlymail.xlsx",
        "name": "",
        "email": "buyer@example.ru",
        "rating": "up",
        "comment": "",
    })
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    rows = client.get("/api/feedback").json()
    mine = [r for r in rows if r["report_key"] == "onlymail.xlsx"]
    assert len(mine) == 1
    assert mine[0]["email"] == "buyer@example.ru"


def test_feedback_rejects_invalid_email(monkeypatch, tmp_path):
    """Если email введён, но некорректен — отклоняем (не сохраняем)."""
    _patch_feedback_db(monkeypatch, tmp_path)
    client = _client(monkeypatch, tmp_path)

    resp = client.post("/api/feedback", json={
        "report_key": "bad.xlsx",
        "name": "Иван",
        "email": "не-почта",
        "rating": "up",
        "comment": "",
    })
    assert resp.status_code == 422
    rows = client.get("/api/feedback").json()
    assert not any(r["report_key"] == "bad.xlsx" for r in rows)


def test_feedback_submit_from_email_saves_to_db(monkeypatch, tmp_path):
    """/api/feedback/submit — клик по эмодзи в письме сохраняет оценку в БД
    с email получателя (чтобы знать, кто оценил) и возвращает HTML-страницу."""
    _patch_feedback_db(monkeypatch, tmp_path)
    client = _client(monkeypatch, tmp_path)

    resp = client.get("/api/feedback/submit?report_key=r2.xlsx&rating=up&email=buyer@example.ru")
    assert resp.status_code == 200
    # Ответ — HTML-страница (не JSON), чтобы браузер показал форму комментария.
    assert resp.headers["content-type"].startswith("text/html")
    assert "Благодарим" in resp.text

    rows = client.get("/api/feedback").json()
    mine = [r for r in rows if r["email"] == "buyer@example.ru"]
    assert len(mine) >= 1
    assert mine[0]["rating"] == "up"
    assert mine[0]["report_key"] == "r2.xlsx"


def test_feedback_submit_link_validates_rating(monkeypatch, tmp_path):
    """/api/feedback/submit — оценка прямо из письма, невалидный рейтинг — 422."""
    _patch_feedback_db(monkeypatch, tmp_path)
    client = _client(monkeypatch, tmp_path)
    resp = client.get("/api/feedback/submit?report_key=some.xlsx&rating=meh")
    assert resp.status_code == 422


def test_feedback_comment_from_email_saves(monkeypatch, tmp_path):
    """/api/feedback/comment — кнопка комментария в письме: показывает форму,
    а с ?comment=... сохраняет текст (rating='text') в БД."""
    _patch_feedback_db(monkeypatch, tmp_path)
    client = _client(monkeypatch, tmp_path)

    # 1) форма комментария
    form = client.get("/api/feedback/comment?report_key=r9.xlsx&email=c@d.ru")
    assert form.status_code == 200
    assert form.headers["content-type"].startswith("text/html")
    assert "textarea" in form.text

    # 2) отправка комментария
    resp = client.get("/api/feedback/comment",
                      params={"report_key": "r9.xlsx", "email": "c@d.ru", "comment": "Сделайте колонку с ценой"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "Благодарим" in resp.text

    rows = client.get("/api/feedback").json()
    mine = [r for r in rows if r["email"] == "c@d.ru"]
    assert len(mine) >= 1
    assert mine[0]["rating"] == "text"
    assert mine[0]["comment"] == "Сделайте колонку с ценой"
    assert mine[0]["report_key"] == "r9.xlsx"


def test_search_forwards_personal_email_to_mailer(monkeypatch, tmp_path):
    """Если пользователь указал в запросе свой email (персональная рассылка
    при тестировании), /api/search должен передать его в try_send_report_email."""
    from procurement_search import feedback as feedback_mod

    sent = {}

    def _fake_send(path, query, to=None):
        sent["to"] = to
        return True

    monkeypatch.setattr(feedback_mod, "try_send_report_email", _fake_send)
    client = _client(monkeypatch, tmp_path)

    _search_and_wait(client, {
        "query": "гальванические покрытия",
        "email": "tester@example.ru",
    })

    assert sent.get("to") == "tester@example.ru"
