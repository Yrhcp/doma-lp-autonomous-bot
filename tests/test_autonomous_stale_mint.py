import math
import sys
from decimal import Decimal
from types import SimpleNamespace

import pytest

from doma_lp_bot import autonomous_cycle as auto, rebalance_mint as mint
from doma_lp_bot.autonomous_dry_run import TEST_WALLET
from doma_lp_bot.manual_open import Blocked, MANAGER_ADDRESS
from doma_lp_bot.rebalance_manual import cycle_base
from doma_lp_bot.storage import load_state, save_state


@pytest.fixture
def scenario(tmp_path, monkeypatch):
    clock = {'now': 1000}
    monkeypatch.setattr(auto.time, 'time', lambda: clock['now'])
    cfg = SimpleNamespace(wallet=TEST_WALLET, token_id=900004, chain=97477,
                          log=str(tmp_path / 'monitor'), range_percent=Decimal('.3'),
                          range_quote_offset=Decimal('15'))
    funds = [8_000_000, 10**15]
    collected = [2_000_000, 5 * 10**14]
    out = 10**14
    after = [funds[0]-200_000, funds[1]+out]
    sqrt = math.isqrt(500_000_000 * 2**192)
    before = {'sqrt': sqrt, 'balances': after[:], 'nonce': 42}
    data, details = mint.mint_data(cfg, before, after, after_swap=True)
    old = {'step':'mint_new', 'chainId':97477, 'from':TEST_WALLET, 'to':MANAGER_ADDRESS,
           'value':0, 'nonce':42, 'data':data, 'details':details}
    done = [
        {'call':{'step':'decrease'}, 'hash':'decrease'},
        {'call':{'step':'collect'}, 'hash':'collect', 'received_raw':collected},
        {'call':{'step':'swap', 'details':{'tokenIn':mint.USDC, 'tokenOut':mint.WETH,
                                           'amountIn':200_000}},
         'hash':'swap', 'swap_received_raw':out},
        {'call':{'step':'approve_mint_usdc'}, 'hash':'approve-usdc'},
        {'call':{'step':'approve_mint_weth'}, 'hash':'approve-weth'},
    ]
    state=auto._empty(cfg); state['completed']=done; state['wallet_budget_raw']=funds
    state['pending']={'call':old, 'hash':None}; save_state(cycle_base(cfg),state)
    fresh={'sqrt':math.isqrt(501_000_000 * 2**192), 'balances':after[:], 'nonce':42}
    monkeypatch.setattr(auto, 'read', lambda *_:fresh)
    confirmed=[]
    def confirm(w,c,state,call,h):
        confirmed.append(call['step'])
        if call['step']=='collect':return collected
        if call['step']=='swap':return out
        return None
    monkeypatch.setattr(auto, '_confirm', confirm)
    calls=[]
    def prepare(w,c,s,done,starting):
        calls.append('mint_new')
        budget=mint.available(done,starting)
        assert budget==after
        data,details=mint.mint_data(c,s,budget,after_swap=True)
        return {'step':'mint_new','chainId':97477,'from':TEST_WALLET,'to':MANAGER_ADDRESS,
                'value':0,'nonce':s['nonce'],'data':data,'details':details}
    monkeypatch.setattr(mint, 'prepare', prepare)
    nonce={'latest':42,'pending':42}
    w=SimpleNamespace(eth=SimpleNamespace(get_transaction_count=lambda wallet,tag:nonce[tag]))
    return SimpleNamespace(cfg=cfg,w=w,state=state,old=old,clock=clock,fresh=fresh,
                           nonce=nonce,calls=calls,confirmed=confirmed)


def test_expired_mint_refreshes_atomically_without_repeating_steps(scenario):
    s=scenario; s.clock['now']=1200
    replacement=auto.refresh_unsigned_mint(s.w,s.cfg)
    assert replacement['hash'] is None
    assert replacement['call']['details']['deadline']==1380
    assert replacement['call']['details']['sqrtReference']==str(s.fresh['sqrt'])
    assert replacement['call']['data']!=s.old['data']
    assert s.confirmed==['decrease','collect','swap','approve_mint_usdc','approve_mint_weth']
    assert s.calls==['mint_new']
    saved=load_state(cycle_base(s.cfg))
    assert saved['completed']==s.state['completed'] and saved['wallet_budget_raw']==s.state['wallet_budget_raw']
    assert saved['pending']==replacement
    assert auto.refresh_unsigned_mint(s.w,s.cfg)==replacement
    assert s.calls==['mint_new']


def test_changed_price_refreshes_before_deadline(scenario):
    s=scenario; s.clock['now']=1010
    result=auto.refresh_unsigned_mint(s.w,s.cfg)
    assert result['call']['details']['sqrtReference']==str(s.fresh['sqrt'])
    assert result['call']['details']['deadline']==1190


@pytest.mark.parametrize('latest,pending', [(43,43),(42,43),(41,42)])
def test_nonce_mismatch_blocks_without_mutating(scenario,latest,pending):
    s=scenario; s.clock['now']=1200; s.nonce.update(latest=latest,pending=pending)
    with pytest.raises(Blocked,match='nonce'):
        auto.refresh_unsigned_mint(s.w,s.cfg)
    assert load_state(cycle_base(s.cfg))==s.state and not s.calls


def test_saved_hash_never_replaced_even_if_nonce_unused(scenario):
    s=scenario; s.clock['now']=1200
    s.state['pending']['hash']='0x'+'ab'*32; save_state(cycle_base(s.cfg),s.state)
    with pytest.raises(Blocked,match='hash'):
        auto.refresh_unsigned_mint(s.w,s.cfg)
    assert load_state(cycle_base(s.cfg))==s.state and not s.calls


def test_receipt_or_simulation_failure_keeps_original_pending(scenario,monkeypatch):
    s=scenario; s.clock['now']=1200
    monkeypatch.setattr(auto,'_confirm',lambda *a:(_ for _ in ()).throw(Blocked('receipt unavailable')))
    with pytest.raises(Blocked,match='receipt'):
        auto.refresh_unsigned_mint(s.w,s.cfg)
    assert load_state(cycle_base(s.cfg))==s.state and not s.calls


def test_failed_mint_simulation_keeps_original_pending(scenario,monkeypatch):
    s=scenario; s.clock['now']=1200
    monkeypatch.setattr(mint,'prepare',lambda *a:(_ for _ in ()).throw(Blocked('simulation failed')))
    with pytest.raises(Blocked,match='simulation failed'):
        auto.refresh_unsigned_mint(s.w,s.cfg)
    assert load_state(cycle_base(s.cfg))==s.state


def test_new_swap_needed_but_not_previously_executed_stops(scenario,monkeypatch):
    s=scenario; s.clock['now']=1200
    done=[item for item in s.state['completed'] if item['call']['step']!='swap']
    s.state['completed']=done
    after=s.state['wallet_budget_raw']
    sqrt=math.isqrt(500_000_000 * 2**192)
    data,details=mint.mint_data(s.cfg,{'sqrt':sqrt,'balances':after,'nonce':42},after,after_swap=True)
    s.state['pending']['call'].update(data=data,details=details)
    save_state(cycle_base(s.cfg),s.state)
    monkeypatch.setattr(auto.swap,'plan_for',lambda *args:{'amount_in':1})
    with pytest.raises(Blocked,match='требует swap'):
        auto.refresh_unsigned_mint(s.w,s.cfg)
    assert load_state(cycle_base(s.cfg))==s.state and not s.calls


def test_nonce_change_during_rebuild_keeps_original_pending(scenario,monkeypatch):
    s=scenario; s.clock['now']=1200
    def moved(w,c,snapshot,done,starting):
        s.nonce['pending']=43
        return {'step':'mint_new','nonce':42}
    monkeypatch.setattr(mint,'prepare',moved)
    with pytest.raises(Blocked,match='nonce'):
        auto.refresh_unsigned_mint(s.w,s.cfg)
    assert load_state(cycle_base(s.cfg))==s.state


def test_autonomous_block_exits_with_prevent_restart_status(monkeypatch,capsys):
    from doma_lp_bot import cli
    from doma_lp_bot.config import Config
    monkeypatch.setattr(sys,'argv',['doma-lp','--autonomous-live','--authorize-real-transactions'])
    monkeypatch.setattr(Config,'load',lambda:object())
    monkeypatch.setattr(auto,'run_authorized_monitor',lambda *a,**k:(_ for _ in ()).throw(Blocked('manual review')))
    with pytest.raises(SystemExit) as result:cli.main()
    assert result.value.code==78 and 'manual review' in capsys.readouterr().out


def test_contract_revert_stops_instead_of_rpc_retry(monkeypatch):
    from web3.exceptions import ContractLogicError, Web3Exception
    class Monitor:
        def __init__(self, cfg):self.state={'cycle_token':None}
    monkeypatch.setattr(auto,'SignalMonitor',Monitor)
    checks=[]
    def chain(w, expected):
        checks.append(expected)
        raise ContractLogicError('execution reverted')
    with pytest.raises(Blocked,match='контракт отклонил'):
        auto._run_dry_loop(object(),'/unused',None,lambda _:object(),chain,None,(Web3Exception,))
    assert checks==[97477]
