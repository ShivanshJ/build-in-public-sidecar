import re
from functools import lru_cache

import numpy as np

EMBED_MODEL = "BAAI/bge-small-en-v1.5"
RERANK_MODEL = "Xenova/ms-marco-MiniLM-L-6-v2"


@lru_cache(maxsize=1)
def _embedder():
    from fastembed import TextEmbedding

    return TextEmbedding(EMBED_MODEL)


@lru_cache(maxsize=1)
def _reranker():
    from fastembed.rerank.cross_encoder import TextCrossEncoder

    return TextCrossEncoder(RERANK_MODEL)


def _unit(v) -> np.ndarray:
    v = np.asarray(v, dtype=np.float32)
    return v / (np.linalg.norm(v) + 1e-9)


def embed_pending(conn, batch: int = 64) -> int:
    done = 0
    while True:
        rows = conn.execute("SELECT id, text FROM chunks WHERE emb IS NULL LIMIT ?", (batch,)).fetchall()
        if not rows:
            return done
        vecs = list(_embedder().embed([r["text"] for r in rows]))
        conn.executemany(
            "UPDATE chunks SET emb=? WHERE id=?",
            [(_unit(v).tobytes(), r["id"]) for v, r in zip(vecs, rows)],
        )
        conn.commit()
        done += len(rows)


def bm25(conn, query: str, n: int = 30, since: str | None = None, until: str | None = None) -> list[int]:
    words = re.findall(r"\w+", query)
    if not words:
        return []
    match = " OR ".join(f'"{w}"' for w in words)
    sql = "SELECT c.id FROM chunks_fts f JOIN chunks c ON c.id=f.rowid WHERE chunks_fts MATCH ?"
    args: list = [match]
    if since:
        sql += " AND c.ts >= ?"
        args.append(since)
    if until:
        sql += " AND c.ts < ?"
        args.append(until)
    sql += " ORDER BY bm25(chunks_fts) LIMIT ?"
    args.append(n)
    return [r["id"] for r in conn.execute(sql, args)]


def dense(conn, query: str, n: int = 30, since: str | None = None, until: str | None = None) -> list[int]:
    sql, args = "SELECT id, emb FROM chunks WHERE emb IS NOT NULL", []
    if since:
        sql += " AND ts >= ?"
        args.append(since)
    if until:
        sql += " AND ts < ?"
        args.append(until)
    rows = conn.execute(sql, args).fetchall()
    if not rows:
        return []
    mat = np.vstack([np.frombuffer(r["emb"], dtype=np.float32) for r in rows])
    q = _unit(next(iter(_embedder().query_embed([query]))))
    top = np.argsort(-(mat @ q))[:n]
    return [rows[i]["id"] for i in top]


def rrf(rankings: list[list[int]], k: int = 60) -> list[int]:
    score: dict[int, float] = {}
    for ranking in rankings:
        for rank, cid in enumerate(ranking):
            score[cid] = score.get(cid, 0.0) + 1.0 / (k + rank + 1)
    return sorted(score, key=score.get, reverse=True)


def search(conn, query: str, k: int = 8, since: str | None = None, until: str | None = None,
           rerank: bool = True, pool: int = 30) -> list[dict]:
    fused = rrf([bm25(conn, query, pool, since, until), dense(conn, query, pool, since, until)])[:pool]
    if not fused:
        return []
    rows = {
        r["id"]: dict(r)
        for r in conn.execute(
            f"SELECT id, session_id, role, text, ts FROM chunks WHERE id IN ({','.join('?' * len(fused))})", fused
        )
    }
    hits = [rows[i] for i in fused if i in rows]
    if rerank:
        scores = list(_reranker().rerank(query, [h["text"] for h in hits]))
        for h, s in zip(hits, scores):
            h["score"] = float(s)
        hits.sort(key=lambda h: h["score"], reverse=True)
    return hits[:k]
