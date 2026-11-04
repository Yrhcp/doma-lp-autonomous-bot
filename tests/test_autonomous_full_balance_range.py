import math
from decimal import Decimal
from types import SimpleNamespace
import pytest

from doma_lp_bot import autonomous_cycle as auto, rebalance_mint as mint
from doma_lp_bot.autonomous_dry_run import TEST_WALLET
from doma_lp_bot.rebalance_manual import cycle_base
from doma_lp_bot.storage import load_state, save_state
from doma_lp_bot.v3_integer import amounts_for_liquidity, liquidity_for_amounts, ticks_for_pool
from doma_lp_bot.v3_math import quote_price, raw_price_at_tick, raw_price_from_sqrt


def test_wallet_balance_snapshot_swap_and_fixed_range_survive_restart(tmp_path, monkeypatch):
    cfg = SimpleNamespace(wallet=TEST_WALLET, token_id=900004, chain=97477,
                          log=str(tmp_path / 'monitor'), range_percent=Decimal('.3'),
                          range_quote_offset=Decimal('15'))
    sqrt = math.isqrt(500_000_000 * 2**192)  # synthetic price: 2,000 USDC.e/WETH
    funds = [8_000_000, 10**15]  # includes balances held before collect
    received = [2_000_000, 5 * 10**14]
    fresh = {'sqrt': sqrt, 'balances': funds[:], 'native': 10**18}
    state = auto._empty(cfg)
    state['completed'] = [
        {'call': {'step': 'decrease'}, 'hash': 'synthetic-decrease'},
        {'call': {'step': 'collect'}, 'hash': 'synthetic-collect', 'received_raw': received},
    ]
    save_state(cycle_base(cfg), state)
    monkeypatch.setattr(auto, 'read', lambda *_: fresh)
    monkeypatch.setattr(auto, '_confirm', lambda w, cfg, state, call, h:
                        received if call['step'] == 'collect' else None)
    seen = []
    def prepare(w, cfg, snapshot, plan, budget, steps):
        seen.append((snapshot, plan.copy(), budget[:]))
        return {'step': 'approve_swap_token'}
    monkeypatch.setattr(auto.swap, 'prepare', prepare)

    first = auto.prepare_next(None, cfg)
    saved = load_state(cycle_base(cfg))
    assert saved['wallet_budget_raw'] == funds
    assert seen[0][2] == funds and seen[0][1]['budget_raw'] == funds
    assert seen[0][1]['range_percent'] == 'quote_offset:15'
    assert auto.prepare_next(None, cfg) == first  # pending and budget restored
    assert len(seen) == 1

    plan = saved['swap_plan']
    assert plan['token_in'] == mint.USDC and 0 < plan['amount_in'] < funds[0]
    raw = raw_price_from_sqrt(sqrt)
    out = int(Decimal(plan['amount_in']) * raw * Decimal('0.9999'))
    done = state['completed'] + [{'call': {'step': 'swap', 'details': {
        'tokenIn': mint.USDC, 'tokenOut': mint.WETH, 'amountIn': plan['amount_in']}},
        'swap_received_raw': out}]
    budget = mint.available(done, saved['wallet_budget_raw'])
    assert budget == [funds[0] - plan['amount_in'], funds[1] + out]
    snapshot = {'sqrt': sqrt, 'balances': budget, 'native': fresh['native']}
    _, details = mint.mint_data(cfg, snapshot, budget, after_swap=True)
    assert [details['amount0Desired'], details['amount1Desired']] == budget
    lo, hi = details['tickLower'], details['tickUpper']
    lower = quote_price(raw_price_at_tick(hi), 6, 18, False)
    upper = quote_price(raw_price_at_tick(lo), 6, 18, False)
    assert Decimal('1984.7') < lower <= Decimal('1985')
    assert Decimal('2015') <= upper < Decimal('2015.3')
    a, b = ticks_for_pool(lo, hi, 1)
    def idle_usdc(amounts):
        liq = liquidity_for_amounts(sqrt, a, b, *amounts)
        used = amounts_for_liquidity(sqrt, a, b, liq, True)
        return Decimal(amounts[0] - used[0]) / 10**6 + Decimal(amounts[1] - used[1]) * 2000 / 10**18
    assert idle_usdc(budget) < idle_usdc(funds) * Decimal('0.01')


def test_confirmed_swap_recovers_full_balance_and_pending_after_restart(tmp_path, monkeypatch):
    cfg = SimpleNamespace(wallet=TEST_WALLET, token_id=900004, chain=97477,
                          log=str(tmp_path / 'monitor'), range_percent=Decimal('.3'),
                          range_quote_offset=Decimal('15'))
    funds = [8_000_000, 10**15]
    received = [2_000_000, 5 * 10**14]
    swap_out = 10**14
    call = {'step': 'swap', 'details': {'tokenIn': mint.USDC, 'tokenOut': mint.WETH,
                                       'amountIn': 200_000}}
    state = auto._empty(cfg)
    state['wallet_budget_raw'] = funds[:]
    state['swap_plan'] = {'range_percent': 'quote_offset:15', 'budget_raw': funds[:]}
    state['completed'] = [
        {'call': {'step': 'decrease'}, 'hash': 'decrease'},
        {'call': {'step': 'collect'}, 'hash': 'collect', 'received_raw': received},
    ]
    state['pending'] = {'call': call, 'hash': 'signed-swap'}
    save_state(cycle_base(cfg), state)
    budgets = []
    monkeypatch.setattr(auto, 'reconcile', lambda w, c, call, h:
                        received if call['step'] == 'collect' else None)
    monkeypatch.setattr(auto.swap, 'reconcile',
                        lambda w, c, call, h, plan, budget: budgets.append(budget[:]) or swap_out)
    confirmed = auto.reconcile_pending(None, cfg)
    assert confirmed['swap_received_raw'] == swap_out
    assert budgets == [funds]
    after = [funds[0] - call['details']['amountIn'], funds[1] + swap_out]
    monkeypatch.setattr(auto, 'read', lambda *_: {'balances': after[:]})
    prepared = []
    def prepare(w, c, snapshot, done, starting):
        prepared.append(mint.available(done, starting))
        return {'step': 'mint_new'}
    monkeypatch.setattr(auto.mint, 'prepare', prepare)
    pending = auto.prepare_next(None, cfg)
    assert prepared == [after]
    assert auto.prepare_next(None, cfg) == pending
    assert prepared == [after]
    assert load_state(cycle_base(cfg))['wallet_budget_raw'] == funds


@pytest.mark.parametrize('step', ['swap', 'approve_mint_usdc', 'mint_new'])
def test_unsigned_legacy_cycle_is_blocked_after_upgrade(tmp_path, step):
    from doma_lp_bot.rebalance_manual import verify_before_wallet
    from doma_lp_bot.manual_open import Blocked
    cfg = SimpleNamespace(wallet=TEST_WALLET, token_id=900004, chain=97477,
                          log=str(tmp_path / 'monitor'), range_quote_offset=Decimal('15'))
    state = auto._empty(cfg)
    record = {'step': step}
    state['completed'] = [{'call': {'step': 'decrease'}},
                          {'call': {'step': 'collect'}, 'received_raw': [10, 20]}]
    state['pending'] = {'call': record, 'hash': None}
    save_state(cycle_base(cfg), state)
    with pytest.raises(Blocked, match='прежним правилам'):
        verify_before_wallet(None, cfg, record)
    assert load_state(cycle_base(cfg))['pending']['call'] == record
