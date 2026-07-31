"""API process configuration."""

from types import SimpleNamespace

from usstocks.api import __main__ as api_main


def test_main_bounds_graceful_shutdown(monkeypatch):
    settings = SimpleNamespace(
        api_host="127.0.0.1",
        api_port=8000,
        log_level="INFO",
        api_graceful_shutdown_seconds=10,
        validate_for_serving=lambda: None,
    )
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    monkeypatch.setattr(api_main, "get_settings", lambda: settings)
    monkeypatch.setattr(
        api_main.uvicorn,
        "run",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    assert api_main.main() == 0
    assert calls[0][1]["timeout_graceful_shutdown"] == 10
    assert calls[0][1]["workers"] == 1
