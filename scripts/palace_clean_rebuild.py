#!/usr/bin/env python3
"""
Palace clean rebuild: extract from SQLite orphan segments, filter noise, rebuild HNSW.

Usage:
    python3 -u scripts/palace_clean_rebuild.py

Takes ~40 minutes for 40K+ drawers (Gemini embedding via LiteLLM proxy).
"""

import sqlite3
import os
import time

import chromadb

NOISE_MARKERS = [
    "sessionId",
    "parentUuid",
    "file-history-snapshot",
    "isSidechain",
    '"type":"permission',
    '"permissionMode"',
    "preventedContinuation",
    '"type":"file-history',
    '"isSnapshotUpdate"',
]


def log(msg):
    print(msg, flush=True)


def main():
    from mempalace.config import MempalaceConfig
    from mempalace.embedding import get_embedding_function

    cfg = MempalaceConfig()
    palace_path = cfg.palace_path
    db_path = os.path.join(palace_path, "chroma.sqlite3")

    log("=" * 55)
    log("  Palace Clean Rebuild — filter noise + rebuild HNSW")
    log("=" * 55)

    # Step 1: Find the best orphan segment with data
    log("\nStep 1: Finding data in SQLite...")
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    cur.execute("SELECT DISTINCT segment_id FROM embeddings")
    all_segs = [r[0] for r in cur.fetchall()]

    best_seg, best_count = None, 0
    for seg in all_segs:
        cur.execute("SELECT COUNT(*) FROM embeddings WHERE segment_id=?", (seg,))
        cnt = cur.fetchone()[0]
        if cnt > best_count:
            best_count = cnt
            best_seg = seg

    log(f"  Best segment: {best_seg} ({best_count} rows)")

    if best_count == 0:
        log("  No data found in SQLite. Nothing to rebuild.")
        conn.close()
        return

    # Step 2: Extract data
    log("\nStep 2: Extracting drawers...")
    t0 = time.time()

    cur.execute(
        "SELECT id, embedding_id FROM embeddings WHERE segment_id=?",
        (best_seg,),
    )
    id_map = {row[0]: row[1] for row in cur.fetchall()}

    cur.execute(
        "SELECT id, key, string_value, int_value, float_value, bool_value FROM embedding_metadata"
    )
    meta_by_id = {}
    for row in cur.fetchall():
        iid, key, sval, ival, fval, bval = row
        if iid not in id_map:
            continue
        if iid not in meta_by_id:
            meta_by_id[iid] = {}
        if sval is not None:
            meta_by_id[iid][key] = sval
        elif ival is not None:
            meta_by_id[iid][key] = ival
        elif fval is not None:
            meta_by_id[iid][key] = fval
        elif bval is not None:
            meta_by_id[iid][key] = bool(bval)

    conn.close()

    all_ids, all_docs, all_metas = [], [], []
    for iid, eid in id_map.items():
        meta = meta_by_id.get(iid, {})
        doc = meta.pop("chroma:document", None)
        if not doc:
            continue
        all_ids.append(eid)
        all_docs.append(doc)
        all_metas.append(meta)

    log(f"  Extracted {len(all_ids)} drawers in {time.time() - t0:.1f}s")

    # Step 3: Filter noise
    log("\nStep 3: Filtering noise...")
    clean_ids, clean_docs, clean_metas = [], [], []
    noise = 0
    for did, doc, meta in zip(all_ids, all_docs, all_metas):
        head = doc[:400] if doc else ""
        if any(marker in head for marker in NOISE_MARKERS):
            noise += 1
            continue
        clean_ids.append(did)
        clean_docs.append(doc)
        clean_metas.append(meta)
    log(f"  Clean: {len(clean_ids)}, Noise removed: {noise}")

    # Step 4: Delete and recreate
    log("\nStep 4: Deleting old collection...")
    client = chromadb.PersistentClient(path=palace_path)
    try:
        client.delete_collection("mempalace_drawers")
        log("  Deleted")
    except Exception as e:
        log(f"  Delete via API failed ({e}), force-deleting via SQLite...")
        conn2 = sqlite3.connect(db_path)
        c2 = conn2.cursor()
        c2.execute("DELETE FROM collections WHERE name='mempalace_drawers'")
        conn2.commit()
        conn2.close()
        client = chromadb.PersistentClient(path=palace_path)
        log("  Force-deleted")

    log("\nStep 5: Creating clean collection...")
    ef = get_embedding_function()
    col = client.create_collection(
        name="mempalace_drawers",
        metadata={"hnsw:space": "cosine"},
        embedding_function=ef,
    )
    ef_name = type(ef).__name__ if ef else "default"
    log(f"  Created with {ef_name}")

    # Step 6: Upsert
    log(f"\nStep 6: Upserting {len(clean_ids)} clean drawers (batch=100)...")
    t0 = time.time()
    filed, errors = 0, 0
    batch_size = 100

    for i in range(0, len(clean_ids), batch_size):
        bi = clean_ids[i : i + batch_size]
        bd = clean_docs[i : i + batch_size]
        bm = clean_metas[i : i + batch_size]
        try:
            col.upsert(documents=bd, ids=bi, metadatas=bm)
            filed += len(bi)
        except Exception as exc:
            log(f"  ERROR at {i}: {exc}")
            for did, doc, meta in zip(bi, bd, bm):
                try:
                    col.upsert(documents=[doc], ids=[did], metadatas=[meta])
                    filed += 1
                except Exception:
                    errors += 1
        if filed % 1000 == 0 or filed == len(clean_ids):
            elapsed = time.time() - t0
            rate = filed / max(elapsed, 0.01)
            eta = (len(clean_ids) - filed) / max(rate, 0.01)
            log(f"  {filed}/{len(clean_ids)} ({elapsed:.0f}s, ~{eta:.0f}s left)")

    total_time = time.time() - t0
    log(f"\nDone. {filed} clean drawers, {errors} errors, {noise} noise removed.")
    log(f"Total time: {total_time:.0f}s")
    log(f"Final count: {col.count()}")
    log(f"\n{'=' * 55}")


if __name__ == "__main__":
    main()
