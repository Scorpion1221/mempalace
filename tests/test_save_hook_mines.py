"""Verify shell hooks do not auto-mine raw conversation transcripts."""

import os


class TestSaveHookNoRawTranscriptMine:
    """Shell hooks may mine explicit MEMPAL_DIR projects, never transcript convos."""

    @staticmethod
    def _hook_src(name: str) -> str:
        path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "hooks", name)
        return open(path, encoding="utf-8").read()

    @staticmethod
    def _active_code(src: str) -> str:
        return "\n".join(
            line for line in src.splitlines() if line.strip() and not line.strip().startswith("#")
        )

    def test_save_hook_does_not_mine_transcript_convos(self):
        active_code = self._active_code(self._hook_src("mempal_save_hook.sh"))

        assert "--mode convos" not in active_code
        assert 'dirname "$TRANSCRIPT_PATH"' not in active_code
        assert 'is_valid_transcript_path "$TRANSCRIPT_PATH"' not in active_code

    def test_precompact_hook_does_not_mine_transcript_convos(self):
        active_code = self._active_code(self._hook_src("mempal_precompact_hook.sh"))

        assert "--mode convos" not in active_code
        assert 'dirname "$TRANSCRIPT_PATH"' not in active_code
        assert 'is_valid_transcript_path "$TRANSCRIPT_PATH"' not in active_code
