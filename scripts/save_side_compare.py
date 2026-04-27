#!/usr/bin/env python3
"""Save-side before/after comparison for fix/save-side-alignment.

Quantifies the three save-path changes with synthetic-but-realistic
inputs. No external dataset required.

What this measures:

1. Auto-save activation rate
   - "before": MEMPAL_RECALL_LLM=1 required; if env unset, save silently
     skipped even when endpoint+model present.
   - "after": endpoint-probe based (default-on); only MEMPAL_LLM=0 disables.

2. Hall taxonomy alignment
   - "before": emotions/consciousness/memory/technical/identity/family/
     creative — drifted from the official doc's 5 classes; many drawers
     fall into "general" because the keywords don't match decision/event
     language.
   - "after": hall_facts/events/discoveries/preferences/advice
     (doc-aligned), with bilingual EN+ZH keyword stems.

3. Cross-wing same-room tunnel coverage
   - "before": tunnels created only when the LLM emits them in the
     `tunnels` array of the save prompt — depends on LLM judgment.
   - "after": auto_link_shared_rooms runs after every save and
     deterministically links same-named rooms across wings (with
     stoplist + popularity cap to prevent N×N edge explosions).

Run:
    python scripts/save_side_compare.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

# Isolate from any pre-existing ~/.mempalace/config.json that may still
# have the legacy hall_keywords block from a prior `mempalace init`.
# Without this redirect, MempalaceConfig() reads the stored old taxonomy
# and detect_hall() ignores the new defaults — masking the real fix.
_ISOLATED_HOME = tempfile.mkdtemp(prefix="mempalace_compare_")
os.environ["HOME"] = _ISOLATED_HOME

from mempalace.config import VALID_HALLS  # noqa: E402
from mempalace.miner import detect_hall as new_detect_hall  # noqa: E402

# Reset the lazy keyword cache so it picks up the isolated HOME above.
import mempalace.miner as _miner_mod  # noqa: E402

_miner_mod._HALL_KEYWORDS_CACHE = None


# ── Old taxonomy snapshot (from dev branch, pre-fix/save-side-alignment) ──
OLD_HALL_KEYWORDS = {
    "emotions": [
        "scared", "afraid", "worried", "happy", "sad", "love", "hate",
        "feel", "cry", "tears",
    ],
    "consciousness": [
        "consciousness", "conscious", "aware", "real", "genuine", "soul",
        "exist", "alive",
    ],
    "memory": ["memory", "remember", "forget", "recall", "archive", "palace", "store"],
    "technical": [
        "code", "python", "script", "bug", "error", "function", "api",
        "database", "server",
    ],
    "identity": ["identity", "name", "who am i", "persona", "self"],
    "family": ["family", "kids", "children", "daughter", "son", "parent", "mother", "father"],
    "creative": ["game", "gameplay", "player", "app", "design", "art", "music", "story"],
}


def old_detect_hall(content: str) -> str:
    """Replica of the pre-fix detect_hall using the old taxonomy."""
    content_lower = content[:3000].lower()
    scores = {}
    for hall, keywords in OLD_HALL_KEYWORDS.items():
        score = sum(1 for kw in keywords if kw in content_lower)
        if score > 0:
            scores[hall] = score
    if scores:
        return max(scores, key=scores.get)
    return "general"


# ── Synthetic drawer corpus — types of memories the doc names ──
DRAWERS = [
    # Decisions / facts
    ("Decided to migrate the auth service from Auth0 to Clerk this quarter", "fact"),
    ("Chose Apollo Server over GraphQL Yoga because of better caching", "fact"),
    ("Locked in the rate limit at 100 req/min — calibrated to PG pool of 200", "fact"),
    ("团队决定采用 Next.js 14 + tRPC + Prisma 作为新项目技术栈", "fact"),
    ("最终敲定移动端使用 React Native 不是 Flutter", "fact"),
    # Events
    ("Debugged the indexer crash, traced it to a missing fsync, deployed the fix", "event"),
    ("Ran the migration script in staging — completed in 14 minutes, no errors", "event"),
    ("Encountered a 504 in the orders endpoint, root-caused to a missing index", "event"),
    ("修复了搜索接口 504 超时问题,部署到 staging 环境", "event"),
    ("上线了新的 CI 流水线,执行了首次全量构建", "event"),
    # Discoveries
    ("Realized the latency was actually GraphQL N+1 on lineItems, not the DB", "discovery"),
    ("Found out the staging instance was running an older Node version", "discovery"),
    ("Turns out the cache was using stale keys after the schema rename", "discovery"),
    ("发现原来内存泄漏是 connection pool 没有释放导致的", "discovery"),
    ("意识到那个性能瓶颈其实是日志同步写盘的问题", "discovery"),
    # Preferences
    ("I always prefer tabs over spaces and never use semicolons in TS", "preference"),
    ("PR merge policy: always squash-merge, never rebase, never merge-commit", "preference"),
    ("User favors small focused PRs over large bundled refactors", "preference"),
    ("用户偏好使用中文注释,从不使用英文驼峰命名风格", "preference"),
    ("总是先写测试再写实现,习惯性 TDD", "preference"),
    # Advice
    ("Priya recommends Clerk over Auth0 because of better RN SDK support", "advice"),
    ("Recommend switching to pnpm for monorepo — better symlink semantics", "advice"),
    ("Best practice: never approve a deploy on a Friday afternoon", "advice"),
    ("建议把数据库迁移放在维护窗口执行,推荐凌晨 2-4 点", "advice"),
    ("Priya 批准了 Clerk 方案,驳回了继续用 Auth0 的提议", "advice"),
]

# Map source-of-truth labels → expected hall
EXPECTED_HALL = {
    "fact": "hall_facts",
    "event": "hall_events",
    "discovery": "hall_discoveries",
    "preference": "hall_preferences",
    "advice": "hall_advice",
}


# ── 1. Auto-save activation rate ──
def measure_save_activation():
    """Probe-style env scenarios. Same env, two gate implementations."""
    scenarios = [
        # (label, env-overrides)
        ("fresh install, only endpoint+model set", {
            "MEMPAL_LLM_ENDPOINT": "http://127.0.0.1:4000/v1",
            "MEMPAL_LLM_MODEL": "gemini-3.1-flash-lite-preview",
            "MEMPAL_RECALL_LLM": None,  # explicitly unset
        }),
        ("fresh install, only legacy aliases set", {
            "MEMPAL_RECALL_ENDPOINT": "http://127.0.0.1:4000/v1",
            "MEMPAL_RECALL_MODEL": "gemini-3.1-flash-lite-preview",
            "MEMPAL_RECALL_LLM": None,
        }),
        ("user opted in legacy: MEMPAL_RECALL_LLM=1 + endpoint", {
            "MEMPAL_RECALL_LLM": "1",
            "MEMPAL_RECALL_ENDPOINT": "http://127.0.0.1:4000/v1",
            "MEMPAL_RECALL_MODEL": "gemini-3.1-flash-lite-preview",
        }),
        ("user opted out canonical: MEMPAL_LLM=0 + endpoint", {
            "MEMPAL_LLM": "0",
            "MEMPAL_LLM_ENDPOINT": "http://127.0.0.1:4000/v1",
            "MEMPAL_LLM_MODEL": "gemini-3.1-flash-lite-preview",
        }),
        ("no LLM env at all", {}),
    ]

    keys_to_clear = (
        "MEMPAL_LLM",
        "MEMPAL_LLM_ENDPOINT",
        "MEMPAL_LLM_MODEL",
        "MEMPAL_LLM_KEY",
        "MEMPAL_RECALL_LLM",
        "MEMPAL_RECALL_ENDPOINT",
        "MEMPAL_RECALL_MODEL",
        "MEMPAL_RECALL_KEY",
    )

    rows = []
    for label, env in scenarios:
        # Reset env
        for k in keys_to_clear:
            os.environ.pop(k, None)
        for k, v in env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

        # "before" gate: literal env check at hooks_cli.py:1382 in dev
        old_gate_fires = os.environ.get("MEMPAL_RECALL_LLM", "") == "1"

        # "after" gate: probe-based is_enabled()
        from importlib import reload

        import mempalace.recall_llm as recall_llm

        reload(recall_llm)
        new_gate_fires = recall_llm.is_enabled()

        rows.append((label, old_gate_fires, new_gate_fires))

    # Restore baseline (otherwise subsequent measurements are dirty)
    for k in keys_to_clear:
        os.environ.pop(k, None)

    return rows


# ── 2. Hall classification quality ──
def measure_hall_quality():
    """For the same drawer corpus, report classification rate to a doc-aligned class.

    The "before" detector cannot output the doc's 5-class names, so the
    fairest framing is: how often does each detector classify the drawer
    into a meaningful (non-fallback) bucket of its own taxonomy, AND what
    fraction of those align with the doc's intended class?
    """
    old_correct_doc_hall = 0  # detector output that *also* maps to expected hall
    old_fallback_general = 0
    new_correct = 0
    new_fallback_to_default = 0

    detail = []
    for content, kind in DRAWERS:
        expected = EXPECTED_HALL[kind]
        old = old_detect_hall(content)
        new = new_detect_hall(content)

        # The old taxonomy cannot natively produce hall_facts/events/etc.,
        # so by definition old never matches `expected` exactly. The closest
        # we can do is count the "general" rate (no signal at all).
        if old == "general":
            old_fallback_general += 1

        if new == expected:
            new_correct += 1
        if new == "hall_events" and expected != "hall_events":
            # falling into the default fallback when the right class
            # was something else
            new_fallback_to_default += 1

        detail.append((kind, expected, old, new))

    return {
        "n": len(DRAWERS),
        "old_general_fallback_rate": old_fallback_general / len(DRAWERS),
        "new_correct_class_rate": new_correct / len(DRAWERS),
        "new_default_fallback_rate": new_fallback_to_default / len(DRAWERS),
        "detail": detail,
    }


# ── 3. Cross-wing same-room tunnel coverage ──
def measure_tunnel_coverage():
    """Stub the build_graph view of the palace, then count tunnels each path
    would emit for the same set of just-saved (wing, room) pairs."""
    from unittest.mock import MagicMock, patch

    with patch.dict("sys.modules", {"chromadb": MagicMock()}):
        import mempalace.palace_graph as palace_graph

    # Synthetic palace state: same 4 rooms appear in 2 wings each.
    palace_nodes = {
        "auth-migration":   {"wings": ["wing_alpha", "wing_bravo"], "halls": [], "count": 4, "dates": []},
        "graphql-switch":   {"wings": ["wing_alpha", "wing_charlie"], "halls": [], "count": 3, "dates": []},
        "ci-pipeline":      {"wings": ["wing_alpha", "wing_bravo"], "halls": [], "count": 5, "dates": []},
        "billing-redesign": {"wings": ["wing_alpha", "wing_delta"], "halls": [], "count": 2, "dates": []},
        "general":          {"wings": ["wing_alpha", "wing_bravo", "wing_charlie", "wing_delta"], "halls": [], "count": 80, "dates": []},
    }

    saved_pairs = [
        ("wing_alpha", "auth-migration"),
        ("wing_alpha", "graphql-switch"),
        ("wing_alpha", "ci-pipeline"),
        ("wing_alpha", "billing-redesign"),
        ("wing_alpha", "general"),  # generic — must be skipped
    ]

    # ── Before (pre-fix): only LLM-emitted tunnels. Worst-case observed in
    # production: LLM emits zero tunnels in many save cycles. Best-case: it
    # emits 1-3 well-targeted ones. We can't simulate the LLM judgment, so
    # we assume the conservative outcome: 0 deterministic auto-tunnels.
    before_tunnels = 0

    # ── After (this branch): auto_link_shared_rooms runs deterministically.
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        tunnel_file = Path(td) / "tunnels.json"
        original = palace_graph._TUNNEL_FILE
        palace_graph._TUNNEL_FILE = str(tunnel_file)

        original_build = palace_graph.build_graph
        palace_graph.build_graph = lambda col=None, config=None: (palace_nodes, [])
        try:
            created = palace_graph.auto_link_shared_rooms(saved_pairs, col=MagicMock())
        finally:
            palace_graph.build_graph = original_build
            palace_graph._TUNNEL_FILE = original

    return {
        "saved_pairs": len(saved_pairs),
        "rooms_with_cross_wing_match": sum(
            1 for w, r in saved_pairs
            if r in palace_nodes and len(palace_nodes[r]["wings"]) >= 2
        ),
        "before_auto_tunnels": before_tunnels,
        "after_auto_tunnels": len(created),
        "after_tunnel_endpoints": [
            f"{t['source']['wing']}/{t['source']['room']} ↔ {t['target']['wing']}/{t['target']['room']}"
            for t in created
        ],
    }


# ── Render ──
def main():
    print("=" * 70)
    print("Save-side before/after — fix/save-side-alignment")
    print("=" * 70)

    print("\n## 1. Auto-save activation rate")
    print(f"{'scenario':<55} {'before':<10} {'after':<10}")
    print("-" * 75)
    n_before = 0
    n_after = 0
    rows = measure_save_activation()
    for label, old, new in rows:
        print(f"{label:<55} {'YES' if old else 'no':<10} {'YES' if new else 'no':<10}")
        n_before += int(old)
        n_after += int(new)
    print("-" * 75)
    print(f"{'TOTAL save-eligible scenarios':<55} {n_before}/{len(rows):<8} {n_after}/{len(rows)}")

    print("\n## 2. Hall classification (n=25 drawers, 5 per class)")
    h = measure_hall_quality()
    print(f"  before: {h['old_general_fallback_rate']:.0%} drop into 'general'")
    print(f"          (taxonomy cannot natively output hall_facts/events/etc.)")
    print(f"  after:  {h['new_correct_class_rate']:.0%} classified into the EXPECTED doc class")
    print(f"          {h['new_default_fallback_rate']:.0%} fall to default hall_events fallback")

    print("\n  Per-drawer detail (kind / expected / old / new):")
    for kind, expected, old, new in h["detail"]:
        flag = "✓" if new == expected else " "
        print(f"    {flag} {kind:<11} → {expected:<18} | old={old:<14} new={new}")

    print("\n## 3. Cross-wing same-room auto-tunnel coverage")
    t = measure_tunnel_coverage()
    print(f"  saved (wing, room) pairs in this synthetic save:   {t['saved_pairs']}")
    print(f"  pairs where the room exists in another wing:       {t['rooms_with_cross_wing_match']}")
    print(f"  before auto-tunnels (LLM-emitted only, worst case): {t['before_auto_tunnels']}")
    print(f"  after  auto-tunnels (deterministic):                {t['after_auto_tunnels']}")
    print("  endpoints created:")
    for ep in t["after_tunnel_endpoints"]:
        print(f"    • {ep}")

    print()
    print("Note: recall benchmarks (LongMemEval / LoCoMo / ConvoMem) measure")
    print("retrieval against pre-built palaces and are NOT expected to move.")
    print("This branch fixes save-side correctness, not recall algorithms.")


if __name__ == "__main__":
    main()
