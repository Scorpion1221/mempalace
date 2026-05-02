#!/usr/bin/env python3
"""
searcher.py — Find anything. Exact words.

Hybrid search: BM25 keyword matching + vector semantic similarity. The
drawer query is the floor — always runs — and closet hits add a rank-based
boost when they agree. Closets are a ranking *signal*, never a gate, so
weak closets (regex extraction on narrative content) can only help, never
hide drawers the direct path would have found.
"""

import logging
import math
import re
from pathlib import Path

from .palace import get_closets_collection, get_collection

# Closet pointer line format: "topic|entities|→drawer_id_a,drawer_id_b"
# Multiple lines may join with newlines inside one closet document.
_CLOSET_DRAWER_REF_RE = re.compile(r"→([\w,]+)")

logger = logging.getLogger("mempalace_mcp")


class SearchError(Exception):
    """Raised when search cannot proceed (e.g. no palace found)."""


# Split on non-word boundaries.  Latin/Cyrillic/digits stay word-level;
# CJK ideographs are split into overlapping bigrams below.
_TOKEN_RE = re.compile(r"\w{2,}", re.UNICODE)

# CJK Unified Ideographs + Extension A + CJK Compat Ideographs + some Kana
_CJK_RE = re.compile(
    r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
    r"\U00020000-\U0002a6df\U0002a700-\U0002ebef]+",
    re.UNICODE,
)

# Preference / advice-seeking question stems. EXPERIMENTAL heuristic,
# NOT applied automatically inside _hybrid_rank because the regex was
# reverse-engineered from 5 failing test-set questions and has no held-
# out validation. Callers that know their query is advice-seeking may
# explicitly pass ``bm25_weight=0`` to _hybrid_rank / search_memories.
#
# Kept here as a documented building block for future experimentation
# — e.g. a query-classifier pipeline, or an opt-in env flag.
_PREFERENCE_QUERY_RE = re.compile(
    r"\b("
    r"any (advice|tips|suggestions|ideas|recommendations|thoughts)"
    r"|(do|did|does) you (have|recommend|suggest|know)"
    r"|what (should|would) i"
    r"|what (is|are|was) (a|the|some) (good|best|effective)"
    r"|what's (a|the|some|an effective|a good)"
    r"|how (can|do|should) i"
    r"|i've been (thinking|feeling|struggling|having|trying|looking|working)"
    r"|i'm (thinking|trying|planning|preparing|prepping|looking|working)"
    r"|can you (recommend|suggest)"
    r"|could you (help|brainstorm|suggest|recommend|draft)"
    r"|i need (to|some)|recommend|suggestion"
    r")\b",
    re.IGNORECASE,
)


def _is_preference_query(query: str) -> bool:
    """Heuristic — does this query look like advice-seeking?

    Experimental. Derived from LongMemEval / ConvoMem test-set failures
    where BM25 keyword overlap reliably over-boosted noise sessions. See
    _PREFERENCE_QUERY_RE for usage caveats; not auto-applied.
    """
    return bool(_PREFERENCE_QUERY_RE.search(query or ""))


def _first_or_empty(results, key: str) -> list:
    """Return the first inner list of a query result field, or [].

    Accepts both the typed :class:`QueryResult` (attribute access) and the
    pre-typed chroma dict shape; this polymorphism is retained so test mocks
    still work and callers mid-migration do not crash. Preserves the empty-
    collection semantics from issue #195: when no queries returned hits, the
    outer list may be empty and indexing ``[0]`` would raise.
    """
    outer = getattr(results, key, None) if not isinstance(results, dict) else results.get(key)
    if not outer:
        return []
    return outer[0] or []


def _tokenize(text: str) -> list:
    """Tokenize for BM25 — word-level for Latin, bigram for CJK.

    CJK scripts lack whitespace word boundaries, so a single ``\\w{2,}``
    regex treats an entire Chinese sentence as one token.  BM25 then fails
    because a query token like ``"之前做的飞书回复表情的补丁"`` will never
    exactly match any document token.

    Fix: detect CJK runs and split them into overlapping character bigrams
    (e.g. ``"飞书回复"`` → ``["飞书", "书回", "回复"]``).  Bigrams are the
    smallest unit that preserves meaningful Chinese word fragments (most
    Chinese words are 2 characters) while staying dependency-free (no jieba).

    Tolerates ``None`` documents — Chroma can return ``None`` in the
    ``documents`` field for drawers without text content.
    """
    if not text:
        return []
    lower = text.lower()
    tokens: list = []
    for raw_token in _TOKEN_RE.findall(lower):
        cjk_runs = _CJK_RE.findall(raw_token)
        if not cjk_runs:
            # Pure Latin / digits / Cyrillic — keep as-is
            tokens.append(raw_token)
            continue
        # Mixed or pure CJK token: extract bigrams from CJK runs,
        # keep non-CJK fragments as word tokens.
        pos = 0
        for run in cjk_runs:
            idx = raw_token.find(run, pos)
            # Non-CJK prefix between previous run and this one
            if idx > pos:
                prefix = raw_token[pos:idx]
                if len(prefix) >= 2:
                    tokens.append(prefix)
            # CJK bigrams (+ single-char fallback for 1-char runs)
            if len(run) == 1:
                tokens.append(run)
            else:
                for i in range(len(run) - 1):
                    tokens.append(run[i : i + 2])
            pos = idx + len(run)
        # Non-CJK suffix after last CJK run
        if pos < len(raw_token):
            suffix = raw_token[pos:]
            if len(suffix) >= 2:
                tokens.append(suffix)
    return tokens


def _bm25_scores(
    query: str,
    documents: list,
    k1: float = 1.5,
    b: float = 0.75,
) -> list:
    """Compute Okapi-BM25 scores for ``query`` against each document.

    IDF is computed over the *provided corpus* using the Lucene/BM25+
    smoothed formula ``log((N - df + 0.5) / (df + 0.5) + 1)``, which is
    always non-negative. This is well-defined for re-ranking a small
    candidate set returned by vector retrieval — IDF then reflects how
    discriminative each query term is *within the candidates*, exactly
    what's needed to reorder them.

    Parameters mirror Okapi-BM25 conventions:
        k1 — term-frequency saturation (1.2-2.0 typical, 1.5 default)
        b  — length normalization (0.0 = none, 1.0 = full, 0.75 default)

    Returns a list of scores in the same order as ``documents``.
    """
    n_docs = len(documents)
    query_terms = set(_tokenize(query))
    if not query_terms or n_docs == 0:
        return [0.0] * n_docs

    tokenized = [_tokenize(d) for d in documents]
    doc_lens = [len(toks) for toks in tokenized]
    if not any(doc_lens):
        return [0.0] * n_docs
    avgdl = sum(doc_lens) / n_docs or 1.0

    # Document frequency: how many docs contain each query term?
    df = {term: 0 for term in query_terms}
    for toks in tokenized:
        seen = set(toks) & query_terms
        for term in seen:
            df[term] += 1

    idf = {term: math.log((n_docs - df[term] + 0.5) / (df[term] + 0.5) + 1) for term in query_terms}

    scores = []
    for toks, dl in zip(tokenized, doc_lens):
        if dl == 0:
            scores.append(0.0)
            continue
        tf: dict = {}
        for t in toks:
            if t in query_terms:
                tf[t] = tf.get(t, 0) + 1
        score = 0.0
        for term, freq in tf.items():
            num = freq * (k1 + 1)
            den = freq + k1 * (1 - b + b * dl / avgdl)
            score += idf[term] * num / den
        scores.append(score)
    return scores


def _hybrid_rank(
    results: list,
    query: str,
    vector_weight: float = 0.8,
    bm25_weight: float = 0.2,
    preferred_wing: str = None,
    wing_boost: float = 0.15,
) -> list:
    """Re-rank ``results`` by a convex combination of vector similarity and BM25.

    * Vector similarity uses absolute cosine sim ``max(0, 1 - distance)`` —
      ChromaDB's hnsw cosine distance lives in ``[0, 2]`` (0 = identical).
      Absolute (not relative-to-max) means adding/removing a candidate
      can't reshuffle the others.
    * BM25 is real Okapi-BM25 with corpus-relative IDF over the candidates
      themselves. Since the absolute scale is unbounded, BM25 is min-max
      normalized within the candidate set so weights are commensurable.
    * When ``preferred_wing`` is set, results from that wing receive an
      additive ``wing_boost`` (default 0.15). This soft-prioritizes the
      active wing without filtering out cross-wing results that score high
      on their own merit.

    Default weights (0.8 / 0.2) tuned on LongMemEval 500q sweep
    (benchmarks/mempalace_eval). At higher BM25 weights, sessions whose
    text happens to share many surface tokens with the query (open-domain
    dialog chunks) displace the user's actual preference / advice
    sessions whose answer-bearing language is paraphrased rather than
    quoted. The 0.2 setting wins R@1 / R@5 / NDCG@5 on the 500-question
    test set; preference-type R@5 climbs from 0.800 (at 0.4) → 0.900,
    while user / temporal categories' BM25-derived gains are preserved.

    Mutates each result dict to add ``bm25_score`` and reorders the list
    in place. Returns the same list for convenience.
    """
    if not results:
        return results

    docs = [r.get("text", "") for r in results]
    bm25_raw = _bm25_scores(query, docs)
    max_bm25 = max(bm25_raw) if bm25_raw else 0.0
    bm25_norm = [s / max_bm25 for s in bm25_raw] if max_bm25 > 0 else [0.0] * len(bm25_raw)

    scored = []
    for r, raw, norm in zip(results, bm25_raw, bm25_norm):
        vec_sim = max(0.0, 1.0 - r.get("distance", 1.0))
        r["bm25_score"] = round(raw, 3)
        score = vector_weight * vec_sim + bm25_weight * norm
        if preferred_wing and r.get("wing") == preferred_wing:
            score += wing_boost
        scored.append((score, r))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    results[:] = [r for _, r in scored]
    return results


def build_where_filter(wing: str = None, room: str = None, hall: str = None) -> dict:
    """Build ChromaDB where filter for wing/room/hall filtering."""
    clauses = []
    if wing:
        clauses.append({"wing": wing})
    if room:
        clauses.append({"room": room})
    if hall:
        clauses.append({"hall": hall})
    if len(clauses) == 0:
        return {}
    if len(clauses) == 1:
        return clauses[0]
    return {"$and": clauses}


def _extract_drawer_ids_from_closet(closet_doc: str) -> list:
    """Parse all `→drawer_id_a,drawer_id_b` pointers out of a closet document.

    Preserves order and dedupes.
    """
    seen: dict = {}
    for match in _CLOSET_DRAWER_REF_RE.findall(closet_doc):
        for did in match.split(","):
            did = did.strip()
            if did and did not in seen:
                seen[did] = None
    return list(seen.keys())


def _expand_with_neighbors(drawers_col, matched_doc: str, matched_meta: dict, radius: int = 1):
    """Expand a matched drawer with its ±radius sibling chunks in the same source file.

    Motivation — "drawer-grep context" feature: a closet hit returns one
    drawer, but the chunk boundary may clip mid-thought (e.g., the matched
    chunk says "here's a breakdown:" and the actual breakdown lives in the
    next chunk). Fetching the small neighborhood around the match gives
    callers enough context without forcing a follow-up ``get_drawer`` call.

    Returns a dict with:
        ``text``            combined chunks in chunk_index order
        ``drawer_index``    the matched chunk's index in the source file
        ``total_drawers``   total drawer count for the source file (or None)

    On any ChromaDB failure or missing metadata, falls back to returning the
    matched drawer alone so search never breaks because neighbor expansion
    failed.
    """
    src = matched_meta.get("source_file")
    chunk_idx = matched_meta.get("chunk_index")
    if not src or not isinstance(chunk_idx, int):
        return {"text": matched_doc, "drawer_index": chunk_idx, "total_drawers": None}

    target_indexes = [chunk_idx + offset for offset in range(-radius, radius + 1)]
    try:
        neighbors = drawers_col.get(
            where={
                "$and": [
                    {"source_file": src},
                    {"chunk_index": {"$in": target_indexes}},
                ]
            },
            include=["documents", "metadatas"],
        )
    except Exception:
        return {"text": matched_doc, "drawer_index": chunk_idx, "total_drawers": None}

    indexed_docs = []
    for doc, meta in zip(neighbors.documents, neighbors.metadatas):
        ci = meta.get("chunk_index")
        if isinstance(ci, int):
            indexed_docs.append((ci, doc))
    indexed_docs.sort(key=lambda pair: pair[0])

    if not indexed_docs:
        combined_text = matched_doc
    else:
        combined_text = "\n\n".join(doc for _, doc in indexed_docs)

    # Cheap total_drawers lookup: metadata-only scan of the source file.
    total_drawers = None
    try:
        all_meta = drawers_col.get(where={"source_file": src}, include=["metadatas"])
        total_drawers = len(all_meta.ids) if all_meta.ids else None
    except Exception:
        pass

    return {
        "text": combined_text,
        "drawer_index": chunk_idx,
        "total_drawers": total_drawers,
    }


def search(query: str, palace_path: str, wing: str = None, room: str = None, n_results: int = 5):
    """
    Search the palace. Returns verbatim drawer content.
    Optionally filter by wing (project) or room (aspect).
    """
    try:
        col = get_collection(palace_path, create=False)
    except Exception:
        print(f"\n  No palace found at {palace_path}")
        print("  Run: mempalace init <dir> then mempalace mine <dir>")
        raise SearchError(f"No palace found at {palace_path}")

    where = build_where_filter(wing, room)

    try:
        kwargs = {
            "query_texts": [query],
            "n_results": n_results,
            "include": ["documents", "metadatas", "distances"],
        }
        if where:
            kwargs["where"] = where

        results = col.query(**kwargs)

    except Exception as e:
        print(f"\n  Search error: {e}")
        raise SearchError(f"Search error: {e}") from e

    docs = _first_or_empty(results, "documents")
    metas = _first_or_empty(results, "metadatas")
    dists = _first_or_empty(results, "distances")

    if not docs:
        print(f'\n  No results found for: "{query}"')
        return

    print(f"\n{'=' * 60}")
    print(f'  Results for: "{query}"')
    if wing:
        print(f"  Wing: {wing}")
    if room:
        print(f"  Room: {room}")
    print(f"{'=' * 60}\n")

    for i, (doc, meta, dist) in enumerate(zip(docs, metas, dists), 1):
        similarity = round(max(0.0, 1 - dist), 3)
        meta = meta or {}
        source = Path(meta.get("source_file", "?")).name
        wing_name = meta.get("wing", "?")
        room_name = meta.get("room", "?")

        print(f"  [{i}] {wing_name} / {room_name}")
        print(f"      Source: {source}")
        print(f"      Match:  {similarity}")
        print()
        # Print the verbatim text, indented
        for line in doc.strip().split("\n"):
            print(f"      {line}")
        print()
        print(f"  {'─' * 56}")

    print()


def _enrich_closet_hits(hits, drawers_col, query):
    """Drawer-grep enrichment for closet-boosted hits."""
    max_chars = 10000
    for h in hits:
        if h.get("matched_via") == "drawer":
            continue
        full_source = h.get("_source_file_full") or ""
        if not full_source:
            continue
        try:
            source_drawers = drawers_col.get(
                where={"source_file": full_source},
                include=["documents", "metadatas"],
            )
        except Exception:
            continue
        docs = source_drawers.documents
        metas_ = source_drawers.metadatas
        if len(docs) <= 1:
            continue
        indexed = []
        for idx, (d, m) in enumerate(zip(docs, metas_)):
            ci = m.get("chunk_index", idx) if isinstance(m, dict) else idx
            if not isinstance(ci, int):
                ci = idx
            indexed.append((ci, d))
        indexed.sort(key=lambda p: p[0])
        ordered_docs = [d for _, d in indexed]
        query_terms = set(_tokenize(query))
        best_idx, best_score = 0, -1
        for idx, d in enumerate(ordered_docs):
            s = sum(1 for t in query_terms if t in d.lower())
            if s > best_score:
                best_score, best_idx = s, idx
        start = max(0, best_idx - 1)
        end = min(len(ordered_docs), best_idx + 2)
        expanded = "\n\n".join(ordered_docs[start:end])
        if len(expanded) > max_chars:
            expanded = (
                expanded[:max_chars] + f"\n\n[...truncated. {len(ordered_docs)} total drawers. "
                "Use mempalace_get_drawer for full content.]"
            )
        h["text"] = expanded
        h["drawer_index"] = best_idx
        h["total_drawers"] = len(ordered_docs)


def _keyword_recall(col, query, where, exclude_ids, limit=15):
    """Fetch drawers matching query keywords via ChromaDB $contains.

    Returns a list of (doc, meta, sentinel_distance) tuples for docs not
    already in *exclude_ids*. The sentinel distance (1.0) is a neutral
    value — BM25 scoring in _hybrid_rank will properly weight these.

    Case handling: ChromaDB's ``$contains`` is case-sensitive, but ``_tokenize``
    lowercases its output. Without variant expansion, a query like
    "MemPalace" (lower-cased to "mempalace") misses drawers that store the
    proper-noun form. We try each keyword in three variants — lowercase
    (what tokenize gives us), titlecase (common in prose: "Melanie"), and
    uppercase (acronyms: "MCP") — until we hit *limit* candidates or
    exhaust the keyword budget. CJK tokens are case-invariant, so the
    extra passes are cheap no-ops for them.
    """
    tokens = _tokenize(query)
    keywords = [t for t in tokens if len(t) >= 2][:5]
    if not keywords:
        return []

    results = []
    seen = set(exclude_ids)
    for kw in keywords:
        # Dedup variants: avoids a redundant extra call for CJK / digit tokens.
        variants = list(dict.fromkeys([kw, kw.title(), kw.upper()]))
        for variant in variants:
            try:
                r = col.get(
                    where_document={"$contains": variant},
                    where=where if where else None,
                    include=["documents", "metadatas"],
                    limit=limit,
                )
            except Exception:
                continue
            for did, doc, meta in zip(
                r.get("ids", []), r.get("documents", []), r.get("metadatas", [])
            ):
                if did not in seen:
                    seen.add(did)
                    results.append((doc, meta or {}, 1.0))
            if len(results) >= limit:
                break
        if len(results) >= limit:
            break
    return results[:limit]


def search_memories(
    query: str,
    palace_path: str,
    wing: str = None,
    room: str = None,
    hall: str = None,
    n_results: int = 5,
    max_distance: float = 0.0,
    preferred_wing: str = None,
    after: str = None,
    extra_queries: list = None,
) -> dict:
    """Programmatic search — returns a dict instead of printing.

    Used by the MCP server and other callers that need data.

    Args:
        query: Natural language search query.
        palace_path: Path to the ChromaDB palace directory.
        wing: Optional wing filter.
        room: Optional room filter.
        hall: Optional hall filter (e.g. "hall_diary" for personal-fact recall).
        n_results: Max results to return.
        max_distance: Max cosine distance threshold. The palace collection uses
            cosine distance (hnsw:space=cosine) — 0 = identical, 2 = opposite.
            Results with distance > this value are filtered out. A value of
            0.0 disables filtering. Typical useful range: 0.3–1.0.
        after: ISO date string (e.g. "2026-04-16"). Post-filter results
            to only include memories filed on or after this date.
    """
    try:
        drawers_col = get_collection(palace_path, create=False)
    except Exception as e:
        logger.error("No palace found at %s: %s", palace_path, e)
        return {
            "error": "No palace found",
            "hint": "Run: mempalace init <dir> && mempalace mine <dir>",
        }

    where = build_where_filter(wing, room, hall)

    # Hybrid retrieval: always query drawers directly (the floor), then use
    # closet hits to boost rankings. Closets are a ranking SIGNAL, never a
    # GATE — direct drawer search is always the baseline.
    #
    # This avoids the "weak-closets regression" where narrative content
    # produces low-signal closets (regex extraction matches few topics)
    # and closet-first routing hides drawers that direct search would find.
    # When time-filtering, over-fetch more aggressively since most results
    # will be filtered out by the post-search date check.
    over_fetch = n_results * 10 if after else n_results * 6
    per_query_fetch = max(over_fetch // 2, n_results * 3)

    all_queries = [query] + (extra_queries or [])
    all_queries = list(dict.fromkeys(q for q in all_queries if q and q.strip()))

    merged: dict = {}

    for q in all_queries:
        try:
            dkwargs = {
                "query_texts": [q],
                "n_results": per_query_fetch,
                "include": ["documents", "metadatas", "distances"],
            }
            if where:
                dkwargs["where"] = where
            qresults = drawers_col.query(**dkwargs)
            for did, doc, meta, dist in zip(
                _first_or_empty(qresults, "ids"),
                _first_or_empty(qresults, "documents"),
                _first_or_empty(qresults, "metadatas"),
                _first_or_empty(qresults, "distances"),
            ):
                if did not in merged or dist < merged[did][2]:
                    merged[did] = (doc, meta, dist)
        except Exception as e:
            err_str = str(e)
            if "embed" in err_str.lower() or "SSL" in err_str or "CERTIFICATE" in err_str:
                return {"error": f"Embedding error (check MEMPAL_EMBEDDING_MODEL config): {e}"}
            if not merged:
                return {"error": f"Search error: {e}"}

    keyword_ids = set(merged.keys())
    all_keyword_queries = " ".join(all_queries)
    keyword_hits = _keyword_recall(
        drawers_col, all_keyword_queries, where, keyword_ids, limit=n_results * 3
    )

    # Gather closet hits (best-per-source) to build a boost lookup.
    closet_boost_by_source: dict = {}  # source_file -> (rank, closet_dist, preview)
    try:
        closets_col = get_closets_collection(palace_path, create=False)
        ckwargs = {
            "query_texts": [query],
            "n_results": n_results * 2,
            "include": ["documents", "metadatas", "distances"],
        }
        if where:
            ckwargs["where"] = where
        closet_results = closets_col.query(**ckwargs)
        for rank, (cdoc, cmeta, cdist) in enumerate(
            zip(
                _first_or_empty(closet_results, "documents"),
                _first_or_empty(closet_results, "metadatas"),
                _first_or_empty(closet_results, "distances"),
            )
        ):
            cmeta = cmeta or {}
            source = cmeta.get("source_file", "")
            if source and source not in closet_boost_by_source:
                closet_boost_by_source[source] = (rank, cdist, cdoc[:200])
    except Exception:
        pass  # no closets yet — hybrid degrades to pure drawer search

    # Rank-based boost. The ordinal signal ("which closet matched best") is
    # more reliable than absolute distance on narrative content, where
    # closet distances cluster in 1.2-1.5 range regardless of match quality.
    CLOSET_RANK_BOOSTS = [0.40, 0.25, 0.15, 0.08, 0.04]
    CLOSET_DISTANCE_CAP = 1.5  # cosine dist > 1.5 = too weak to use as signal

    scored: list = []
    for doc, meta, dist in merged.values():
        # Filter on raw distance before rounding to avoid precision loss.
        if max_distance > 0.0 and dist > max_distance:
            continue

        meta = meta or {}

        # Time filter: skip drawers filed before the requested date.
        if after:
            filed_at = meta.get("filed_at", "") or ""
            if filed_at < after:
                continue

        source = meta.get("source_file", "") or ""
        boost = 0.0
        matched_via = "drawer"
        closet_preview = None
        if source in closet_boost_by_source:
            c_rank, c_dist, c_preview = closet_boost_by_source[source]
            if c_dist <= CLOSET_DISTANCE_CAP and c_rank < len(CLOSET_RANK_BOOSTS):
                boost = CLOSET_RANK_BOOSTS[c_rank]
                matched_via = "drawer+closet"
                closet_preview = c_preview

        effective_dist = dist - boost
        entry = {
            "text": doc,
            "wing": meta.get("wing", "unknown"),
            "room": meta.get("room", "unknown"),
            "source_file": Path(source).name if source else "?",
            "created_at": meta.get("filed_at", "unknown"),
            "similarity": round(max(0.0, 1 - effective_dist), 3),
            "distance": round(dist, 4),
            "effective_distance": round(effective_dist, 4),
            "closet_boost": round(boost, 3),
            "matched_via": matched_via,
            # Internal: retain the full source_file path + chunk_index so the
            # enrichment step below doesn't have to reverse-lookup via
            # basename-suffix matching (which silently collides when two
            # files share a basename across different directories).
            "_sort_key": effective_dist,
            "_source_file_full": source,
            "_chunk_index": meta.get("chunk_index"),
        }
        if closet_preview:
            entry["closet_preview"] = closet_preview
        scored.append(entry)

    for doc, meta, dist in keyword_hits:
        if max_distance > 0.0 and dist > max_distance:
            continue
        if after:
            filed_at = meta.get("filed_at", "") or ""
            if filed_at < after:
                continue
        source = meta.get("source_file", "") or ""
        scored.append(
            {
                "text": doc,
                "wing": meta.get("wing", "unknown"),
                "room": meta.get("room", "unknown"),
                "source_file": Path(source).name if source else "?",
                "created_at": meta.get("filed_at", "unknown"),
                "similarity": 0.0,
                "distance": round(dist, 4),
                "effective_distance": round(dist, 4),
                "closet_boost": 0.0,
                "matched_via": "keyword",
                "_sort_key": dist,
                "_source_file_full": source,
                "_chunk_index": meta.get("chunk_index"),
            }
        )

    # BM25 hybrid re-rank on the FULL candidate set, then truncate.
    scored = _hybrid_rank(scored, query, preferred_wing=preferred_wing)
    hits = scored[:n_results]

    # Drawer-grep enrichment for closet-boosted hits.
    _enrich_closet_hits(hits, drawers_col, query)

    for h in hits:
        h.pop("_sort_key", None)
        h.pop("_source_file_full", None)
        h.pop("_chunk_index", None)

    return {
        "query": query,
        "filters": {"wing": wing, "room": room},
        "total_before_filter": len(merged) + len(keyword_hits),
        "results": hits,
    }
