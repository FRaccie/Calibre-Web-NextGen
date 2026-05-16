# -*- coding: utf-8 -*-
# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2026 Calibre-Web contributors
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""Helpers for the container ``/health`` probe (see ``cps/web.py``).

Kept stdlib-only and free of any ``cps`` imports so the probe stays cheap
and unit-testable without booting the Flask app. Implements the service
half of the issue #193 / PR #196 request: a DB ``SELECT 1`` alone reports
green while the worker/ingest is wedged, so the probe must also assert the
core supervised services are up.
"""

import os
import shutil
import subprocess

# Core s6 longrun services the container needs for ingest + metadata sync.
# Mirrors scripts/check-cwa-services.sh so the probe and the installer agree
# on what "running" means (same `s6-rc -a list` source of truth).
CORE_SERVICES = ("cwa-ingest-service", "metadata-change-detector")

# Opt-out for environments that intentionally run without these services.
_DISABLE_ENV = "CWA_HEALTHCHECK_CHECK_SERVICES"


def core_services_status(services=CORE_SERVICES, *,
                         _which=shutil.which, _run=subprocess.run):
    """Best-effort liveness of the core s6 services.

    Returns ``(checked, all_ok, status)`` where:

    * ``checked`` (bool) is ``False`` when the s6 tooling is unavailable
      (non-container dev/test) or the check is disabled via the
      ``CWA_HEALTHCHECK_CHECK_SERVICES=0`` env var. Callers MUST NOT mark
      the container unhealthy on ``checked is False`` alone — only on an
      actual service being down — so local/dev/CI runs without s6 don't
      regress to a 503.
    * ``all_ok`` (bool) is ``True`` when every requested service is active,
      and also ``True`` when ``checked`` is ``False`` (nothing to fail on).
    * ``status`` (dict) maps each service name to ``True`` (active),
      ``False`` (down), or ``None`` (not determined).
    """
    services = tuple(services)

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
        # Tooling present but the call failed/timed out: treat as
        # "could not determine" rather than "down" to avoid flapping the
        # container unhealthy on a transient s6 hiccup.
        return False, True, {s: None for s in services}

    # `s6-rc -a list` prints one active service per line. Exact-token
    # membership (not substring) so "cwa-ingest-service-foo" cannot satisfy
    # "cwa-ingest-service".
    active = set(result.stdout.split())
    status = {s: (s in active) for s in services}
    return True, all(status.values()), status
