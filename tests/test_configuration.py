from config import ROOT, Settings
from scripts.check_configuration import report_configuration


def test_repository_env_path_is_independent_of_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert Settings.model_config['env_file'] == ROOT / '.env'
    assert Settings.model_config['env_file'].is_absolute()


def test_config_override_diagnostics_hide_secrets(tmp_path, monkeypatch, capsys):
    import scripts.check_configuration as checks
    monkeypatch.setattr(checks, 'ROOT', tmp_path)
    (tmp_path / '.env').write_text('OPENAI_API_KEY=file-test-secret\nDATABASE_URL=postgresql+asyncpg://local:password@localhost/db\n')
    monkeypatch.setenv('OPENAI_API_KEY', 'exported-test-secret')
    monkeypatch.setenv('DATABASE_URL', 'postgresql+asyncpg://effective:exported-password@localhost/db')
    settings = Settings(_env_file=tmp_path / '.env')
    assert settings.openai_api_key.get_secret_value() == 'exported-test-secret'
    report_configuration(settings)
    output = capsys.readouterr().out
    assert 'differs from .env = True' in output
    assert 'Effective database username: effective' in output
    assert 'password' not in output and 'test-secret' not in output
    assert 'exported-password' not in repr(settings)
