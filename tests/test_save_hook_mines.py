"""Hook policy: shell hooks must not auto-mine raw transcripts."""

import os


def _hook_src(name: str) -> str:
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "hooks", name)
    return open(path, encoding="utf-8").read()


class TestShellHooksDoNotAutoMineTranscripts:
    def test_save_hook_does_not_mine_transcript_dir(self):
        src = _hook_src("mempal_save_hook.sh")

        assert 'dirname "$TRANSCRIPT_PATH"' not in src
        assert "--mode convos" not in src
        assert 'mempalace mine "$MEMPAL_DIR" --mode projects' in src

    def test_precompact_hook_does_not_mine_transcript_dir(self):
        src = _hook_src("mempal_precompact_hook.sh")

        assert 'dirname "$TRANSCRIPT_PATH"' not in src
        assert "--mode convos" not in src
        assert 'mempalace mine "$MEMPAL_DIR" --mode projects' in src
