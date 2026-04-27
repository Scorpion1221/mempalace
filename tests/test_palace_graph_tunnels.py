"""Tests for explicit tunnel helpers in mempalace.palace_graph."""

from unittest.mock import MagicMock, patch

import pytest

with patch.dict("sys.modules", {"chromadb": MagicMock()}):
    import mempalace.palace_graph as palace_graph


def _use_tmp_tunnel_file(monkeypatch, tmp_path):
    tunnel_file = tmp_path / "tunnels.json"
    monkeypatch.setattr(palace_graph, "_TUNNEL_FILE", str(tunnel_file))
    return tunnel_file


class TestTunnelStorage:
    def test_load_tunnels_missing_file_returns_empty_list(self, tmp_path, monkeypatch):
        _use_tmp_tunnel_file(monkeypatch, tmp_path)
        assert palace_graph._load_tunnels() == []

    def test_load_tunnels_corrupt_file_returns_empty_list(self, tmp_path, monkeypatch):
        tunnel_file = _use_tmp_tunnel_file(monkeypatch, tmp_path)
        tunnel_file.write_text("{not valid json", encoding="utf-8")
        assert palace_graph._load_tunnels() == []

    def test_save_and_load_round_trip(self, tmp_path, monkeypatch):
        _use_tmp_tunnel_file(monkeypatch, tmp_path)
        tunnels = [
            {
                "id": "abc123",
                "source": {"wing": "wing_code", "room": "auth"},
                "target": {"wing": "wing_people", "room": "users"},
                "label": "same concept",
            }
        ]
        palace_graph._save_tunnels(tunnels)
        assert palace_graph._load_tunnels() == tunnels


class TestExplicitTunnels:
    def test_create_tunnel_deduplicates_reverse_order_and_updates_label(
        self, tmp_path, monkeypatch
    ):
        _use_tmp_tunnel_file(monkeypatch, tmp_path)

        first = palace_graph.create_tunnel(
            "wing_code", "auth", "wing_people", "users", label="same concept"
        )
        second = palace_graph.create_tunnel(
            "wing_people", "users", "wing_code", "auth", label="updated label"
        )

        assert first["id"] == second["id"]
        assert len(palace_graph.list_tunnels()) == 1
        assert second["label"] == "updated label"
        assert second["created_at"] == first["created_at"]
        assert "updated_at" in second

    def test_create_tunnel_rejects_empty_names(self, tmp_path, monkeypatch):
        _use_tmp_tunnel_file(monkeypatch, tmp_path)

        with pytest.raises(ValueError):
            palace_graph.create_tunnel("", "auth", "wing_people", "users")

    def test_list_tunnels_filters_by_either_side(self, tmp_path, monkeypatch):
        _use_tmp_tunnel_file(monkeypatch, tmp_path)

        palace_graph.create_tunnel("wing_code", "auth", "wing_people", "users", label="A")
        palace_graph.create_tunnel("wing_ops", "deploy", "wing_people", "users", label="B")

        assert len(palace_graph.list_tunnels()) == 2
        assert len(palace_graph.list_tunnels("wing_people")) == 2
        assert len(palace_graph.list_tunnels("wing_code")) == 1

    def test_delete_tunnel_removes_saved_tunnel(self, tmp_path, monkeypatch):
        _use_tmp_tunnel_file(monkeypatch, tmp_path)

        tunnel = palace_graph.create_tunnel(
            "wing_code", "auth", "wing_people", "users", label="same concept"
        )

        assert palace_graph.delete_tunnel(tunnel["id"]) == {"deleted": tunnel["id"]}
        assert palace_graph.list_tunnels() == []

    def test_follow_tunnels_returns_direction_and_preview(self, tmp_path, monkeypatch):
        _use_tmp_tunnel_file(monkeypatch, tmp_path)

        palace_graph.create_tunnel(
            "wing_code",
            "auth",
            "wing_people",
            "users",
            label="same concept",
            target_drawer_id="drawer_users_1",
        )

        col = MagicMock()
        col.get.return_value = {
            "ids": ["drawer_users_1"],
            "documents": ["A" * 400],
            "metadatas": [{}],
        }

        outgoing = palace_graph.follow_tunnels("wing_code", "auth", col=col)
        assert len(outgoing) == 1
        assert outgoing[0]["direction"] == "outgoing"
        assert outgoing[0]["connected_wing"] == "wing_people"
        assert outgoing[0]["connected_room"] == "users"
        assert outgoing[0]["drawer_id"] == "drawer_users_1"
        assert len(outgoing[0]["drawer_preview"]) == 300

        incoming = palace_graph.follow_tunnels("wing_people", "users", col=col)
        assert len(incoming) == 1
        assert incoming[0]["direction"] == "incoming"
        assert incoming[0]["connected_wing"] == "wing_code"

    def test_follow_tunnels_returns_connections_even_if_collection_lookup_fails(
        self, tmp_path, monkeypatch
    ):
        _use_tmp_tunnel_file(monkeypatch, tmp_path)

        palace_graph.create_tunnel(
            "wing_code",
            "auth",
            "wing_people",
            "users",
            label="same concept",
            target_drawer_id="drawer_users_1",
        )

        col = MagicMock()
        col.get.side_effect = RuntimeError("boom")

        connections = palace_graph.follow_tunnels("wing_code", "auth", col=col)
        assert len(connections) == 1
        assert "drawer_preview" not in connections[0]


class TestAutoLinkSharedRooms:
    """Deterministic auto-tunnel pass after async-save drawer writes."""

    def _stub_build_graph(self, monkeypatch, nodes):
        """Replace build_graph with a stub returning fixed nodes/edges."""
        monkeypatch.setattr(palace_graph, "build_graph", lambda col=None, config=None: (nodes, []))

    def test_links_two_wings_sharing_a_room(self, tmp_path, monkeypatch):
        _use_tmp_tunnel_file(monkeypatch, tmp_path)
        self._stub_build_graph(
            monkeypatch,
            {
                "auth-migration": {
                    "wings": ["wing_alice", "wing_bob"],
                    "halls": [],
                    "count": 2,
                    "dates": [],
                },
            },
        )
        created = palace_graph.auto_link_shared_rooms(
            [("wing_alice", "auth-migration")], col=MagicMock()
        )
        assert len(created) == 1
        t = created[0]
        assert {t["source"]["wing"], t["target"]["wing"]} == {"wing_alice", "wing_bob"}
        assert t["source"]["room"] == "auth-migration"
        assert t["target"]["room"] == "auth-migration"
        assert t["label"].startswith("auto:")

    def test_skips_when_room_only_in_one_wing(self, tmp_path, monkeypatch):
        _use_tmp_tunnel_file(monkeypatch, tmp_path)
        self._stub_build_graph(
            monkeypatch,
            {
                "graphql-switch": {"wings": ["wing_alice"], "halls": [], "count": 1, "dates": []},
            },
        )
        created = palace_graph.auto_link_shared_rooms(
            [("wing_alice", "graphql-switch")], col=MagicMock()
        )
        assert created == []

    def test_skips_generic_room_in_stoplist(self, tmp_path, monkeypatch):
        _use_tmp_tunnel_file(monkeypatch, tmp_path)
        # 'decisions' is in the generic stoplist — even if it shows up
        # across many wings, no auto-tunnel.
        self._stub_build_graph(
            monkeypatch,
            {
                "decisions": {
                    "wings": ["wing_a", "wing_b", "wing_c"],
                    "halls": [],
                    "count": 5,
                    "dates": [],
                },
            },
        )
        created = palace_graph.auto_link_shared_rooms([("wing_a", "decisions")], col=MagicMock())
        assert created == []

    def test_skips_when_room_too_popular(self, tmp_path, monkeypatch):
        _use_tmp_tunnel_file(monkeypatch, tmp_path)
        # 6 wings (> _AUTO_TUNNEL_POPULARITY_CAP=5) — auto-link would
        # explode N×N edges, so skip.
        self._stub_build_graph(
            monkeypatch,
            {
                "perf-tuning": {
                    "wings": ["w1", "w2", "w3", "w4", "w5", "w6"],
                    "halls": [],
                    "count": 30,
                    "dates": [],
                },
            },
        )
        created = palace_graph.auto_link_shared_rooms([("w1", "perf-tuning")], col=MagicMock())
        assert created == []

    def test_idempotent_across_repeated_calls(self, tmp_path, monkeypatch):
        _use_tmp_tunnel_file(monkeypatch, tmp_path)
        self._stub_build_graph(
            monkeypatch,
            {
                "auth-migration": {
                    "wings": ["wing_alice", "wing_bob"],
                    "halls": [],
                    "count": 2,
                    "dates": [],
                },
            },
        )
        palace_graph.auto_link_shared_rooms([("wing_alice", "auth-migration")], col=MagicMock())
        palace_graph.auto_link_shared_rooms([("wing_alice", "auth-migration")], col=MagicMock())
        # Symmetric tunnel ID dedupes — only one record on disk.
        assert len(palace_graph.list_tunnels()) == 1

    def test_max_per_save_caps_output(self, tmp_path, monkeypatch):
        _use_tmp_tunnel_file(monkeypatch, tmp_path)
        # 5 wings sharing the room — under popularity cap (5), so auto-link.
        # max_per_save=2 should cap created tunnels.
        self._stub_build_graph(
            monkeypatch,
            {
                "billing": {
                    "wings": ["w1", "w2", "w3", "w4", "w5"],
                    "halls": [],
                    "count": 5,
                    "dates": [],
                },
            },
        )
        created = palace_graph.auto_link_shared_rooms(
            [("w1", "billing")], col=MagicMock(), max_per_save=2
        )
        assert len(created) == 2

    def test_skips_when_saved_pairs_empty(self, tmp_path, monkeypatch):
        _use_tmp_tunnel_file(monkeypatch, tmp_path)
        self._stub_build_graph(
            monkeypatch,
            {
                "anything": {"wings": ["w1", "w2"], "halls": [], "count": 2, "dates": []},
            },
        )
        assert palace_graph.auto_link_shared_rooms([], col=MagicMock()) == []
