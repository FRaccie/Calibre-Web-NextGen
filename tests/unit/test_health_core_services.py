# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2026 Calibre-Web contributors
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""Tests for the issue #193 healthcheck (service-liveness + storage).

A DB ``SELECT 1`` alone reports the container green while a core longrun
service is dead or the web worker is wedged (the production duplicate-scan
incident). ``core_services_status`` asserts all longrun s6 services;
``library_storage_status`` adds an informational disk caveat. These pin the
contracts, the graceful-degrade behavior (no s6 in dev/CI must not 503),
the oneshot-exclusion drift guard, and the per-service ignore escape hatch.
"""

import importlib.util
import os
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]

# stdlib-only module, no cps imports — load by path so the test never
# boots the Flask app.
_HEALTH_PATH = REPO / "cps" / "health.py"
_spec = importlib.util.spec_from_file_location("cps_health_under_test", _HEALTH_PATH)
health = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(health)

WEB_PY = REPO / "cps" / "web.py"
S6_RCD = REPO / "root" / "etc" / "s6-overlay" / "s6-rc.d"

ALL_UP = "\n".join(health.CORE_SERVICES) + "\nsome-oneshot-done\n"


def _run_returning(stdout):
    def _run(cmd, **kwargs):
        _run.cmd = cmd
        _run.kwargs = kwargs
        return type("R", (), {"stdout": stdout, "returncode": 0})()
    return _run


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(health._DISABLE_ENV, raising=False)
    monkeypatch.delenv(health._IGNORE_ENV, raising=False)


# --------------------------------------------------------------------------
# core_services_status
# --------------------------------------------------------------------------

def test_all_longrun_services_active():
    checked, ok, status = health.core_services_status(
        _which=lambda _n: "/command/s6-rc", _run=_run_returning(ALL_UP),
    )
    assert checked is True and ok is True
    assert set(status) == set(health.CORE_SERVICES)
    assert all(status.values())


def test_any_one_longrun_down_fails_probe():
    for victim in health.CORE_SERVICES:
        up = [s for s in health.CORE_SERVICES if s != victim]
        checked, ok, status = health.core_services_status(
            _which=lambda _n: "/command/s6-rc",
            _run=_run_returning("\n".join(up)),
        )
        assert checked is True
        assert ok is False, f"{victim} down must fail the probe"
        assert status[victim] is False
        assert all(status[s] for s in up)


def test_substring_does_not_satisfy_service():
    poisoned = "\n".join(s + "-foo" for s in health.CORE_SERVICES)
    checked, ok, status = health.core_services_status(
        _which=lambda _n: "/command/s6-rc", _run=_run_returning(poisoned),
    )
    assert checked is True and ok is False
    assert not any(status.values())


def test_no_s6_tooling_degrades_gracefully():
    checked, ok, status = health.core_services_status(
        _which=lambda _n: None, _run=_run_returning("unused"),
    )
    assert checked is False and ok is True
    assert set(status.values()) == {None}


def test_env_opt_out(monkeypatch):
    monkeypatch.setenv(health._DISABLE_ENV, "0")

    def _run(*a, **k):
        raise AssertionError("must not invoke s6-rc when disabled")

    checked, ok, status = health.core_services_status(
        _which=lambda _n: "/command/s6-rc", _run=_run,
    )
    assert (checked, ok) == (False, True)
    assert set(status.values()) == {None}


def test_subprocess_failure_degrades_gracefully():
    def _boom(*a, **k):
        raise TimeoutError("s6-rc timed out")

    checked, ok, status = health.core_services_status(
        _which=lambda _n: "/command/s6-rc", _run=_boom,
    )
    assert checked is False and ok is True
    assert set(status.values()) == {None}


def test_ignored_service_is_reported_but_not_gated(monkeypatch):
    monkeypatch.setenv(health._IGNORE_ENV, "cwa-auto-zipper, cwa-preview-cache-cleanup")
    up = [s for s in health.CORE_SERVICES
          if s not in ("cwa-auto-zipper", "cwa-preview-cache-cleanup")]
    checked, ok, status = health.core_services_status(
        _which=lambda _n: "/command/s6-rc", _run=_run_returning("\n".join(up)),
    )
    assert checked is True
    assert ok is True, "ignored down services must not fail the probe"
    # still reported with their real (down) state
    assert status["cwa-auto-zipper"] is False
    assert status["cwa-preview-cache-cleanup"] is False


def test_invocation_is_bounded():
    run = _run_returning(ALL_UP)
    health.core_services_status(_which=lambda _n: "/command/s6-rc", _run=run)
    assert run.cmd == ["s6-rc", "-a", "list"]
    assert run.kwargs.get("timeout") is not None and run.kwargs["timeout"] <= 3


# --------------------------------------------------------------------------
# Drift guards vs the actual s6 definitions
# --------------------------------------------------------------------------

def _service_type(name):
    return (S6_RCD / name / "type").read_text().strip()


@pytest.mark.skipif(not S6_RCD.is_dir(), reason="s6-rc.d not present")
def test_core_services_are_all_longrun():
    for name in health.CORE_SERVICES:
        assert (S6_RCD / name).is_dir(), f"{name} missing from s6-rc.d"
        assert _service_type(name) == "longrun", (
            f"{name} is not a longrun — only longruns may be required"
        )


@pytest.mark.skipif(not S6_RCD.is_dir(), reason="s6-rc.d not present")
def test_no_oneshot_is_required():
    oneshots = {d.name for d in S6_RCD.iterdir()
                if d.is_dir() and (d / "type").is_file()
                and (d / "type").read_text().strip() == "oneshot"}
    leaked = oneshots & set(health.CORE_SERVICES)
    assert not leaked, f"oneshot services must never be required: {leaked}"


@pytest.mark.skipif(not S6_RCD.is_dir(), reason="s6-rc.d not present")
def test_every_longrun_is_covered():
    longruns = {d.name for d in S6_RCD.iterdir()
                if d.is_dir() and (d / "type").is_file()
                and (d / "type").read_text().strip() == "longrun"}
    missing = longruns - set(health.CORE_SERVICES)
    assert not missing, (
        f"longrun service(s) not asserted by the healthcheck: {missing} — "
        f"add to CORE_SERVICES or justify exclusion"
    )


def test_core_services_match_installer_script():
    script = (REPO / "scripts" / "check-cwa-services.sh").read_text()
    for name in ("cwa-ingest-service", "metadata-change-detector"):
        assert name in script, f"{name} not asserted by check-cwa-services.sh"


# --------------------------------------------------------------------------
# library_storage_status (informational only — never gates)
# --------------------------------------------------------------------------

def _statvfs(free, total, frsize=4096):
    return type("S", (), {
        "f_frsize": frsize,
        "f_bavail": free // frsize,
        "f_blocks": total // frsize,
    })()


def test_storage_reports_free_and_flags_low():
    s = health.library_storage_status(
        "/calibre-library",
        _statvfs=lambda _p: _statvfs(free=200 * 1024 * 1024, total=10 * 1024**3),
    )
    assert s["checked"] is True
    assert s["low"] is True
    assert s["critically_low"] is False
    assert s["free_bytes"] == 200 * 1024 * 1024


def test_storage_critically_low_flag():
    s = health.library_storage_status(
        "/calibre-library",
        _statvfs=lambda _p: _statvfs(free=50 * 1024 * 1024, total=10 * 1024**3),
    )
    assert s["low"] is True and s["critically_low"] is True


def test_storage_healthy_volume_no_flags():
    s = health.library_storage_status(
        "/calibre-library",
        _statvfs=lambda _p: _statvfs(free=50 * 1024**3, total=100 * 1024**3),
    )
    assert (s["low"], s["critically_low"]) == (False, False)


def test_storage_unstattable_path_is_unknown_not_unhealthy():
    def _boom(_p):
        raise FileNotFoundError("no such mount")

    s = health.library_storage_status("/nope", _statvfs=_boom)
    assert s == {"checked": False}


# --------------------------------------------------------------------------
# web.py wiring (source-pin, no Flask import)
# --------------------------------------------------------------------------

def test_web_health_route_wires_services_and_storage():
    text = WEB_PY.read_text()
    assert "from .health import core_services_status, library_storage_status" in text
    block = re.search(r'@web\.route\("/health"\).*?\), 200 if', text, re.DOTALL)
    assert block, "could not locate /health handler"
    body = block.group(0)
    assert "core_services_status()" in body
    assert "library_storage_status(cwa_get_library_location())" in body
    assert '"checks"' in body and '"services"' in body and '"storage"' in body
    # storage must NOT gate health
    assert "db_up and services_ok" in body
    assert "storage" not in body.split("healthy =")[1].split("return")[0]
