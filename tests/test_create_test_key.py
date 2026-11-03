import importlib.util
import stat
from pathlib import Path
from unittest.mock import Mock
import pytest

spec = importlib.util.spec_from_file_location('create_test_key', Path(__file__).parents[1] / 'tools/create_test_key.py')
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


def test_creates_once_mode_600_without_printing_secret(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(setup.Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(setup, 'interactive_terminal', lambda: True)
    monkeypatch.setattr(setup.getpass, 'getpass', lambda _: 'test-secret')
    monkeypatch.setattr(setup.Account, 'from_key', lambda _: Mock(address=setup.TEST_WALLET))
    setup.main()
    target = tmp_path / '.local/share/doma-lp/test-wallet.key'
    assert target.read_text() == 'test-secret\n'
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert 'test-secret' not in capsys.readouterr().out
    with pytest.raises(SystemExit, match='уже существует'):
        setup.main()
    assert target.read_text() == 'test-secret\n'


def test_wrong_wallet_creates_no_key(tmp_path, monkeypatch):
    monkeypatch.setattr(setup.Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(setup, 'interactive_terminal', lambda: True)
    monkeypatch.setattr(setup.getpass, 'getpass', lambda _: 'test-secret')
    monkeypatch.setattr(setup.Account, 'from_key', lambda _: Mock(address='0xother'))
    with pytest.raises(SystemExit, match='не соответствует'):
        setup.main()
    assert not (tmp_path / '.local/share/doma-lp/test-wallet.key').exists()


def test_noninteractive_does_not_read_secret(tmp_path, monkeypatch):
    monkeypatch.setattr(setup.Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(setup, 'interactive_terminal', lambda: False)
    monkeypatch.setattr(setup.getpass, 'getpass', lambda _: pytest.fail('must not read'))
    with pytest.raises(SystemExit, match='интерактивный терминал'):
        setup.main()
