from types import SimpleNamespace
from decimal import Decimal
import pytest
from doma_lp_bot import autonomous_cycle as auto
from doma_lp_bot.autonomous_dry_run import TEST_WALLET
from doma_lp_bot.manual_open import Blocked
from doma_lp_bot.rebalance_manual import cycle_base, scope
from doma_lp_bot.storage import load_state, save_state


def cfg(tmp_path, nft=900004):
    return SimpleNamespace(wallet=TEST_WALLET, token_id=nft, chain=97477, log=str(tmp_path/'monitor'))


def item(step, tx_hash='0xabc', **extra):
    return {'call': {'step': step}, 'hash': tx_hash, **extra}


def test_pending_survives_restart_and_confirmed_step_not_reprepared(tmp_path, monkeypatch):
    c = cfg(tmp_path); prepared = []
    monkeypatch.setattr(auto, 'prepare', lambda w, c, step: prepared.append(step) or {'step': step})
    monkeypatch.setattr(auto, '_confirm', lambda *args: None)
    first = auto.prepare_next(None, c)
    assert first['call']['step'] == 'decrease'
    assert auto.prepare_next(None, c) == first and prepared == ['decrease']
    state = load_state(cycle_base(c)); state['pending']['hash'] = '0xabc'
    save_state(cycle_base(c), state)
    assert auto.reconcile_pending(None, c)['call']['step'] == 'decrease'
    assert auto.prepare_next(None, c)['call']['step'] == 'collect'
    assert prepared == ['decrease', 'collect']


def test_failed_confirmation_does_not_clear_pending(tmp_path, monkeypatch):
    c = cfg(tmp_path)
    s = auto._empty(c); s['pending'] = {'call': {'step': 'decrease'}, 'hash': '0xabc'}
    save_state(cycle_base(c), s)
    def fail(*args): raise Blocked('receipt unavailable')
    monkeypatch.setattr(auto, '_confirm', fail)
    with pytest.raises(Blocked): auto.reconcile_pending(None, c)
    assert load_state(cycle_base(c))['pending'] == s['pending']


def test_recover_new_nft_and_reject_protected_id(tmp_path, monkeypatch):
    c = cfg(tmp_path); s = auto._empty(c)
    s['completed'] = [item('decrease'), item('collect', received_raw=[1, 1]),
                      item('mint_new', new_token_id=80001)]
    s['active_token_id'] = 80001; save_state(cycle_base(c), s)
    monkeypatch.setattr(auto, '_confirm', lambda *args: 80001)
    assert auto.resolve_active(None, c).token_id == 80001
    s['completed'][-1]['new_token_id'] = 900002; s['active_token_id'] = 900002
    save_state(cycle_base(c), s)
    monkeypatch.setattr(auto, '_confirm', lambda *args: 900002)
    with pytest.raises(Blocked): auto.resolve_active(None, c)


def test_signal_buffer_confirmation_restart_and_cycle_latch(tmp_path):
    c = cfg(tmp_path)
    c.confirm = 60; c.exit_buffer_percent = Decimal('0.1'); c.range_percent = Decimal('0.3')
    c.range_quote_offset = Decimal('10')
    c.poll = 15; c.cooldown = 3600; c.limit = 3; c.max_observation_gap = 90
    s = {'token_id': 900004, 'pool': 'pool', 'lower': 1, 'upper': 2, 'liquidity': 3,
         'block': 1, 'status': 'OUT_OF_RANGE', 'price_usdc_per_weth': Decimal('100.05'),
         'lower_price': Decimal('90'), 'upper_price': Decimal('100')}
    m = auto.SignalMonitor(c)
    assert not m.observe(s, 100)
    s['price_usdc_per_weth'] = Decimal('100.10')
    for i in range(4):
        s['block'] += 1
        assert not m.observe(s, 115 + 15*i)
    s['block'] += 1
    assert m.observe(s, 175)
    restarted = auto.SignalMonitor(c)
    assert restarted.observe(s, 176)
    assert len(restarted.state['signals']) == 1


def test_dry_loop_stops_before_broadcast(tmp_path, monkeypatch, capsys):
    c = cfg(tmp_path); c.poll = 15
    class FakeMonitor:
        def __init__(self, cfg): self.cfg = cfg; self.state = {'cycle_token': None}
        def observe(self, s, now): self.state['cycle_token'] = self.cfg.token_id; return True
    monkeypatch.setattr(auto, 'SignalMonitor', FakeMonitor)
    monkeypatch.setattr(auto, 'resolve_active', lambda w, initial: initial)
    monkeypatch.setattr(auto, '_state', lambda active: {'pending': None})
    monkeypatch.setattr(auto, 'prepare_next', lambda w, active: {'call': {'step': 'decrease'}, 'hash': None})
    monkeypatch.setattr(auto, 'cycle_gas_budget', lambda w, wallet: (100, 10, 1))
    monkeypatch.setattr(auto, 'eth_usd_spot', lambda: Decimal('2000'))
    signed = []
    auto._run_dry_loop(c, '/key', lambda *args: {'status': 'OUT_OF_RANGE', 'block': 1},
                       lambda cfg: SimpleNamespace(eth=SimpleNamespace(get_balance=lambda *args, **kwargs: 10**18)), lambda w, chain: None,
                       lambda w, c, call, key: signed.append(call), ())
    assert signed == [{'step': 'decrease'}]
    assert 'отправка отключена' in capsys.readouterr().out


def test_pending_with_wrong_stage_is_rejected(tmp_path):
    c = cfg(tmp_path); s = auto._empty(c)
    s['pending'] = {'call': {'step': 'collect'}, 'hash': '0xabc'}
    save_state(cycle_base(c), s)
    with pytest.raises(Blocked, match='не соответствует'):
        auto.reconcile_pending(None, c)


def test_broadcast_requires_separate_authorization(tmp_path, monkeypatch):
    from doma_lp_bot import autonomous_dry_run
    from web3 import Web3
    staged = []; sent = []
    raw = b'synthetic test bytes'
    monkeypatch.setattr(autonomous_dry_run, 'stage_signed_pending',
                        lambda *args: staged.append(True) or raw)
    w = SimpleNamespace(eth=SimpleNamespace(
        send_raw_transaction=lambda x: sent.append(x) or Web3.keccak(x)))
    with pytest.raises(Blocked, match='отдельного разрешения'):
        auto.broadcast_authorized(w, cfg(tmp_path), {}, '/key')
    assert not staged and not sent
    assert auto.broadcast_authorized(w, cfg(tmp_path), {}, '/key', authorized=True) == Web3.to_hex(Web3.keccak(raw))
    assert len(staged) == len(sent) == 1


def test_loop_restart_reconciles_unknown_send_before_next_step(tmp_path, monkeypatch):
    c = cfg(tmp_path); c.poll = 15
    model = {'pending': None, 'step': 0, 'active': 900004, 'cycle': 900004}
    order = ['decrease', 'collect', 'swap', 'mint_new']
    sent = []
    class FakeMonitor:
        def __init__(self, cfg):
            self.cfg = cfg; self.state = {'cycle_token': model['cycle']}
            self.gate = SimpleNamespace(reset_observation=lambda: None)
        def completed(self, w, initial):
            self.state['cycle_token'] = None; model['cycle'] = None
            return auto.with_token(initial, model['active'])
    monkeypatch.setattr(auto, 'SignalMonitor', FakeMonitor)
    monkeypatch.setattr(auto, 'cycle_gas_budget', lambda w, wallet: (100, 10, 1))
    monkeypatch.setattr(auto, 'resolve_active', lambda w, initial: auto.with_token(initial, model['active']))
    monkeypatch.setattr(auto, '_state', lambda active: {'pending': model['pending']})
    def prepare(w, active):
        model['pending'] = {'call': {'step': order[model['step']]}, 'hash': None}
        return model['pending']
    monkeypatch.setattr(auto, 'prepare_next', prepare)
    def reconcile(w, active):
        assert model['pending']['hash']
        if model['pending']['call']['step'] == 'mint_new': model['active'] = 80001
        model['pending'] = None; model['step'] += 1
    monkeypatch.setattr(auto, 'reconcile_pending', reconcile)
    def broadcast(w, active, call, key, authorized):
        sent.append(call['step']); model['pending']['hash'] = '0xabc'
        if len(sent) == 1: raise TimeoutError('unknown result')
    def stop_sleep(_): raise StopIteration
    monkeypatch.setattr(auto.time, 'sleep', stop_sleep)
    rpc = SimpleNamespace(eth=SimpleNamespace(get_transaction_receipt=lambda h: {'blockNumber': 1}, block_number=2))
    args = (c, '/key', lambda *args: (_ for _ in ()).throw(StopIteration()),
            lambda cfg: rpc, lambda w, chain: None, lambda *args: None, (TimeoutError,))
    with pytest.raises(StopIteration): auto._run_dry_loop(*args, broadcast=broadcast)
    assert sent == ['decrease'] and model['pending']['hash']
    with pytest.raises(StopIteration): auto._run_dry_loop(*args, broadcast=broadcast)
    assert sent == order and model['active'] == 80001

def test_complete_cycle_gas_budget_includes_eight_steps_and_buffer(monkeypatch):
    monkeypatch.setattr(auto.time, 'time', lambda: 1000)
    monkeypatch.setattr(auto, 'oracle', lambda w, signature, argument, block: 100 if 'L1' in signature else 10)
    w = SimpleNamespace(eth=SimpleNamespace(
        get_block=lambda tag: SimpleNamespace(timestamp=1000, number=42, baseFeePerGas=10),
        get_balance=lambda wallet, block_identifier: 10**17, gas_price=20))
    native, required, block = auto.cycle_gas_budget(w, TEST_WALLET)
    assert (native, block) == (10**17, 42)
    assert required == 10 * (800000 * 1_000_020 + 220)
