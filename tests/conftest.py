"""
Shared pytest configuration.

Currently this only contains a workaround for an upstream pytest bug that
crashes the whole run on Windows at session finish.  See below.
"""
from __future__ import annotations

import os
from pathlib import Path


# ── Windows: pytest's dead-symlink cleanup crashes the session ───────────────
#
# pytest keeps a `pytest-current` symlink in its temp root pointing at the most
# recent numbered run directory.  Two pieces of pytest's own code combine badly
# on Windows:
#
#   * `_pytest.pathlib._force_symlink` refreshes that link by calling
#     `Path.unlink()` on it.  On Windows `os.unlink` cannot remove a *directory*
#     symlink or junction — it raises `PermissionError` (WinError 5) — and
#     `_force_symlink` swallows that under `except OSError: pass`.  The link is
#     therefore never updated and keeps pointing at an older run directory.
#
#   * At session finish `cleanup_numbered_dir` deletes the older run
#     directories, which leaves that link dangling, and then calls
#     `cleanup_dead_symlinks`, which calls `Path.unlink()` on it with *no*
#     exception handling at all.
#
# The result is `PermissionError: [WinError 5] Access is denied:
# '...\\pytest-of-<user>\\pytest-current'` raised out of `pytest_sessionfinish`,
# which aborts the run with a traceback and a non-zero exit status even when
# every test passed.
#
# The fix is to remove such a link with the call Windows actually accepts for a
# directory link (`os.rmdir`, which removes the link and not its target), and to
# never let a cleanup failure take the session down.  On POSIX `Path.unlink()`
# already handles both cases, so this is a no-op there.


def _remove_link(path: Path) -> None:
    """Remove a symlink/junction, using the right call for the platform."""
    try:
        path.unlink()
    except OSError:
        # Windows directory symlink or junction: unlink is denied, rmdir works
        # and removes only the link, never the directory it points at.
        os.rmdir(path)


def _patch_pytest_dead_symlink_cleanup() -> None:
    try:
        from _pytest import pathlib as _pytest_pathlib
    except Exception:  # pragma: no cover - pytest internals moved
        return

    if not hasattr(_pytest_pathlib, "cleanup_dead_symlinks"):  # pragma: no cover
        return

    def cleanup_dead_symlinks(root: Path) -> None:
        try:
            entries = list(root.iterdir())
        except OSError:
            return
        for left_dir in entries:
            try:
                if left_dir.is_symlink() and not left_dir.resolve().exists():
                    _remove_link(left_dir)
            except OSError:
                # Best-effort housekeeping of a temp directory: a leftover link
                # is harmless, aborting the test session over one is not.
                continue

    # `cleanup_numbered_dir` resolves this name as a module global at call time,
    # so replacing the module attribute is enough.
    _pytest_pathlib.cleanup_dead_symlinks = cleanup_dead_symlinks


_patch_pytest_dead_symlink_cleanup()
