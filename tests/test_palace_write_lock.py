"""Tests for ``mempalace.palace.palace_write_lock``.

The lock must be enforced across PROCESSES (not just threads), because
``fcntl.flock`` is per-process. So every contention test uses
``multiprocessing`` or a real ``subprocess.Popen``, never a thread.
"""

from __future__ import annotations

import hashlib
import multiprocessing as mp
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from mempalace.palace import (
    PalaceWriteLockTimeout,
    palace_write_lock,
)

# ── Helpers ────────────────────────────────────────────────────────────


def _expected_lock_path(palace_path: str | Path) -> Path:
    """Recompute the lock-file path the same way the implementation does."""
    resolved = str(Path(palace_path).resolve())
    digest = hashlib.sha256(resolved.encode()).hexdigest()[:16]
    return Path(os.path.expanduser("~")) / ".mempalace" / "locks" / f"palace_write_{digest}.lock"


def _hold_lock_worker(palace_path: str, ready_path: str, release_path: str) -> None:
    """Subprocess target: acquire the lock, signal ready, wait for release."""
    with palace_write_lock(palace_path, timeout=10.0):
        Path(ready_path).touch()
        # Spin until the parent tells us to release. Cap at a generous
        # safety bound so a buggy test never hangs CI forever.
        deadline = time.monotonic() + 30.0
        while not Path(release_path).exists() and time.monotonic() < deadline:
            time.sleep(0.02)


def _try_acquire_worker(
    palace_path: str,
    timeout: float,
    result_path: str,
) -> None:
    """Subprocess target: try to acquire the lock and record the outcome."""
    start = time.monotonic()
    try:
        with palace_write_lock(palace_path, timeout=timeout):
            elapsed = time.monotonic() - start
            Path(result_path).write_text(f"ok {elapsed:.4f}")
    except PalaceWriteLockTimeout:
        elapsed = time.monotonic() - start
        Path(result_path).write_text(f"timeout {elapsed:.4f}")
    except Exception as exc:  # pragma: no cover - defensive
        Path(result_path).write_text(f"error {type(exc).__name__}: {exc}")


def _wait_for(path: Path, timeout: float = 5.0) -> bool:
    """Block until ``path`` exists or ``timeout`` elapses."""
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)
    return True


# multiprocessing must use ``spawn`` so the worker re-imports cleanly across
# platforms (macOS defaults to spawn on 3.8+, Linux to fork).
_MP_CTX = mp.get_context("spawn")


# ── Tests ──────────────────────────────────────────────────────────────


def test_lock_blocks_concurrent_acquirer(tmp_path):
    """Process A holds the lock; process B times out within the window."""
    palace = tmp_path / "palace_a"
    palace.mkdir()
    ready = tmp_path / "a_ready"
    release = tmp_path / "a_release"
    result = tmp_path / "b_result"

    holder = _MP_CTX.Process(
        target=_hold_lock_worker,
        args=(str(palace), str(ready), str(release)),
    )
    holder.start()
    try:
        assert _wait_for(ready), "holder failed to acquire lock"

        contender = _MP_CTX.Process(
            target=_try_acquire_worker,
            args=(str(palace), 0.5, str(result)),
        )
        contender.start()
        contender.join(timeout=5.0)
        assert not contender.is_alive(), "contender hung"

        assert result.exists(), "contender produced no result"
        text = result.read_text()
        assert text.startswith("timeout "), f"expected timeout, got: {text}"
        elapsed = float(text.split()[1])
        # Should time out near the requested 0.5s — generous upper bound
        # to absorb scheduling jitter on slow CI.
        assert 0.4 <= elapsed <= 2.0, f"unexpected timeout duration: {elapsed}s"
    finally:
        release.touch()
        holder.join(timeout=5.0)
        if holder.is_alive():  # pragma: no cover - defensive cleanup
            holder.terminate()
            holder.join(timeout=2.0)


def test_lock_released_on_normal_exit(tmp_path):
    """After the context manager exits, a fresh acquirer succeeds."""
    palace = tmp_path / "palace_b"
    palace.mkdir()

    # First holder runs to completion in this process.
    with palace_write_lock(str(palace), timeout=2.0):
        pass

    # A new subprocess should be able to acquire immediately.
    result = tmp_path / "second_result"
    proc = _MP_CTX.Process(
        target=_try_acquire_worker,
        args=(str(palace), 2.0, str(result)),
    )
    proc.start()
    proc.join(timeout=5.0)
    assert not proc.is_alive(), "second acquirer hung"

    text = result.read_text()
    assert text.startswith("ok "), f"second acquirer failed: {text}"
    elapsed = float(text.split()[1])
    assert elapsed < 1.0, f"second acquirer took unexpectedly long: {elapsed}s"


def test_lock_released_on_sigkill(tmp_path):
    """SIGKILL on the holder must release the lock via kernel cleanup."""
    if os.name == "nt":  # pragma: no cover - covered by Unix runners
        pytest.skip("SIGKILL semantics differ on Windows")

    palace = tmp_path / "palace_c"
    palace.mkdir()
    ready = tmp_path / "c_ready"

    # Use a real subprocess so we can SIGKILL it cleanly without affecting
    # the test runner. Inline source keeps the test self-contained.
    holder_src = textwrap.dedent(
        f"""
        import sys
        from pathlib import Path
        sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})
        from mempalace.palace import palace_write_lock

        with palace_write_lock({str(palace)!r}, timeout=10.0):
            Path({str(ready)!r}).touch()
            # Block forever — parent will SIGKILL us.
            import time
            while True:
                time.sleep(60)
        """
    )

    holder = subprocess.Popen([sys.executable, "-c", holder_src])
    try:
        assert _wait_for(ready, timeout=10.0), "holder subprocess failed to acquire"

        # Kill it. The kernel must drop the flock before the next acquirer
        # can succeed.
        holder.send_signal(signal.SIGKILL)
        holder.wait(timeout=5.0)
    finally:
        if holder.poll() is None:  # pragma: no cover - defensive cleanup
            holder.kill()
            holder.wait(timeout=2.0)

    # Now a fresh acquirer should succeed almost immediately.
    start = time.monotonic()
    with palace_write_lock(str(palace), timeout=2.0):
        elapsed = time.monotonic() - start

    # Kernel cleanup is essentially instant; a generous 500ms bound keeps
    # the test stable on slow CI without masking real regressions.
    assert elapsed < 0.5, f"post-SIGKILL acquisition took {elapsed}s"


def test_per_palace_independence(tmp_path):
    """Different palace paths must use independent locks."""
    palace_a = tmp_path / "palace_one"
    palace_b = tmp_path / "palace_two"
    palace_a.mkdir()
    palace_b.mkdir()

    ready = tmp_path / "ready"
    release = tmp_path / "release"
    result = tmp_path / "result"

    # Holder takes the lock on palace_a.
    holder = _MP_CTX.Process(
        target=_hold_lock_worker,
        args=(str(palace_a), str(ready), str(release)),
    )
    holder.start()
    try:
        assert _wait_for(ready), "holder failed to acquire palace_a lock"

        # An acquirer for palace_b must NOT be blocked by palace_a's holder.
        acquirer = _MP_CTX.Process(
            target=_try_acquire_worker,
            args=(str(palace_b), 1.0, str(result)),
        )
        acquirer.start()
        acquirer.join(timeout=5.0)
        assert not acquirer.is_alive(), "palace_b acquirer hung"

        text = result.read_text()
        assert text.startswith("ok "), f"palace_b acquirer should succeed, got: {text}"
        elapsed = float(text.split()[1])
        assert elapsed < 0.5, f"palace_b acquirer took too long: {elapsed}s"
    finally:
        release.touch()
        holder.join(timeout=5.0)
        if holder.is_alive():  # pragma: no cover - defensive cleanup
            holder.terminate()
            holder.join(timeout=2.0)


def test_timeout_raises_palace_write_lock_timeout(tmp_path):
    """The exact ``PalaceWriteLockTimeout`` class must be raised on timeout."""
    palace = tmp_path / "palace_d"
    palace.mkdir()

    ready = tmp_path / "d_ready"
    release = tmp_path / "d_release"

    holder = _MP_CTX.Process(
        target=_hold_lock_worker,
        args=(str(palace), str(ready), str(release)),
    )
    holder.start()
    try:
        assert _wait_for(ready), "holder failed to acquire lock"

        with pytest.raises(PalaceWriteLockTimeout):
            with palace_write_lock(str(palace), timeout=0.3):
                pass  # pragma: no cover - body never runs
    finally:
        release.touch()
        holder.join(timeout=5.0)
        if holder.is_alive():  # pragma: no cover - defensive cleanup
            holder.terminate()
            holder.join(timeout=2.0)


def test_relative_vs_absolute_path_same_lock(tmp_path, monkeypatch):
    """Relative and absolute paths to the same palace must hash to one lock."""
    palace = tmp_path / "palace_rel"
    palace.mkdir()

    # cd into tmp_path so "./palace_rel" resolves to the same directory
    # as the absolute path.
    monkeypatch.chdir(tmp_path)

    rel_path = Path("./palace_rel")
    abs_path = palace.resolve()

    expected = _expected_lock_path(abs_path)

    # Acquire via relative path — verify the expected lock file appears.
    with palace_write_lock(rel_path, timeout=2.0):
        assert expected.exists(), f"lock file {expected} not created"

    # Acquire via absolute path — same lock file is reused.
    with palace_write_lock(abs_path, timeout=2.0):
        assert expected.exists(), "lock file path differs between rel and abs"

    # Sanity: independent acquirer on relative path is blocked while
    # absolute path acquirer holds the lock.
    ready = tmp_path / "rel_ready"
    release = tmp_path / "rel_release"
    result = tmp_path / "rel_result"

    holder = _MP_CTX.Process(
        target=_hold_lock_worker,
        args=(str(abs_path), str(ready), str(release)),
    )
    holder.start()
    try:
        assert _wait_for(ready), "absolute-path holder failed to acquire"

        contender = _MP_CTX.Process(
            target=_try_acquire_worker,
            args=(str(rel_path), 0.3, str(result)),
        )
        contender.start()
        contender.join(timeout=5.0)
        assert not contender.is_alive(), "relative-path contender hung"

        text = result.read_text()
        assert text.startswith("timeout "), (
            f"relative path should be blocked by absolute holder, got: {text}"
        )
    finally:
        release.touch()
        holder.join(timeout=5.0)
        if holder.is_alive():  # pragma: no cover - defensive cleanup
            holder.terminate()
            holder.join(timeout=2.0)


def test_symlink_resolves_to_same_lock(tmp_path):
    """A symlink to a palace must share the lock with the real palace."""
    real_palace = tmp_path / "real_palace"
    real_palace.mkdir()
    symlink_path = tmp_path / "symlink_palace"

    try:
        symlink_path.symlink_to(real_palace, target_is_directory=True)
    except (OSError, NotImplementedError):  # pragma: no cover - sandboxed FS
        pytest.skip("symlink creation not supported on this filesystem")

    expected = _expected_lock_path(real_palace)

    # Holder takes the lock via the symlink path; contender via the real path
    # should see the same lock file and be blocked.
    ready = tmp_path / "sym_ready"
    release = tmp_path / "sym_release"
    result = tmp_path / "sym_result"

    holder = _MP_CTX.Process(
        target=_hold_lock_worker,
        args=(str(symlink_path), str(ready), str(release)),
    )
    holder.start()
    try:
        assert _wait_for(ready), "symlink holder failed to acquire"
        assert expected.exists(), "lock file should derive from resolved path"

        contender = _MP_CTX.Process(
            target=_try_acquire_worker,
            args=(str(real_palace), 0.3, str(result)),
        )
        contender.start()
        contender.join(timeout=5.0)
        assert not contender.is_alive(), "real-path contender hung"

        text = result.read_text()
        assert text.startswith("timeout "), (
            f"real path should be blocked by symlink holder, got: {text}"
        )
    finally:
        release.touch()
        holder.join(timeout=5.0)
        if holder.is_alive():  # pragma: no cover - defensive cleanup
            holder.terminate()
            holder.join(timeout=2.0)
