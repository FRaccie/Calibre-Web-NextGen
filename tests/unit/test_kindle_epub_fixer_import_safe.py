# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2026 Calibre-Web contributors
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""Regression: kindle_epub_fixer must not grab a singleton lock at import.

The single-instance lock was acquired at module *import* time on a shared
tempdir path, with `sys.exit(2)` on collision and cleanup only via
`atexit`. The module is imported as a library by scripts/convert_library.py,
the ingest processor, and the test suite — so under pytest-xdist two worker
processes importing it raced on `/tmp/kindle_epub_fixer.lock`, the second
hit `FileExistsError` -> `SystemExit: 2`, and the whole Fast Tests job went
red (`test_metadata_db_write_coordination.py` collateral). A hard-killed run
also stranded the lock and blocked every subsequent fix until manual `rm`.

The guard now lives in `acquire_singleton_lock()`, called from the CLI
entrypoint (`main()`) only. Importing the module must be side-effect free.
"""

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for p in (str(REPO_ROOT / "scripts"), str(REPO_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture
def kef():
    return pytest.importorskip("kindle_epub_fixer")


def test_import_creates_no_lock_and_does_not_exit(kef):
    # Import already happened in the fixture without raising SystemExit;
    # the default lock path must not have been created by the import.
    assert not os.path.exists(kef.LOCK_PATH), (
        "importing kindle_epub_fixer created the singleton lock — the guard "
        "leaked back to import time and will collide across xdist workers"
    )
    assert hasattr(kef, "acquire_singleton_lock"), (
        "singleton guard must be an explicitly-callable entrypoint helper"
    )


def test_lock_path_matches_cwa_functions_cleanup_contract(kef):
    # cps/cwa_functions.py removes tempfile.gettempdir()+'/kindle_epub_fixer.lock';
    # the module's LOCK_PATH must stay byte-identical or cleanup silently breaks.
    import tempfile
    assert kef.LOCK_PATH == os.path.join(tempfile.gettempdir(),
                                         "kindle_epub_fixer.lock")


def test_acquire_is_a_single_instance_guard(kef, tmp_path, monkeypatch):
    lock = tmp_path / "k.lock"
    monkeypatch.setattr(kef, "LOCK_PATH", str(lock))

    kef.acquire_singleton_lock()
    assert lock.exists(), "first acquire must create the lock"

    with pytest.raises(SystemExit) as exc:
        kef.acquire_singleton_lock()
    assert exc.value.code == 2, "second instance must exit(2) (unchanged contract)"

    kef.removeLock()
    assert not lock.exists(), "removeLock must clear the lock"
    # removeLock is idempotent (FileNotFoundError swallowed).
    kef.removeLock()


def test_reimport_is_idempotent_and_lockless(kef):
    # Simulate a second xdist worker re-importing: must not raise / create lock.
    import importlib
    reloaded = importlib.reload(kef)
    assert not os.path.exists(reloaded.LOCK_PATH)
