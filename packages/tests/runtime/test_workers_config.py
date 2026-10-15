"""AGENTRT_WORKERS resolves like the other settings: process env, then the
state-dir .env (it was read from the process env only, so a value in .env --
where operators put configuration -- was silently ignored)."""

from agentrt.runtime import config


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTRT_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("AGENTRT_WORKERS", raising=False)


def _write_dotenv(text: str) -> None:
    config.config_file().parent.mkdir(parents=True, exist_ok=True)
    config.config_file().write_text(text, encoding="utf-8")


def test_workers_default_is_one(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    assert config.workers() == 1


def test_workers_read_from_state_dotenv(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    _write_dotenv("AGENTRT_WORKERS=8\n")
    assert config.workers() == 8


def test_process_env_wins_over_dotenv(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    _write_dotenv("AGENTRT_WORKERS=8\n")
    monkeypatch.setenv("AGENTRT_WORKERS", "3")
    assert config.workers() == 3
