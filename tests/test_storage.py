import json
import pytest
from types import SimpleNamespace
from doma_lp_bot import storage,monitor
from doma_lp_bot.report import analyze
from doma_lp_bot.strategy import SignalGate

def config(p):
    return SimpleNamespace(log=str(p),chain=97477,wallet='0xabc',manager='0xdef',max_observation_gap=90)

def test_rotation_limits_and_report_retention(tmp_path,monkeypatch):
    monkeypatch.setattr(storage,'MAX_BYTES',250)
    p=tmp_path/'monitor.jsonl'
    for i in range(15): monitor.write_log(p,'error',chain=97477,wallet='0xabc',manager='0xdef',error_category='rpc',index=i)
    files=storage.log_files(p)
    assert len(files)==5 and all(x.stat().st_size<=250 for x in files)
    rows=[json.loads(x) for file in files for x in file.read_text().splitlines()]
    assert [r['index'] for r in rows] == sorted(r['index'] for r in rows)
    report=analyze(p,config(p))
    assert report['rpc_errors']==len(rows) and any('удалена' in w for w in report['warnings'])

def test_incomplete_or_invalid_last_record_recovered(tmp_path):
    p=tmp_path/'monitor.jsonl';monitor.write_log(p,'first',n=1)
    with p.open('ab') as f: f.write(b'{"event":"trunc')
    monitor.write_log(p,'second',n=2)
    rows=[json.loads(x) for x in p.read_text().splitlines()]
    assert [r['event'] for r in rows]==['first','log_recovery','second']
    with p.open('ab') as f: f.write(b'{invalid}\n')
    monitor.write_log(p,'third')
    assert [json.loads(x)['event'] for x in p.read_text().splitlines()][-2:]==['log_recovery','third']

def test_state_atomic_restore_latch_and_cooldown(tmp_path):
    import time
    p=tmp_path/'monitor.jsonl';c=config(p);state=monitor.initialize_state(c)
    now=time.time();ident=(1,'pool',0,10,1)
    state['signals']=[now];state['latch']=[list(ident),'below']
    state['watch']={'watch_id':'x','deadline_epoch':now+100,'duration_seconds':100}
    storage.save_state(p,state)
    restored=monitor.initialize_state(c)
    gate=SignalGate(0,120,3,restored['signals'],latched=(tuple(restored['latch'][0]),restored['latch'][1]))
    assert not gate.observe(False,now+60,10,'below',ident,False)
    assert not gate.observe(True,now+61,11,None,ident,True)
    assert not gate.observe(False,now+62,12,'below',ident,False)
    assert gate.observe(False,now+121,13,'below',ident,False)
    assert monitor.watch_deadline(c,100,restored)[0]=='x'
    assert storage.state_path(p).parent==p.parent
    assert not list(tmp_path.glob('*.tmp'))

def test_corrupt_state_fails_closed_and_no_missing_state_with_rotated_logs(tmp_path):
    p=tmp_path/'monitor.jsonl';c=config(p)
    storage.state_path(p).write_text('{bad')
    with pytest.raises(ValueError,match='state damaged'): monitor.initialize_state(c)
    storage.state_path(p).unlink();p.with_name(p.name+'.1').write_text('{}\n')
    with pytest.raises(ValueError,match='state missing'): monitor.initialize_state(c)

def test_backoff_is_bounded_increases_and_resets():
    values=[monitor.backoff_delay(i,lambda a,b:a) for i in range(1,9)]
    assert values==[5,10,20,40,80,160,300,300]
    assert monitor.backoff_delay(1,lambda a,b:b)==6.25
    assert monitor.backoff_delay(0)==0

def test_failed_atomic_save_raises_fail_closed(tmp_path,monkeypatch):
    def fail(*args): raise OSError('disk full')
    monkeypatch.setattr(monitor,'atomic_save_state',fail)
    with pytest.raises(monitor.DurableStateError): monitor.initialize_state(config(tmp_path/'monitor.jsonl'))

def test_single_writer_lock_blocks_second_instance(tmp_path):
    import fcntl,os
    p=tmp_path/'monitor.jsonl'; c=config(p)
    fd=os.open(str(p)+'.lock',os.O_CREAT|os.O_RDWR,0o600)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        with pytest.raises(RuntimeError,match='already running'): monitor.run(c,once=True)
    finally: os.close(fd)

def test_atomic_save_failure_does_not_replace_valid_state(tmp_path,monkeypatch):
    p=tmp_path/'monitor.jsonl';c=config(p);old=monitor.initialize_state(c)
    def fail(*args): raise OSError('replace failed')
    monkeypatch.setattr(storage.os,'replace',fail)
    modified=dict(old);modified['signals']=[123.0]
    with pytest.raises(OSError): storage.save_state(p,modified)
    assert storage.load_state(p)==old
    assert not list(tmp_path.glob('*.tmp'))
