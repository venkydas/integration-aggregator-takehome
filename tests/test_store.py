from app.store import TTLStore


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_items_expire():
    clock = FakeClock()
    store: TTLStore[str] = TTLStore(10, clock=clock)
    store.put("k", "v")

    clock.now = 9.9
    assert store.get("k") == "v"
    clock.now = 10
    assert store.get("k") is None


def test_pop_removes_item():
    store: TTLStore[str] = TTLStore(10, clock=FakeClock())
    store.put("k", "v")
    assert store.pop("k") == "v"
    assert store.pop("k") is None


def test_purge_drops_only_expired():
    clock = FakeClock()
    store: TTLStore[str] = TTLStore(10, clock=clock)
    store.put("old", "1")
    clock.now = 5
    store.put("new", "2")
    clock.now = 12

    assert store.purge() == 1
    assert len(store) == 1
    assert store.get("new") == "2"
