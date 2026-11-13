from pathlib import Path

from trainer.config import load_paths, load_settings


def test_paths_from_trainer_home(monkeypatch, tmp_path):
    monkeypatch.setenv("TRAINER_HOME", str(tmp_path))
    p = load_paths()
    assert p.db == tmp_path / "data" / "trainer.db"
    assert p.token_file == tmp_path / ".secrets" / "garmin.token"
    assert p.plans == tmp_path / "plans"


def test_request_interval_env_override(monkeypatch):
    monkeypatch.setenv("TRAINER_REQUEST_INTERVAL", "2.5")
    assert load_settings().request_interval_s == 2.5


def test_gitignore_covers_secrets_and_data():
    lines = (Path(__file__).parents[1] / ".gitignore").read_text().splitlines()
    for entry in ("data/", ".secrets/", ".venv/", "*.token"):
        assert entry in lines


def test_load_paths_uses_home_pointer(monkeypatch, tmp_path):
    import trainer.config as config

    pointer = tmp_path / ".trainer-home"
    pointer.write_text(f"{tmp_path / 'elsewhere'}\n")
    monkeypatch.delenv("TRAINER_HOME", raising=False)
    monkeypatch.setattr(config, "HOME_POINTER", pointer)
    assert config.load_paths().root == tmp_path / "elsewhere"
