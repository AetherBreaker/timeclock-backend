"""Tests for the page's entry point and start-up."""

# Standard library imports
import importlib
import json
import types
from importlib.metadata import entry_points
from typing import TYPE_CHECKING, Any

# Third party imports
import pytest
import uvicorn
from page_sim import page_settings

if TYPE_CHECKING:
  # Standard library imports
  from pathlib import Path

_COMPLETE = {
  "PAGE_PUBLIC_URL": "https://page.example",
  "PAGE_SESSION_SECRET": "s" * 64,
  "TIMECLOCK_WEB_PASSWORD": "p" * 48,
  "ZOHO_CLIENT_ID": "page",
  "ZOHO_CLIENT_SECRET": "zoho-secret",
}
_WITHOUT_ZOHO = {name: value for name, value in _COMPLETE.items() if not name.startswith("ZOHO_")}
_TEST_ACCOUNTS = json.dumps({"ann@example.com": "p" * 16})


def _serve(monkeypatch: pytest.MonkeyPatch, **values: str) -> dict[str, Any]:
  """Run `serve()` with these settings, capturing what it hands NiceGUI instead of starting it."""
  module = importlib.import_module("timeclock_backend.web.app")  # the live one: NiceGUI's test reset drops and re-imports it
  captured: dict[str, Any] = {"run": None, "build": None}
  monkeypatch.setattr(module, "PageSettings", types.SimpleNamespace(get_settings=lambda: page_settings(**values)))
  monkeypatch.setattr(module, "build", lambda *args, **kwargs: captured.update(build=(args, kwargs)))
  monkeypatch.setattr(module, "register_for_shutdown", lambda *_a, **_k: None)
  monkeypatch.setattr(module.app, "on_startup", lambda _handler: None)
  monkeypatch.setattr(module.app, "on_shutdown", lambda _handler: None)
  monkeypatch.setattr(module.ui, "run", lambda **kwargs: captured.update(run=kwargs))  # noqa: PLW0108 - keyword arguments only, as ui.run gets them
  monkeypatch.setattr(uvicorn.Server, "capture_signals", uvicorn.Server.capture_signals)  # restored after serve() patches it
  module.serve()
  return captured


def test_the_page_has_its_own_console_script() -> None:
  scripts = {ep.name: ep.value for ep in entry_points(group="console_scripts") if ep.dist and ep.dist.name == "timeclock-backend"}
  assert scripts["timeclock-web"] == "timeclock_backend.web.__main__:run_page"
  assert [name for name in scripts if name.startswith("run-app-")] == ["run-app-timeclock-backend"]  # devkit-container runs only it


@pytest.mark.parametrize("missing", sorted(_COMPLETE))
def test_serve_refuses_missing_settings(monkeypatch: pytest.MonkeyPatch, missing: str) -> None:
  with pytest.raises(ValueError, match=missing):
    _serve(monkeypatch, **{name: value for name, value in _COMPLETE.items() if name != missing})


def test_serve_runs_nicegui_with_secure_session_cookies(monkeypatch: pytest.MonkeyPatch) -> None:
  run = _serve(monkeypatch, **_COMPLETE)["run"]
  assert run is not None
  assert {key: run[key] for key in ("host", "port", "reload", "show", "storage_secret", "log_config")} == {
    "host": "0.0.0.0",
    "port": 8080,
    "reload": False,
    "show": False,
    "storage_secret": "s" * 64,
    "log_config": None,
  }
  assert run["session_middleware_kwargs"] == {
    "session_cookie": "__Host-timeclock-web",
    "https_only": True,
    "same_site": "lax",
    "max_age": 400 * 24 * 3600,  # the browser's storage id; the gate ends the sign-in
  }


def test_serve_creates_the_heartbeat_folder(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
  _serve(monkeypatch, **_COMPLETE, PERSISTED_DIR_LOC=str(tmp_path))  # a fresh volume: socket logging creates no logs folder
  assert (tmp_path / "logs").is_dir()  # the healthcheck reads logs/heartbeat.txt


def test_serve_runs_with_only_the_test_sign_in(monkeypatch: pytest.MonkeyPatch) -> None:
  assert _serve(monkeypatch, **_WITHOUT_ZOHO, PAGE_TEST_ACCOUNTS=_TEST_ACCOUNTS)["run"] is not None


def test_serve_refuses_without_a_sign_in_method(monkeypatch: pytest.MonkeyPatch) -> None:
  with pytest.raises(ValueError, match="PAGE_TEST_ACCOUNTS"):
    _serve(monkeypatch, **_WITHOUT_ZOHO)


def test_serve_refuses_half_of_zoho_beside_the_test_sign_in(monkeypatch: pytest.MonkeyPatch) -> None:
  values = {name: value for name, value in _COMPLETE.items() if name != "ZOHO_CLIENT_SECRET"}
  with pytest.raises(ValueError, match="ZOHO_CLIENT_SECRET"):
    _serve(monkeypatch, **values, PAGE_TEST_ACCOUNTS=_TEST_ACCOUNTS)


def test_serve_refuses_bad_test_accounts(monkeypatch: pytest.MonkeyPatch) -> None:
  with pytest.raises(ValueError, match="PAGE_TEST_ACCOUNTS"):
    _serve(monkeypatch, **_COMPLETE, PAGE_TEST_ACCOUNTS='{"ann@example.com": "short"}')


def _alert_text(exc: BaseException) -> str:
  """What aeth_ext's crash alert shows (`show_locals=True`): the message and each of the page's own frames' locals."""
  parts, tb = [str(exc)], exc.__traceback__
  while tb is not None:
    if "timeclock_backend" in tb.tb_frame.f_code.co_filename:
      parts.append(repr(tb.tb_frame.f_locals))
    tb = tb.tb_next
  return "\n".join(parts)


_SECRETS = ("s" * 64, "p" * 48, "zoho-secret", "test-password-0123", "test-pw-short")


@pytest.mark.parametrize(
  "values",
  [
    {
      **{k: v for k, v in _COMPLETE.items() if k != "PAGE_PUBLIC_URL"},
      "PAGE_TEST_ACCOUNTS": json.dumps({"a@b.co": "test-password-0123"}),
    },
    {**_COMPLETE, "PAGE_TEST_ACCOUNTS": json.dumps({"ann@example.com": "test-pw-short"})},
    {**_COMPLETE, "PAGE_TEST_ACCOUNTS": json.dumps({"test-password-0123": "ann@example.com"})},
    {**_COMPLETE, "PAGE_TEST_ACCOUNTS": '["ann@example.com", "test-password-0123"]'},
    {**_COMPLETE, "PAGE_TEST_ACCOUNTS": '{"ann@example.com": "test-password-0123"'},
  ],
  ids=["no-public-url", "short-password", "swapped", "a-list", "not-json"],
)
def test_a_refused_start_keeps_every_secret_out_of_the_crash_alert(monkeypatch: pytest.MonkeyPatch, values: dict[str, str]) -> None:
  with pytest.raises(ValueError) as raised:
    _serve(monkeypatch, **values)
  alert = _alert_text(raised.value)
  assert [secret for secret in _SECRETS if secret in alert] == []
