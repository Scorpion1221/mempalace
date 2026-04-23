"""Verify save hook does NOT auto-mine conversations.

Auto-mining raw transcripts was removed because it produced noise (68% of
drawers were tool output). Memory saving now happens via:
1. Async Haiku-powered background save (hooks_cli.py _async_save_worker)
2. Explicit AI diary_write/add_drawer MCP tool calls

This test ensures auto-mine stays removed.
"""

import os


class TestSaveHookNoAutoMine:
    """The save hook must NOT auto-mine transcripts."""

    def test_hook_does_not_mine(self):
        """The hook should not execute mempalace mine commands (comments OK)."""
        hook_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "hooks",
            "mempal_save_hook.sh",
        )
        src = open(hook_path).read()

        non_comment_lines = [
            line for line in src.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        active_code = "\n".join(non_comment_lines)

        assert "mempalace mine" not in active_code, (
            "Save hook should not auto-mine. Mining was removed because it "
            "produced noise. Use hooks_cli.py async save or explicit MCP calls."
        )
