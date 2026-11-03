from types import SimpleNamespace
import json
from doma_lp_bot import monitor

def cfg(tmp):
    return SimpleNamespace(log=str(tmp/'events.jsonl'), chain=97477, wallet='0xabc', manager='0xdef', confirm=60, cooldown=30, limit=3, rpc='https://example',poll=1,max_observation_gap=90)

def test_rpc_failure_logged_and_once_returns_failure(tmp_path, monkeypatch):
    c=cfg(tmp_path)
    monkeypatch.setattr(monitor,'connect',lambda c: object())
    def fail(*a): raise TimeoutError('offline')
    monkeypatch.setattr(monitor,'snapshot',fail)
    assert monitor.run(c,once=True)==1
    e=json.loads((tmp_path/'events.jsonl').read_text())
    assert e['event']=='error' and e['error_type']=='TimeoutError'

def test_previous_signals_restore_scope(tmp_path):
    c=cfg(tmp_path)
    monitor.write_log(c.log,'signal',chain=c.chain,wallet=c.wallet,manager=c.manager,token_id=1)
    monitor.write_log(c.log,'signal',chain=c.chain,wallet='0xother',manager=c.manager,token_id=1)
    assert len(monitor.previous_signals(c.log,c)) == 1

def test_rpc_outage_position_change_and_restart_limit(tmp_path, monkeypatch):
    import time
    from decimal import Decimal as D
    from doma_lp_bot.strategy import SignalGate
    c=cfg(tmp_path); c.confirm=30; c.limit=1; c.cooldown=0; c.poll=1
    c.exit_buffer_percent=D('0.1'); c.range_percent=D('0.7')
    c.gas_units_a=None; c.gas_units_b=None; c.router_bps=None; c.slippage_bps=None; c.impact_bps=None
    monkeypatch.setattr(monitor,'connect',lambda c: object())
    monkeypatch.setattr(monitor,'show',lambda *a: None)
    monkeypatch.setattr(monitor,'show_comparison',lambda *a: None)
    monkeypatch.setattr(monitor,'simulate',lambda *a: {'status':'test'})
    def snap(token,block):
        return {'token_id':token,'pool':'p','lower':0,'upper':10,'liquidity':1,'block':block,
                'status':'OUT_OF_RANGE','price_usdc_per_weth':D(90),'lower_price':D(100),'upper_price':D(110)}
    frames=iter([snap(1,1), TimeoutError('down'),snap(1,2),snap(2,3),snap(2,4)])
    def snapshot(*a):
        x=next(frames)
        if isinstance(x,Exception): raise x
        return x
    monkeypatch.setattr(monitor,'snapshot',snapshot)
    now=[time.time()]
    monkeypatch.setattr(monitor.time,'time',lambda:now[0])
    class Stop(Exception): pass
    sleeps=[0]
    def sleep(_):
        sleeps[0]+=1; now[0]+=30
        if sleeps[0]==5: raise Stop()
    monkeypatch.setattr(monitor.time,'sleep',sleep)
    import pytest
    with pytest.raises(Stop): monitor.run(c)
    events=[json.loads(x) for x in (tmp_path/'events.jsonl').read_text().splitlines()]
    signals=[x for x in events if x['event']=='signal']
    assert [x['event'] for x in events].count('error')==1
    assert [x['event'] for x in events].count('gap')==1
    assert [x['event'] for x in events].count('reconnect')==1
    assert len(signals)==1 and signals[0]['token_id']==2
    prior=monitor.previous_signals(c.log,c)
    gate=SignalGate(0,0,1,prior)
    assert len(prior)==1 and not gate.observe(False,now[0],50,'below')

def test_malformed_signal_fails_closed(tmp_path):
    import pytest
    c=cfg(tmp_path)
    (tmp_path/'events.jsonl').write_text('{"event":"signal","chain":97477}\n')
    with pytest.raises(ValueError,match='cannot safely enforce'): monitor.previous_signals(c.log,c)

def test_future_signal_timestamp_fails_closed(tmp_path):
    import pytest
    from datetime import datetime,timezone,timedelta
    c=cfg(tmp_path)
    future=(datetime.now(timezone.utc)+timedelta(days=1)).isoformat()
    (tmp_path/'events.jsonl').write_text(json.dumps({'event':'signal','time':future,'chain':c.chain,'wallet':c.wallet,'manager':c.manager})+'\n')
    with pytest.raises(ValueError,match='cannot safely enforce'): monitor.previous_signals(c.log,c)

def test_latch_restores_across_restart_and_gap(tmp_path):
    from doma_lp_bot.strategy import SignalGate
    c=cfg(tmp_path); identity=(5,'p',0,10,100)
    snap={'chain':c.chain,'wallet':c.wallet,'manager':c.manager,'token_id':5,'pool':'p','lower':0,'upper':10,'liquidity':100,'status':'OUT_OF_RANGE','buffered_exit':'below'}
    monitor.write_log(c.log,'snapshot',**snap)
    monitor.write_log(c.log,'signal',chain=c.chain,wallet=c.wallet,manager=c.manager,token_id=5,position_identity=identity,side='below')
    monitor.write_log(c.log,'error',chain=c.chain,wallet=c.wallet,manager=c.manager,message='offline')
    latch,_=monitor.recover_episode(c.log,c)
    g=SignalGate(0,0,3,latched=latch)
    assert not g.observe(False,100,2,'below',identity,False)
    monitor.write_log(c.log,'snapshot',**{**snap,'status':'IN_RANGE','buffered_exit':None})
    assert monitor.recover_episode(c.log,c)[0] is None

def test_watch_duration_finishes_and_checkpoint_resumes(tmp_path,monkeypatch):
    import time
    from decimal import Decimal as D
    c=cfg(tmp_path); c.poll=15; c.exit_buffer_percent=D('.1'); c.range_percent=D('.7')
    c.gas_units_a=None; c.gas_units_b=None; c.router_bps=None; c.slippage_bps=None; c.impact_bps=None
    now=[time.time()]; mono=[1000.]; counter=[0]
    monkeypatch.setattr(monitor.time,'time',lambda:now[0]); monkeypatch.setattr(monitor.time,'monotonic',lambda:mono[0])
    def sleep(t): now[0]+=t; mono[0]+=t
    monkeypatch.setattr(monitor.time,'sleep',sleep)
    monkeypatch.setattr(monitor,'connect',lambda c:object()); monkeypatch.setattr(monitor,'show',lambda *a:None)
    monkeypatch.setattr(monitor,'simulate',lambda *a:{'test':True})
    def snap(*a):
        counter[0]+=1
        return {'token_id':1,'pool':'p','lower':0,'upper':10,'liquidity':1,'block':counter[0], 'status':'IN_RANGE',
                'price_usdc_per_weth':D(105),'lower_price':D(100),'upper_price':D(110)}
    monkeypatch.setattr(monitor,'snapshot',snap)
    wid,deadline=monitor.watch_deadline(c,35)
    assert monitor.watch_deadline(c,35)==(wid,deadline)
    assert monitor.run(c,duration_seconds=35)==0
    ev=[json.loads(x)['event'] for x in (tmp_path/'events.jsonl').read_text().splitlines()]
    assert ev.count('watch_start')==1 and ev.count('snapshot')==3 and ev[-1]=='watch_complete'

def test_jsonl_nested_decimal_roundtrip(tmp_path):
    from decimal import Decimal as D
    p=tmp_path/'data.jsonl'
    monitor.write_log(p,'snapshot',value=D('0.000000000123456789'),simulation={'a':D('12.34'),'unknown':None})
    row=json.loads(p.read_text())
    assert D(row['value'])==D('0.000000000123456789')
    assert row['simulation']=={'a':'12.34','unknown':None}
