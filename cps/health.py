# -*- coding: utf-8 -*-
# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2026 Calibre-Web contributors
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""Helpers for the container ``/health`` probe (see ``cps/web.py``).

Issue #193 / PR #196. A metadata.db ``SELECT 1`` alone reports the
container green while a core supervised service is dead or the web worker
is wedged (cf. the production duplicate-scan incident: DB readable, the
process unresponsive). This module asserts the **longrun** s6 services and
reports a storage caveat.

Kept stdlib-only and free of any ``cps`` imports so the probe stays cheap
and unit-testable without booting Flask.

Scope decisions (intentional):

* All five longrun services are CRITICAL — any one down -> 503, so an
  autoheal sidecar restarts the container. Oneshot services
  (calibre-binaries-setup, cwa-init, cwa-auto-library,
  cwa-checksum-backfill, cwa-chown-library-migration,
  cwa-process-recovery) are deliberately excluded: they run once and
  exit, so they are never "running" and must not be treated as down.
* ``CWA_HEALTHCHECK_IGNORE_SERVICES`` (comma-separated) is an escape hatch:
  a service the operator has deliberately disabled (e.g. auto-zipper off
  by config) can be excluded from the 503 gate while still being reported.
* Low disk space is informational only — it never flips health to 503.
  A restart does not free space, so failing the probe would just cause an
  unproductive restart loop; the orchestrator/operator should act on the
  reported flag instead.

Unfixable from inside (documented, not handled here): if the web worker
is GIL-wedged, ``/health`` itself will not respond, so Docker's
``curl --max-time`` HEALTHCHECK catches it anyway — an in-process probe
cannot self-report that state.
"""

import os
import shutil
import subprocess

# All longrun s6 services. Mirrors root/etc/s6-overlay/s6-rc.d/*/type and
# scripts/check-cwa-services.sh so the probe, the installer, and the s6
# definitions agree. test_health_core_services.py pins this against the
# s6-rc.d type files so adding/removing a longrun upstream fails loudly.
CORE_SERVICES = (
    "svc-calibre-web-automated",
    "cwa-ingest-service",
    "metadata-change-detector",
    "cwa-auto-zipper",
    "cwa-preview-cache-cleanup",
)

# Whole-check opt-out; per-service ignore (still reported, not gated).
_DISABLE_ENV = "CWA_HEALTHCHECK_CHECK_SERVICES"
_IGNORE_ENV = "CWA_HEALTHCHECK_IGNORE_SERVICES"

# Storage flag thresholds (bytes). Informational only — never gate health.
_DISK_LOW = 512 * 1024 * 1024             # 512 MiB: warn
_DISK_CRITICALLY_LOW = 100 * 1024 * 1024  # 100 MiB: writes likely to fail


def _ignored_services():
    raw = os.environ.get(_IGNORE_ENV, "")
    return {s.strip() for s in raw.split(",") if s.strip()}


def core_services_status(services=CORE_SERVICES, *,
                         _which=shutil.which, _run=subprocess.run):
    """Best-effort liveness of the core longrun s6 services.

    Returns ``(checked, all_ok, status)``:

    * ``checked`` is ``False`` when the s6 tooling is unavailable
      (non-container dev/test) or disabled via
      ``CWA_HEALTHCHECK_CHECK_SERVICES=0``. Callers MUST NOT mark the
      container unhealthy on ``checked is False`` alone — only on a service
      actually being down — so dev/CI without s6 never regresses to 503.
    * ``all_ok`` is ``True`` when every non-ignored service is active, and
      also ``True`` when ``checked`` is ``False``.
    * ``status`` maps every service name to ``True`` (active), ``False``
      (down), or ``None`` (not determined). Ignored services are still
      reported here but excluded from the ``all_ok`` gate.
    """
    services = tuple(services)
    ignored = _ignored_services()

    if os.environ.get(_DISABLE_ENV, "1") == "0":
        return False, True, {s: None for s in services}

    if not _which("s6-rc"):
        return False, True, {s: None for s in services}

    try:
        result = _run(
            ["s6-rc", "-a", "list"],
            capture_output=True, text=True, timeout=2,
        )
    except Exception:
        # Tooling present but the call failed/timed out: "could not
        # determine" rather than "down", to avoid flapping unhealthy on a
        # transient s6 hiccup.
        return False, True, {s: None for s in services}

    # `s6-rc -a list` prints one active service per line. Exact-token
    # membership (not substring) so "<name>-foo" cannot satisfy "<name>".
    active = set(result.stdout.split())
    status = {s: (s in active) for s in services}
    all_ok = all(up for name, up in status.items() if name not in ignored)
    return True, all_ok, status


def library_storage_status(library_path, *, _statvfs=os.statvfs):
    """Informational free-space report for the library mount.

    Never gates health (see module docstring). Returns a dict; ``checked``
    is ``False`` if the path can't be stat'd (caller treats missing data as
    "unknown", not unhealthy).
    """
    try:
        st = _statvfs(library_path)
    except Exception:
        return {"checked": False}

    free = st.f_bavail * st.f_frsize
    total = st.f_blocks * st.f_frsize
    return {
        "checked": True,
        "free_bytes": free,
        "total_bytes": total,
        "low": free < _DISK_LOW,
        "critically_low": free < _DISK_CRITICALLY_LOW,
    }
