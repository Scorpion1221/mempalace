"""Tests for hall detection.

The hall taxonomy follows the official MemPalace doc — five canonical
hall types describing the SHAPE of a memory (rooms cover the topic):

    hall_facts        — decisions, choices locked in, configurations
    hall_events       — sessions, milestones, debugging, deployments
    hall_discoveries  — breakthroughs, insights, surprising findings
    hall_preferences  — habits, likes/dislikes, style choices
    hall_advice       — recommendations, approvals, suggestions

Auto-save prefers the LLM's per-drawer judgment; ``detect_hall()`` is
the bilingual keyword-stem fallback. When no keywords match, the
fallback is ``DEFAULT_HALL_FALLBACK`` (``hall_events``).
"""

import os

import yaml


class TestDetectHall:
    """detect_hall must route content to the canonical 5-class taxonomy."""

    def test_function_exists(self):
        from mempalace.miner import detect_hall

        assert callable(detect_hall)

    def test_facts_content(self):
        from mempalace.miner import detect_hall

        text = "We decided to migrate to Postgres and chose Clerk over Auth0"
        assert detect_hall(text) == "hall_facts"

    def test_events_content(self):
        from mempalace.miner import detect_hall

        text = "Debugged the indexer crash, fixed the OAuth callback, deployed to staging"
        assert detect_hall(text) == "hall_events"

    def test_discoveries_content(self):
        from mempalace.miner import detect_hall

        text = "Discovered the latency was caused by N+1 queries — turns out the resolver fired 200x per request"
        assert detect_hall(text) == "hall_discoveries"

    def test_preferences_content(self):
        from mempalace.miner import detect_hall

        text = "I always prefer tabs over spaces and never use semicolons in TypeScript"
        assert detect_hall(text) == "hall_preferences"

    def test_advice_content(self):
        from mempalace.miner import detect_hall

        text = "Recommend switching to Clerk; Priya suggested we should adopt the new pattern"
        assert detect_hall(text) == "hall_advice"

    def test_chinese_facts_content(self):
        from mempalace.miner import detect_hall

        text = "团队决定采用 Next.js 14，前端选择了 tRPC + Prisma"
        assert detect_hall(text) == "hall_facts"

    def test_chinese_events_content(self):
        from mempalace.miner import detect_hall

        text = "修复了搜索接口超时问题，部署到了 staging 环境，运行正常"
        assert detect_hall(text) == "hall_events"

    def test_general_fallback_to_events(self):
        from mempalace.miner import detect_hall

        text = "The weather is nice today in California"
        # No keyword matches → DEFAULT_HALL_FALLBACK = hall_events
        assert detect_hall(text) == "hall_events"

    def test_highest_score_wins(self):
        from mempalace.miner import detect_hall

        # decided + chose (2) outweighs fixed (1)
        text = "We decided to use Postgres and chose Apollo, then fixed the bug"
        assert detect_hall(text) == "hall_facts"


class TestDrawerHasHallMetadata:
    """When a drawer is created, it must carry a hall field in metadata."""

    def test_add_drawer_includes_hall(self, palace_path):
        from mempalace.palace import get_collection
        from mempalace.miner import add_drawer

        col = get_collection(palace_path)
        add_drawer(
            collection=col,
            wing="test",
            room="general",
            content="Debugged the indexer crash and deployed the fix",
            source_file=os.path.join(palace_path, "test.py"),
            chunk_index=0,
            agent="test",
        )
        results = col.get(limit=1, include=["metadatas"])
        meta = results["metadatas"][0]
        assert "hall" in meta, "Drawer metadata must include 'hall' field"
        assert meta["hall"] == "hall_events"


class TestValidHallsConst:
    """The canonical 5-class taxonomy is exported as VALID_HALLS."""

    def test_valid_halls_exact_set(self):
        from mempalace.config import VALID_HALLS

        assert VALID_HALLS == frozenset(
            {
                "hall_facts",
                "hall_events",
                "hall_discoveries",
                "hall_preferences",
                "hall_advice",
                "hall_diary",
            }
        )

    def test_default_fallback_is_in_valid_halls(self):
        from mempalace.config import DEFAULT_HALL_FALLBACK, VALID_HALLS

        assert DEFAULT_HALL_FALLBACK in VALID_HALLS


class TestConvoMinerWritesHalls:
    """Conversation miner must tag drawers with hall metadata too."""

    def test_convo_miner_drawers_have_hall(self, tmp_dir):
        from mempalace.config import VALID_HALLS
        from mempalace.palace import get_collection
        from mempalace.convo_miner import mine_convos

        palace_dir = os.path.join(tmp_dir, "palace")
        os.makedirs(palace_dir)
        convo_dir = os.path.join(tmp_dir, "convos")
        os.makedirs(convo_dir)
        with open(os.path.join(convo_dir, "session.txt"), "w") as f:
            f.write("> How do I fix the python script bug?\n")
            f.write("Debugged it — the error handler was swallowing the traceback. Fixed.\n")
            f.write("> What about the database migration?\n")
            f.write("Ran the migration script and deployed.\n")

        mine_convos(convo_dir, palace_dir, wing="test", agent="test")

        col = get_collection(palace_dir, create=False)
        results = col.get(limit=10, include=["metadatas"])
        assert len(results["ids"]) > 0, "No drawers created by convo_miner"
        for meta in results["metadatas"]:
            if meta.get("ingest_mode") == "convos":
                assert "hall" in meta, f"Convo drawer missing hall metadata: {meta}"
                assert meta["hall"] in VALID_HALLS, (
                    f"Convo drawer hall {meta['hall']!r} outside canonical "
                    f"VALID_HALLS — taxonomy drift"
                )


class TestDetectHallCaching:
    """detect_hall should cache config to avoid disk reads per drawer."""

    def test_detect_hall_does_not_reread_config(self):
        """After first call, config should be cached — no new MempalaceConfig()."""
        import mempalace.miner as miner_mod

        miner_mod._HALL_KEYWORDS_CACHE = None

        miner_mod.detect_hall("Fixed the indexer bug")
        assert miner_mod._HALL_KEYWORDS_CACHE is not None

        cached_ref = miner_mod._HALL_KEYWORDS_CACHE

        miner_mod.detect_hall("Decided to switch to Postgres")
        assert miner_mod._HALL_KEYWORDS_CACHE is cached_ref


class TestMineProjectWritesHalls:
    """Full mine pipeline must produce drawers with hall metadata."""

    def test_mined_drawers_have_hall(self, tmp_dir):
        from mempalace.config import VALID_HALLS
        from mempalace.palace import get_collection
        from mempalace.miner import mine

        palace_dir = os.path.join(tmp_dir, "palace")
        os.makedirs(palace_dir)
        project_dir = os.path.join(tmp_dir, "project")
        os.makedirs(project_dir)
        config = {"wing": "test", "rooms": [{"name": "general", "description": "all"}]}
        with open(os.path.join(project_dir, "mempalace.yaml"), "w") as f:
            yaml.dump(config, f)
        with open(os.path.join(project_dir, "code.py"), "w") as f:
            f.write("def fix_bug():\n    # Fixed the indexer crash in handler\n    pass\n")

        mine(project_dir, palace_dir, wing_override="test", agent="test")

        col = get_collection(palace_dir, create=False)
        results = col.get(limit=10, include=["metadatas"])
        for meta in results["metadatas"]:
            assert "hall" in meta, f"Drawer missing hall metadata: {meta}"
            assert meta["hall"] in VALID_HALLS, (
                f"Mined drawer hall {meta['hall']!r} outside canonical VALID_HALLS — taxonomy drift"
            )
