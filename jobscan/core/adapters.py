"""The adapter registry as the rest of the library sees it, and section lookup for a stored job."""
import os
import sys
from core.text import generic_sections

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # the jobscan directory


def load_source(name):
    import importlib
    try:
        return importlib.import_module(f"sources.{name}")
    except ModuleNotFoundError:
        sys.exit(f"unknown source '{name}'. Available: {', '.join(list_sources())}")

def list_sources():
    d = os.path.join(HERE, "sources")
    return sorted(f[:-3] for f in os.listdir(d) if f.endswith(".py") and not f.startswith("_"))

_platforms = {}

def platform_name(adapter):
    """Display name of an adapter's hosting platform ("Workday"), or the adapter name itself."""
    if adapter not in _platforms:
        mod, _, _ = source_meta(adapter)
        _platforms[adapter] = getattr(mod, "DISPLAY_NAME", None) or adapter
    return _platforms[adapter]

def source_meta(source_name):
    try:
        mod = load_source(source_name) if source_name in list_sources() else None
    except SystemExit:
        mod = None
    return (mod, getattr(mod, "PRIMARY_KIND", "posting"), getattr(mod, "KEY_SECTION", None))

def document_sections(source_name, text, kind):
    mod, _, _ = source_meta(source_name)
    if mod and hasattr(mod, "sections"):
        try:
            return mod.sections(text, kind)
        except Exception:  # noqa: BLE001
            pass
    return generic_sections(text)

def primary_document(conn, job_id):
    j = conn.execute("SELECT source FROM jobs WHERE id=%s", (job_id,)).fetchone()
    if not j:
        raise KeyError(job_id)
    mod, pk, key = source_meta(j["source"])
    d = conn.execute("SELECT * FROM documents WHERE job_id=%s AND kind=%s ORDER BY id DESC LIMIT 1",
                     (job_id, pk)).fetchone()
    if not d:
        d = conn.execute("SELECT * FROM documents WHERE job_id=%s AND kind!='board_card' ORDER BY id DESC LIMIT 1",
                         (job_id,)).fetchone()
    return d, j["source"], key

def get_section(conn, job_id, name=None, kind=None):
    d, source, key = primary_document(conn, job_id)
    if kind:
        d = conn.execute("SELECT * FROM documents WHERE job_id=%s AND kind=%s ORDER BY id DESC LIMIT 1",
                         (job_id, kind)).fetchone()
    if not d:
        return None, None
    secs = document_sections(source, d["text"], d["kind"])
    name = name or key
    if name in secs:
        return d, secs[name]
    for k, v in secs.items():
        if name and k.lower().startswith(name.lower()):
            return d, v
    return d, None
