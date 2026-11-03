from decimal import Decimal as D
from types import SimpleNamespace
import json
import pytest
from doma_lp_bot import monitor
from doma_lp_bot.preflight import validate_budget,prepare_mint_unsigned,PreparationBlocked,MINT_ABI
from doma_lp_bot.pool import position_status
from doma_lp_bot.strategy import SignalGate,buffered_exit
from doma_lp_bot.v3_math import range_for_quote,raw_price_at_tick,quote_price

@pytest.mark.parametrize('amount,reserve',[('50.01','5'),('0','1'),('-1','1'),('50','0'),('50','50'),('NaN','5')])
def test_budget_rejects_invalid(amount,reserve):
    with pytest.raises(ValueError):validate_budget(amount,reserve)

def test_never_prepare_unsigned_when_blocked_or_fabricated_ready():
    for p in ({'status':'BLOCKED_READ_ONLY','blockers':['unknown cost']}, {'status':'READY_VERIFIED','blockers':[]}):
        with pytest.raises(PreparationBlocked):prepare_mint_unsigned(p)

def test_mint_abi_only_new_position():
    assert [x['name'] for x in MINT_ABI[0]['inputs'][0]['components']]==['token0','token1','fee','tickLower','tickUpper','amount0Desired','amount1Desired','amount0Min','amount1Min','recipient','deadline']
    assert all(x['name'] not in ('decreaseLiquidity','burn','collect') for x in MINT_ABI)

def test_above_below_buffers_and_boundaries():
    s={'price_usdc_per_weth':D('111'),'lower_price':D(100),'upper_price':D(110),'status':'OUT_OF_RANGE'}
    assert buffered_exit(s,D('.1'))=='above'
    s['price_usdc_per_weth']=D('99.9');assert buffered_exit(s,D('.1'))=='below'
    s['status']='IN_RANGE';assert buffered_exit(s,D('.1')) is None
    assert [position_status(t,100,200) for t in (99,100,199,200)]==['OUT_OF_RANGE','IN_RANGE','IN_RANGE','OUT_OF_RANGE']

def test_new_range_spacing_in_both_token_orientations():
    raw=D('443000000')
    for is_w0 in (True,False):
        lo,hi=range_for_quote(raw,D('.7'),10,is_w0)
        assert lo%10==hi%10==0
        prices=sorted(quote_price(raw_price_at_tick(t),6,18,is_w0) for t in (lo,hi))
        spot=quote_price(raw,6,18,is_w0)
        assert prices[0] <= spot*D('.993') and prices[1] >= spot*D('1.007')

def test_early_stop_never_fakes_completion_then_reconcile_once(tmp_path,monkeypatch):
    import time
    now=[time.time()]
    monkeypatch.setattr(monitor.time,'time',lambda:now[0])
    cfg=SimpleNamespace(log=str(tmp_path/'events.jsonl'),chain=97477,wallet='0xabc',manager='0xdef',confirm=60,cooldown=10,limit=3,max_observation_gap=90,poll=15,rpc='test')
    state=monitor.initialize_state(cfg);wid,deadline=monitor.watch_deadline(cfg,40,state)
    assert not monitor.complete_watch(cfg,state)
    now[0]=deadline+1
    assert monitor.finalize_watch(cfg)==0
    assert monitor.finalize_watch(cfg)==0
    events=[json.loads(line) for line in (tmp_path/'events.jsonl').read_text().splitlines()]
    assert sum(x['event']=='watch_complete' for x in events)==1
    assert monitor.initialize_state(cfg)['watch'] is None

def test_completion_recovers_after_log_write_before_state_clear(tmp_path,monkeypatch):
    import time
    now=[time.time()];monkeypatch.setattr(monitor.time,'time',lambda:now[0])
    cfg=SimpleNamespace(log=str(tmp_path/'events.jsonl'),chain=97477,wallet='0xabc',manager='0xdef')
    state=monitor.initialize_state(cfg);wid,deadline=monitor.watch_deadline(cfg,40,state);now[0]=deadline+1
    state['watch']['completion_pending']=True;monitor.save_state(cfg.log,state)
    monitor.write_log(cfg.log,'watch_complete',chain=cfg.chain,wallet=cfg.wallet,manager=cfg.manager,watch_id=wid)
    assert monitor.finalize_watch(cfg)==0
    assert sum(json.loads(x)['event']=='watch_complete' for x in (tmp_path/'events.jsonl').read_text().splitlines())==1

def test_confirmation_resets_on_gap_side_switch_and_restart():
    g=SignalGate(60,0,3,max_gap=70);ident=(1,'p',0,10,1)
    assert not g.observe(False,0,1,'above',ident,False)
    assert not g.observe(False,20,2,'below',ident,False)
    assert not g.observe(False,100,3,'below',ident,False)
    assert g.observe(False,160,4,'below',ident,False);g.record(160)
    restarted=SignalGate(60,0,3,prior=g.signals,latched=g.latched)
    assert not restarted.observe(False,200,5,'below',ident,False)
    assert not restarted.observe(False,260,6,'below',ident,False)
    assert not restarted.observe(True,270,7,None,ident,True)
    assert not restarted.observe(False,280,8,'above',ident,False)
    assert restarted.observe(False,340,9,'above',ident,False)

def test_live_preflight_with_mock_is_blocked_and_never_uses_transactions(monkeypatch):
    import time
    from doma_lp_bot import preflight as p
    class Call:
        def __init__(self,value):self.value=value
        def call(self,block_identifier=None):
            assert block_identifier==123
            return self.value
    class Functions:
        def __init__(self,address):self.address=address
        def factory(self):return Call(p.FACTORY_ADDRESS)
        def getPool(self,*args):return Call('0x0000000000000000000000000000000000001234')
        def token0(self):return Call(p.USDC)
        def token1(self):return Call(p.WETH)
        def fee(self):return Call(100)
        def tickSpacing(self):return Call(1)
        def slot0(self):return Call((2**96,0,0,1,1,0,True))
        def decimals(self):return Call(6 if self.address==p.USDC else 18)
        def balanceOf(self,*args):return Call(10**9)
        def allowance(self,*args):return Call(3)
        def quoteExactInputSingle(self,args):
            assert args==(p.USDC,p.WETH,1000000,100,0)
            return Call((12,2**96,0,1000))
    class Block:
        number=123;timestamp=time.time();hash=b'blockhash'
    class Eth:
        chain_id=97477;gas_price=1
        def contract(self,address,abi):return SimpleNamespace(functions=Functions(address))
        def get_block(self,_):return Block()
        def get_code(self,*args,**kwargs):return b'code'
        def get_balance(self,*args,**kwargs):return 123
    monkeypatch.setattr(p,'connect',lambda cfg:SimpleNamespace(eth=Eth()))
    cfg=SimpleNamespace(chain=97477,manager=p.MANAGER_ADDRESS,usdc=p.USDC,weth=p.WETH,wallet=p.WETH,max_age=120)
    plan=p.live_preflight(cfg)
    assert plan['status']=='BLOCKED_READ_ONLY' and plan['transaction_data'] is None
    assert plan['unsigned_transaction_template']['data'] is None
    assert plan['pool_quote_probe']['amount_out_raw']==12
    assert plan['position'].startswith('NEW_POSITION_ONLY')
    assert len(plan['allowances_raw_by_spender'])==2

def test_wrong_network_or_contract_fails_before_rpc(monkeypatch):
    from doma_lp_bot import preflight as p
    monkeypatch.setattr(p,'connect',lambda cfg:pytest.fail('network must not be reached'))
    c=SimpleNamespace(chain=1,manager=p.MANAGER_ADDRESS,usdc=p.USDC,weth=p.WETH)
    with pytest.raises(ValueError,match='Doma'):p.live_preflight(c)

def test_unfinished_final_record_does_not_block_completion(tmp_path,monkeypatch):
    import time
    now=[time.time()];monkeypatch.setattr(monitor.time,'time',lambda:now[0])
    c=SimpleNamespace(log=str(tmp_path/'events.jsonl'),chain=97477,wallet='0xabc',manager='0xdef')
    state=monitor.initialize_state(c);wid,deadline=monitor.watch_deadline(c,40,state)
    with open(c.log,'ab') as f:f.write(b'{"event":"partial')
    now[0]=deadline+1
    assert monitor.finalize_watch(c)==0
    rows=[json.loads(x) for x in open(c.log)]
    assert sum(x['event']=='watch_complete' for x in rows)==1
    assert any(x['event']=='log_recovery' for x in rows)

def test_stale_pool_snapshot_rejected_before_position_reads():
    from doma_lp_bot.pool import snapshot
    class Eth:
        chain_id=97477
        def get_block(self,_): return {'number':1,'timestamp':0}
    c=SimpleNamespace(chain=97477,max_age=120,snapshot_timeout=10)
    with pytest.raises(ValueError,match='stale'): snapshot(SimpleNamespace(eth=Eth()),c)

def test_sigterm_at_elapsed_deadline_marks_completion(tmp_path,monkeypatch):
    import time
    now=[time.time()];mono=[100.0]
    monkeypatch.setattr(monitor.time,'time',lambda:now[0]);monkeypatch.setattr(monitor.time,'monotonic',lambda:mono[0])
    c=SimpleNamespace(log=str(tmp_path/'events.jsonl'),chain=97477,wallet='0xabc',manager='0xdef',confirm=60,cooldown=10,limit=3,max_observation_gap=90,poll=15,rpc='test')
    monkeypatch.setattr(monitor,'connect',lambda c:object())
    def interrupt(*args):
        now[0]+=40;mono[0]+=40
        raise monitor.StopRequested()
    monkeypatch.setattr(monitor,'snapshot',interrupt)
    assert monitor.run(c,duration_seconds=40)==0
    rows=[json.loads(x) for x in (tmp_path/'events.jsonl').read_text().splitlines()]
    assert sum(x['event']=='watch_complete' for x in rows)==1
    assert monitor.initialize_state(c)['watch'] is None

def test_sigterm_before_deadline_preserves_watch(tmp_path,monkeypatch):
    import time
    now=[time.time()];monkeypatch.setattr(monitor.time,'time',lambda:now[0])
    c=SimpleNamespace(log=str(tmp_path/'events.jsonl'),chain=97477,wallet='0xabc',manager='0xdef',confirm=60,cooldown=10,limit=3,max_observation_gap=90,poll=15,rpc='test')
    monkeypatch.setattr(monitor,'connect',lambda c:object())
    def interrupt(*args):raise monitor.StopRequested()
    monkeypatch.setattr(monitor,'snapshot',interrupt)
    assert monitor.run(c,duration_seconds=40)==0
    rows=[json.loads(x) for x in (tmp_path/'events.jsonl').read_text().splitlines()]
    assert rows[-1]['event']=='watch_paused' and monitor.initialize_state(c)['watch'] is not None

def test_blocked_cli_returns_nonzero(monkeypatch,capsys):
    import sys
    from doma_lp_bot import cli,preflight
    monkeypatch.setattr(sys,'argv',['doma-lp','--prepare50'])
    monkeypatch.setattr(cli.Config,'load',lambda:object())
    monkeypatch.setattr(preflight,'live_preflight',lambda cfg:{'status':'BLOCKED_READ_ONLY','blockers':['unknown']})
    with pytest.raises(SystemExit) as result:cli.main()
    assert result.value.code==2 and 'BLOCKED_READ_ONLY' in capsys.readouterr().out

def test_spot_swap_estimate_blocks_when_both_assets_short():
    from doma_lp_bot.preflight import illustrative_swap_requirement,USDC,WETH
    assert illustrative_swap_requirement((10,10),(100,100),D(2000))[0] is None
    req,_=illustrative_swap_requirement((100_000_000,0),(50_000_000,10**16),D(2000))
    assert req==(USDC,WETH,20_000_000)
    req,_=illustrative_swap_requirement((0,10**17),(20_000_000,10**16),D(2000))
    assert req==(WETH,USDC,10**16)
