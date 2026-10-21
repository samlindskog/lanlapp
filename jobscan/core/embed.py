"""Embeddings (OpenAI text-embedding-3-small) for semantic search; hash-based so re-runs are no-ops."""
import hashlib
import json
import os
import urllib.error
import urllib.request
from core.adapters import document_sections, primary_document
from core.db import EMBED_MODEL, now
import ssl


def _ssl_context():
    for cafile in (None, "/etc/ssl/cert.pem"):
        try:
            if cafile is None:
                try:
                    import certifi
                    return ssl.create_default_context(cafile=certifi.where())
                except ImportError:
                    return ssl.create_default_context()
            if os.path.exists(cafile):
                return ssl.create_default_context(cafile=cafile)
        except Exception:  # noqa: BLE001
            continue
    return ssl.create_default_context()

SSL_CTX = _ssl_context()



def _grade_str(j):
    from core.jobs import grade_str   # jobs imports embed: resolve the cycle lazily
    return grade_str(j)


MAX_INPUT_CHARS = 20000      # ~5k tokens of English; dense text (tables, codes) runs nearer 3 chars a token


def embed_texts(texts, model=EMBED_MODEL):
    """OpenAI embeddings via plain HTTPS (no SDK). Returns list of vectors. An input the model
    still finds too long (the 8192-token limit counts tokens, not characters) is halved and the
    batch retried, a few times, rather than failing the whole batch."""
    texts = [t[:MAX_INPUT_CHARS] for t in texts]
    for _ in range(4):
        try:
            return _embed_once(texts, model)
        except RuntimeError as e:
            if "maximum input length" not in str(e):
                raise
            longest = max(range(len(texts)), key=lambda i: len(texts[i]))
            texts[longest] = texts[longest][: len(texts[longest]) // 2]
    return _embed_once(texts, model)


def _embed_once(texts, model):
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY is not set")
    body = json.dumps({"model": model, "input": texts}).encode()
    req = urllib.request.Request("https://api.openai.com/v1/embeddings", data=body, method="POST",
                                 headers={"Authorization": f"Bearer {key}",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120, context=SSL_CTX) as r:
            data = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode()).get("error", {}).get("message", "")
        except Exception:  # noqa: BLE001
            msg = ""
        raise RuntimeError(f"OpenAI embeddings request failed ({e.code}): {msg or e.reason}") from None
    out = [None] * len(texts)
    for item in data["data"]:
        out[item["index"]] = item["embedding"]
    return out

def embedding_text(conn, job):
    """What gets embedded for a job: title, location, series, grade, and the
    adapter's key sections (or the primary document's text)."""
    d, source, key = primary_document(conn, job["id"])
    parts = [job.get("title") or "", job.get("location") or "", job.get("series") or "",
             _grade_str(job) if job.get("pay_plan") or job.get("grade_low") else ""]
    if d:
        secs = document_sections(source, d["text"], d["kind"])
        picked = []
        for name in ("Summary", "Duties", key or "", "Education", "Position Overview/Duties"):
            if name and name in secs:
                picked.append(f"{name}\n{secs[name]}")
        body = "\n\n".join(picked) if picked else (d["text"] or "")
        parts.append(body)
    text = "\n".join(p for p in parts if p).strip()
    return text[:MAX_INPUT_CHARS]   # embed_texts halves a text the model still rejects

def _vec(v):
    return "[" + ",".join(f"{x:.7f}" for x in v) + "]"

def embed_if_possible(conn, ids, log=lambda m: None):
    """Keep embeddings current after data changes. Idempotent: embed_jobs only
    re-embeds a job whose embedding text hash changed. Silently skipped when no
    OPENAI_API_KEY is set, and never raises (embedding must not break ingestion)."""
    if not ids or not os.environ.get("OPENAI_API_KEY"):
        return {"embedded": 0, "skipped": len(ids or [])}
    try:
        return embed_jobs(conn, ids=list(ids), log=log)
    except Exception as e:  # noqa: BLE001
        log(f"embedding skipped: {e}")
        return {"embedded": 0, "error": str(e)}

def embed_jobs(conn, ids=None, force=False, log=print, batch=32):
    """Embed jobs lacking an embedding or whose embedded text changed."""
    if ids:
        jobs = conn.execute("SELECT * FROM jobs WHERE id = ANY(%s) ORDER BY id", (list(ids),)).fetchall()
    else:
        jobs = conn.execute("SELECT * FROM jobs ORDER BY id").fetchall()
    existing = {r["job_id"]: r["text_hash"] for r in conn.execute("SELECT job_id, text_hash FROM embeddings")}
    todo = []
    for j in jobs:
        text = embedding_text(conn, j)
        if not text:
            continue
        h = hashlib.sha1(text.encode()).hexdigest()
        if force or existing.get(j["id"]) != h:
            todo.append((j["id"], h, text))
    log(f"embedding {len(todo)} of {len(jobs)} jobs ({EMBED_MODEL})")
    done = 0
    for i in range(0, len(todo), batch):
        chunk = todo[i:i + batch]
        vecs = embed_texts([t for _, _, t in chunk])
        for (jid, h, text), v in zip(chunk, vecs):
            conn.execute("""INSERT INTO embeddings (job_id, model, text_hash, content, embedding, created_at)
                VALUES (%s,%s,%s,%s,%s::vector,%s)
                ON CONFLICT (job_id) DO UPDATE SET model=EXCLUDED.model, text_hash=EXCLUDED.text_hash,
                    content=EXCLUDED.content, embedding=EXCLUDED.embedding, created_at=EXCLUDED.created_at""",
                         (jid, EMBED_MODEL, h, text, _vec(v), now()))
        conn.commit()
        done += len(chunk)
        log(f"  embedded {done}/{len(todo)}")
    return {"embedded": done, "skipped": len(jobs) - len(todo)}

def semantic_search(conn, query, limit=50):
    """Rank jobs by cosine similarity of their embedding to the query."""
    qv = _vec(embed_texts([query])[0])
    rows = conn.execute("""
        SELECT j.id, j.title, j.location, j.close_date, 1 - (e.embedding <=> %s::vector) AS score
        FROM embeddings e JOIN jobs j ON j.id = e.job_id
        ORDER BY e.embedding <=> %s::vector LIMIT %s""", (qv, qv, limit)).fetchall()
    return [dict(r) for r in rows]
