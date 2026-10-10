import threading

from shortener.records import Click, IdempotencyEntry, UrlRecord
from shortener.service import UrlService, is_expired, link_status
from shortener.store import UrlStore


def make_record(code="abc1234", **overrides) -> UrlRecord:
    fields = {"code": code, "url": "https://example.com", "created_at": 100.0}
    fields.update(overrides)
    return UrlRecord(**fields)


def test_add_and_get():
    store = UrlStore()
    assert store.add(make_record()) is True
    assert store.get("abc1234").url == "https://example.com"
    assert store.get("missing") is None


def test_add_duplicate_code_is_refused_and_keeps_original():
    store = UrlStore()
    store.add(make_record(url="https://first.example"))
    assert store.add(make_record(url="https://second.example")) is False
    assert store.get("abc1234").url == "https://first.example"


def test_update_replaces_fields():
    store = UrlStore()
    store.add(make_record())
    updated = store.update("abc1234", url="https://new.example")
    assert updated.url == "https://new.example"
    assert store.get("abc1234").url == "https://new.example"
    assert store.update("missing", url="x") is None


def test_soft_delete_keeps_record_and_stamps_time(clock):
    store = UrlStore(clock=clock)
    store.add(make_record())
    clock.advance(5)
    deleted = store.soft_delete("abc1234")
    assert deleted.deleted_at == clock.now
    assert store.get("abc1234") is not None  # still stored, only marked
    assert store.soft_delete("missing") is None


def test_expiry_follows_the_fake_clock(clock):
    record = make_record(created_at=clock.now, expires_at=clock.now + 60)
    assert not is_expired(record, clock.now)
    assert not is_expired(record, clock.now + 59.9)
    assert is_expired(record, clock.now + 60)
    assert link_status(record, clock.now + 61) == "expired"


def test_record_without_expiry_never_expires(clock):
    assert not is_expired(make_record(), clock.now + 10**9)


def test_deleted_status_wins_over_expiry(clock):
    record = make_record(expires_at=1.0, deleted_at=2.0)
    assert link_status(record, clock.now) == "deleted"


def test_clicks_are_stored_per_code_and_returned_as_copy():
    store = UrlStore()
    store.add_click("a", Click(1.0, None, "h1"))
    clicks = store.get_clicks("a")
    clicks.clear()
    assert len(store.get_clicks("a")) == 1
    assert store.get_clicks("other") == []


def test_idempotency_entries_roundtrip():
    store = UrlStore()
    entry = IdempotencyEntry("fp", make_record())
    assert store.get_idempotency("k") is None
    store.put_idempotency("k", entry)
    assert store.get_idempotency("k") == entry


def test_service_idempotency_same_body_returns_same_record(store, clock):
    service = UrlService(store, clock=clock)
    first = service.create("https://example.com", idempotency_key="k1")
    clock.advance(10)
    second = service.create("https://example.com", idempotency_key="k1")
    assert first.created is True and second.created is False
    assert first.record == second.record


def test_json_persistence_survives_restart(tmp_path, clock):
    path = tmp_path / "data.json"
    store = UrlStore(path=path, clock=clock)
    store.add(make_record(expires_at=500.0))
    store.add_click("abc1234", Click(1.0, "https://ref.example", "hash"))
    store.put_idempotency("key", IdempotencyEntry("fp", make_record()))
    store.soft_delete("abc1234")

    reloaded = UrlStore(path=path, clock=clock)
    record = reloaded.get("abc1234")
    assert record.expires_at == 500.0
    assert record.deleted_at == clock.now
    assert reloaded.get_clicks("abc1234") == [Click(1.0, "https://ref.example", "hash")]
    assert reloaded.get_idempotency("key").fingerprint == "fp"
    assert not (tmp_path / "data.json.tmp").exists()


def test_store_without_path_writes_no_files(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    UrlStore().add(make_record())
    assert list(tmp_path.iterdir()) == []


def test_concurrent_adds_of_same_code_only_one_wins():
    store = UrlStore()
    results = []

    def worker():
        results.append(store.add(make_record()))

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results.count(True) == 1
