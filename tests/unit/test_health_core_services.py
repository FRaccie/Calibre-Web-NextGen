# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2026 Calibre-Web contributors
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""Tests for the issue #193 healthcheck service-liveness half.

A DB ``SELECT 1`` alone reports the container green while ingest /
metadata-change-detector are dead or the web worker is wedged (the
production duplicate-scan incident). ``core_services_status`` adds the
s6 service assertion; these pin its contract and the graceful-degrade
behavior that keeps non-container dev/CI from regressing to 503.
"""

import importlib.util
import pathlib
import re

import pytest

# stdlib-only module, no cps imports — load by path so the test never
# boots the Flask app.
_HEALTH_PATH = pathlib.Path(__file__).resolve().parents[2] / "cps" / "health.py"
_spec = importlib.util.spec_from_file_location("cps_health_under_test", _HEALTH_PATH)
health = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(health)

WEB_PY = pathlib.Path(__file__).resolve().parents[2] / "cps" / "web.py"


def _run_returning(stdout):
    def _run(cmd, **kwargs):
        _run.cmd = cmd
        _run.kwargs = kwargs
        return type("R", (), {"stdout": stdout, "returncode": 0})()
    return _run


def test_all_services_active(monkeypatch):
    monkeypatch.delenv(health._DISABLE_ENV, raising=False)
    checked, ok, status = health.core_services_status(
        _which=lambda _n: "/command/s6-rc",
        _run=_run_returning("cwa-ingest-service\nmetadata-change-detector\nother\n"),
    )
    assert checked is True
    assert ok is True
    assert status == {"cwa-ingest-service": True, "metadata-change-detector": True}


def test_one_service_down_fails_probe(monkeypatch):
    monkeypatch.delenv(health._DISABLE_ENV, raising=False)
    checked, ok, status = health.core_services_status(
        _which=lambda _n: "/command/s6-rc",
        _run=_run_returning("cwa-ingest-service\n"),
    )
    assert checked is True
    assert ok is False
    assert status == {"cwa-ingest-service": True, "metadata-change-detector": False}


def test_substring_does_not_satisfy_service(monkeypatch):
    # A service named "<name>-foo" must NOT count as "<name>" up.
    monkeypatch.delenv(health._DISABLE_ENV, raising=False)
    checked, ok, status = health.core_services_status(
        _which=lambda _n: "/command/s6-rc",
        _run=_run_returning("cwa-ingest-service-foo\nmetadata-change-detector\n"),
    )
    assert checked is True
    assert ok is False
    assert status["cwa-ingest-service"] is False
    assert status["metadata-change-detector"] is True


def test_no_s6_tooling_degrades_gracefully(monkeypatch):
    # Dev/CI without s6: must NOT fail the probe on that basis alone.
    monkeypatch.delenv(health._DISABLE_ENV, raising=False)
    checked, ok, status = health.core_services_status(
        _which=lambda _n: None,
        _run=_run_returning("should not be called"),
    )
    assert checked is False
    assert ok is True
    assert status == {"cwa-ingest-service": None, "metadata-change-detector": None}


def test_env_opt_out(monkeypatch):
    monkeypatch.setenv(health._DISABLE_ENV, "0")
    called = {"run": False}

    def _run(*a, **k):
        called["run"] = True
        raise AssertionError("must not invoke s6-rc when disabled")

    checked, ok, status = health.core_services_status(
        _which=lambda _n: "/command/s6-rc", _run=_run,
    )
    assert called["run"] is False
    assert (checked, ok) == (False, True)
    assert status == {"cwa-ingest-service": None, "metadata-change-detector": None}


def test_subprocess_failure_degrades_gracefully(monkeypatch):
    monkeypatch.delenv(health._DISABLE_ENV, raising=False)

    def _boom(*a, **k):
        raise TimeoutError("s6-rc timed out")

    checked, ok, status = health.core_services_status(
        _which=lambda _n: "/command/s6-rc", _run=_boom,
    )
    assert checked is False
    assert ok is True
    assert set(status.values()) == {None}


def test_invocation_shape_is_bounded(monkeypatch):
    monkeypatch.delenv(health._DISABLE_ENV, raising=False)
    run = _run_returning("cwa-ingest-service\nmetadata-change-detector\n")
    health.core_services_status(_which=lambda _n: "/command/s6-rc", _run=run)
    assert run.cmd == ["s6-rc", "-a", "list"]
    # Must be time-bounded so the 3s Docker HEALTHCHECK timeout can't hang.
    assert run.kwargs.get("timeout") is not None
    assert run.kwargs["timeout"] <= 3


def test_core_services_match_installer_script():
    # The probe and scripts/check-cwa-services.sh must agree on the names.
    script = (pathlib.Path(__file__).resolve().parents[2]
              / "scripts" / "check-cwa-services.sh").read_text()
    for name in health.CORE_SERVICES:
        assert name in script, f"{name} not asserted by check-cwa-services.sh"


def test_web_health_route_wires_service_check():
    # Source-pin (no Flask import): /health must consume core_services_status
    # and expose the new checks block, not just the DB result.
    text = WEB_PY.read_text()
    assert "from .health import core_services_status" in text
    block = re.search(r'@web\.route\("/health"\).*?\), 200 if', text, re.DOTALL)
    assert block, "could not locate /health handler"
    body = block.group(0)
    assert "core_services_status()" in body
    assert '"checks"' in body and '"services"' in body
    assert "db_up and services_ok" in body
