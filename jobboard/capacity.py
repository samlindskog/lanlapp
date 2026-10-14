"""The one browser slot. The VPS has 2 GB for everything, so only one operation may drive a
Chrome at a time: a background run (scan, bulk refresh) or a synchronous one (single refresh,
board registration). Everyone asks here, so the policy and the error are the same everywhere."""
import contextlib
import threading

import jobscan as J
import runs


class Busy(Exception):
    """Another operation holds the browser slot; .holder says which."""

    def __init__(self, holder):
        super().__init__(f"a run is active ({holder.get('kind')} {holder.get('id')}); the browser is busy")
        self.holder = holder


_lock = threading.Lock()
_sync = {}               # label -> started_at, for synchronous holders


def busy():
    """The holder ({'id', 'kind'}) or None."""
    with _lock:
        for label, since in _sync.items():
            return {"id": label, "kind": "sync"}
    return runs.active()


def check():
    """Raise Busy if anything holds the slot (call before starting a background run)."""
    h = busy()
    if h:
        raise Busy(h)


@contextlib.contextmanager
def hold(label):
    """Hold the slot for a synchronous operation; raises Busy instead of waiting."""
    with _lock:
        if _sync:
            raise Busy({"id": next(iter(_sync)), "kind": "sync"})
        h = runs.active()
        if h:
            raise Busy(h)
        _sync[label] = J.now()
    try:
        yield
    finally:
        with _lock:
            _sync.pop(label, None)
