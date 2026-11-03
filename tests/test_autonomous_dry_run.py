import os
from types import SimpleNamespace
import pytest
from eth_account import Account
from doma_lp_bot import autonomous_dry_run as dry
from doma_lp_bot.manual_open import Blocked
from doma_lp_bot.storage import load_state, save_state


def record(wallet):
    return {'chainId': 97477, 'from': wallet, 'to': '0x' + '12' * 20, 'value': 0,
            'nonce': 0, 'data': '0x', 'gasLimit': 21000,
            'maxFeePerGas': 2_000_000, 'maxPriorityFeePerGas': 1_000_000}


def test_protected_wallet_and_positions():
    for token in (None, 900001, 900002):
        with pytest.raises(Blocked):
            dry.validate_selection(SimpleNamespace(chain=97477, wallet=dry.TEST_WALLET, token_id=token))
    with pytest.raises(Blocked):
        dry.validate_selection(SimpleNamespace(chain=97477, wallet='0x2222222222222222222222222222222222222222', token_id=2))


def test_signing_rehearsal_requires_durable_call_and_verification(tmp_path, monkeypatch):
    a = Account.create(); monkeypatch.setattr(dry, 'TEST_WALLET', a.address)
    cfg = SimpleNamespace(chain=97477, wallet=a.address, token_id=123, log=str(tmp_path/'log'))
    tx = record(a.address)
    key = tmp_path/'key'; key.write_text(a.key.hex()); os.chmod(key, 0o600)
    state = {'scope': dry.scope(cfg), 'pending': {'call': tx, 'hash': None}}
    monkeypatch.setattr(dry, 'load_state', lambda _: state)
    checked = []
    monkeypatch.setattr(dry, 'verify_before_wallet', lambda w, c, r: checked.append(r))
    digest = dry.sign_rehearsal(None, cfg, tx, str(key))
    assert len(digest) == 64 and checked == [tx]
    state['pending']['hash'] = '0x' + 'ab'*32
    with pytest.raises(Blocked): dry.sign_rehearsal(None, cfg, tx, str(key))
    assert len(checked) == 1


def test_secret_permissions_and_wrong_key_fail_closed(tmp_path, monkeypatch):
    a = Account.create(); other = Account.create()
    monkeypatch.setattr(dry, 'TEST_WALLET', a.address)
    cfg = SimpleNamespace(chain=97477, wallet=a.address, token_id=123, log=str(tmp_path/'log'))
    tx = record(a.address); state = {'scope': dry.scope(cfg), 'pending': {'call': tx, 'hash': None}}
    monkeypatch.setattr(dry, 'load_state', lambda _: state)
    key = tmp_path/'key'; key.write_text(other.key.hex()); os.chmod(key, 0o644)
    with pytest.raises(Blocked, match='0600'): dry.sign_rehearsal(None, cfg, tx, str(key))
    os.chmod(key, 0o600)
    with pytest.raises(Blocked, match='не соответствует'): dry.sign_rehearsal(None, cfg, tx, str(key))


def test_staged_hash_survives_restart_and_prevents_repeat(tmp_path, monkeypatch):
    a = Account.create(); monkeypatch.setattr(dry, 'TEST_WALLET', a.address)
    cfg = SimpleNamespace(chain=97477, wallet=a.address, token_id=123, log=str(tmp_path/'log'))
    tx = record(a.address)
    key = tmp_path/'key'; key.write_text(a.key.hex()); os.chmod(key, 0o600)
    state = {'version': 1, 'scope': dry.scope(cfg), 'signals': [], 'latch': None,
             'watch': None, 'latest': None, 'pending': {'call': tx, 'hash': None}, 'completed': []}
    save_state(dry.cycle_base(cfg), state)
    checked = []
    monkeypatch.setattr(dry, 'verify_before_wallet', lambda *args: checked.append(True))
    raw = dry.stage_signed_pending(None, cfg, tx, str(key))
    restored = load_state(dry.cycle_base(cfg))
    assert restored['pending']['hash'] == dry.Web3.to_hex(dry.Web3.keccak(raw))
    assert raw.hex() not in str(restored) and len(checked) == 1
    with pytest.raises(Blocked, match='pending'):
        dry.stage_signed_pending(None, cfg, tx, str(key))
    assert len(checked) == 1


def test_unknown_send_result_never_resends(tmp_path, monkeypatch):
    from doma_lp_bot.autonomous_cycle import broadcast_authorized
    a = Account.create(); monkeypatch.setattr(dry, 'TEST_WALLET', a.address)
    cfg = SimpleNamespace(chain=97477, wallet=a.address, token_id=123, log=str(tmp_path/'log'))
    tx = record(a.address)
    key = tmp_path/'key'; key.write_text(a.key.hex()); os.chmod(key, 0o600)
    s = {'version': 1, 'scope': dry.scope(cfg), 'signals': [], 'latch': None,
         'watch': None, 'latest': None, 'pending': {'call': tx, 'hash': None}, 'completed': []}
    save_state(dry.cycle_base(cfg), s)
    monkeypatch.setattr(dry, 'verify_before_wallet', lambda *args: None)
    sent = []
    def timeout(raw):
        sent.append(True)
        raise TimeoutError('unknown test result')
    w = SimpleNamespace(eth=SimpleNamespace(send_raw_transaction=timeout))
    with pytest.raises(TimeoutError):
        broadcast_authorized(w, cfg, tx, str(key), authorized=True)
    assert load_state(dry.cycle_base(cfg))['pending']['hash']
    with pytest.raises(Blocked):
        broadcast_authorized(w, cfg, tx, str(key), authorized=True)
    assert len(sent) == 1
