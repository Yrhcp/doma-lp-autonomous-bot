import json
import threading
import urllib.request
import urllib.error
from types import SimpleNamespace
import pytest
from doma_lp_bot import rabby_local as r

class Child:
    def poll(self):return None

def test_arm_revalidates_and_cannot_repeat(monkeypatch):
    s=r.Session.__new__(r.Session);s.cfg=SimpleNamespace();s.lock=threading.RLock();s.call={'step':'approve_usdc'};s.attempted=False;s.awaiting=True;s.child=Child();s.status=''
    checks=[];monkeypatch.setattr(r,'connect',lambda c:object());monkeypatch.setattr(r,'verify_before_wallet',lambda w,c,call:checks.append(call))
    assert s.arm()['call']=={'step':'approve_usdc'} and len(checks)==1
    with pytest.raises(ValueError,match='заблокирована'):s.arm()

def test_failed_revalidation_never_arms(monkeypatch):
    s=r.Session.__new__(r.Session);s.cfg=SimpleNamespace();s.lock=threading.RLock();s.call={'step':'mint'};s.attempted=False;s.awaiting=True;s.child=Child()
    monkeypatch.setattr(r,'connect',lambda c:object())
    def fail(*a):raise ValueError('minima changed')
    monkeypatch.setattr(r,'verify_before_wallet',fail)
    with pytest.raises(ValueError,match='minima'):s.arm()
    assert not s.attempted

def test_local_server_requires_token_and_origin():
    class Dummy:
        cfg=SimpleNamespace(rpc='private')
        def state(self):return {'ok':True}
        def arm(self):return {'call':{'step':'test'},'revalidation':{}}
    server=r.Server(Dummy(),'secret');thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        url=server.origin+'/api/state'
        with pytest.raises(urllib.error.HTTPError) as exc:urllib.request.urlopen(url)
        assert exc.value.code==403
        req=urllib.request.Request(url,headers={'X-Local-Token':'secret'})
        assert json.load(urllib.request.urlopen(req))=={'ok':True}
        req=urllib.request.Request(server.origin+'/api/arm',data=b'{}',headers={'X-Local-Token':'secret','Origin':'https://evil.example','Content-Type':'application/json'})
        with pytest.raises(urllib.error.HTTPError) as exc:urllib.request.urlopen(req)
        assert exc.value.code==403
        req=urllib.request.Request(server.origin+'/api/arm',data=b'{}',headers={'X-Local-Token':'secret','Origin':server.origin,'Content-Type':'application/json'})
        assert json.load(urllib.request.urlopen(req))['call']['step']=='test'
    finally:server.shutdown();server.server_close();thread.join()

def test_mint_revalidation_checks_minima_and_exact_call(monkeypatch):
    from decimal import Decimal
    from web3 import Web3
    from eth_abi import encode
    from doma_lp_bot import manual_open as m
    from doma_lp_bot.v3_math import raw_price_from_sqrt,sqrt_ratio_at_tick
    wallet=Web3.to_checksum_address('0x0000000000000000000000000000000000000001')
    cfg=SimpleNamespace(wallet=wallet,max_age=120)
    sqrt=sqrt_ratio_at_tick(199150);now=__import__('time').time()
    snapshot={'nonce':4,'balances':[m.CAP0,m.CAP1],'allowances':[m.CAP0,m.CAP1],'base_fee':10,'block':7,'hash':'abcd','native':10**16,'timestamp':now,'price':Decimal('2200'),'sqrt':sqrt,'tick':199150}
    data,details=m.encode_mint(cfg,{'sqrt':sqrt,'raw':raw_price_from_sqrt(sqrt)})
    record={'step':'mint','chainId':97477,'from':wallet,'value':0,'nonce':4,'to':m.MANAGER_ADDRESS,'data':data,'gasLimit':600000,'maxPriorityFeePerGas':1000000,'maxFeePerGas':2000000,'balances_raw':snapshot['balances'],'allowances_raw':snapshot['allowances'],'details':details}
    class FakeEth:
        def call(self,tx,block_identifier):return encode(['uint256','uint128','uint256','uint256'],[70000,details['liquidity'],*details['expected_raw']])
        def estimate_gas(self,tx,block_identifier):return 500000
        def get_block(self,n):return SimpleNamespace(hash=SimpleNamespace(hex=lambda:'abcd'))
    w=SimpleNamespace(eth=FakeEth());monkeypatch.setattr(m,'read_snapshot',lambda w,c:snapshot);monkeypatch.setattr(m,'oracle',lambda *a:0)
    assert m.verify_before_wallet(w,cfg,record)['block']==7
    w.eth.call=lambda *a,**k:encode(['uint256','uint128','uint256','uint256'],[70000,details['liquidity'],1,details['expected_raw'][1]])
    with pytest.raises(m.Blocked,match='limits'):m.verify_before_wallet(w,cfg,record)

def test_refresh_never_repeats_a_wallet_attempt():
    s=r.Session.__new__(r.Session);s.lock=threading.RLock();s.awaiting=True;s.call={'step':'mint'};s.attempted=True;s.child=Child()
    with pytest.raises(ValueError,match='повтор заблокирован'):s.refresh()

def test_refresh_unsent_call_requests_safe_recalculation():
    import io
    child=Child();child.stdin=io.BytesIO()
    s=r.Session.__new__(r.Session);s.lock=threading.RLock();s.awaiting=True;s.call={'step':'mint'};s.attempted=False;s.child=child;s.error='old'
    s.refresh()
    assert child.stdin.getvalue()==b'cancel\n'
    assert s.call is None and not s.awaiting and s.error is None


def rejection_session():
    import io
    child=Child();child.stdin=io.BytesIO()
    s=r.Session.__new__(r.Session);s.lock=threading.RLock();s.cfg=SimpleNamespace(wallet='wallet',log='/tmp/unused')
    s.rebalance=True;s.awaiting=True;s.attempted=True;s.call={'step':'decrease','nonce':398};s.child=child;s.error='old'
    return s


def rejection_mocks(monkeypatch,s,*,latest=398,pending=398,hash_value=None):
    from doma_lp_bot import storage,rebalance_manual,rpc
    monkeypatch.setattr(rebalance_manual,'scope',lambda cfg:{'nft':900003})
    monkeypatch.setattr(rebalance_manual,'resolve_cycle',lambda w,cfg:cfg)
    state={'scope':{'nft':900003},'pending':{'call':dict(s.call),'hash':hash_value}}
    monkeypatch.setattr(storage,'load_state',lambda path:state)
    monkeypatch.setattr(rpc,'check_chain',lambda w,c:None)
    eth=SimpleNamespace(get_transaction_count=lambda wallet,tag:latest if tag=='latest' else pending)
    monkeypatch.setattr(r,'connect',lambda cfg:SimpleNamespace(eth=eth))
    return state


def test_rejected_decrease_rechecks_journal_and_nonce(monkeypatch):
    s=rejection_session();rejection_mocks(monkeypatch,s)
    s.wallet_rejected(4001)
    assert s.child.stdin.getvalue()==b'cancel\n' and s.call is None and not s.awaiting
    assert s.attempted and s.error is None


@pytest.mark.parametrize('code',[None,4000,4900,'4001',True])
def test_unknown_wallet_errors_do_not_release(monkeypatch,code):
    s=rejection_session();rejection_mocks(monkeypatch,s)
    with pytest.raises(ValueError,match='заблокирован'):s.wallet_rejected(code)
    assert s.child.stdin.getvalue()==b'' and s.call and s.awaiting and s.attempted


@pytest.mark.parametrize('latest,pending,hash_value',[(399,399,None),(398,399,None),(398,398,'0x'+'11'*32)])
def test_rejection_keeps_lock_when_network_or_journal_changed(monkeypatch,latest,pending,hash_value):
    s=rejection_session();rejection_mocks(monkeypatch,s,latest=latest,pending=pending,hash_value=hash_value)
    with pytest.raises(ValueError,match='заблокирован'):s.wallet_rejected(4001)
    assert s.child.stdin.getvalue()==b'' and s.call and s.awaiting and s.attempted


def test_rejection_keeps_lock_when_rpc_unavailable(monkeypatch):
    s=rejection_session();rejection_mocks(monkeypatch,s)
    monkeypatch.setattr(r,'connect',lambda c:(_ for _ in ()).throw(TimeoutError('RPC')))
    with pytest.raises(TimeoutError):s.wallet_rejected(4001)
    assert s.child.stdin.getvalue()==b'' and s.call and s.awaiting and s.attempted


def test_rejection_cannot_clear_other_steps_or_unattempted(monkeypatch):
    s=rejection_session();rejection_mocks(monkeypatch,s);s.call['step']='collect'
    with pytest.raises(ValueError):s.wallet_rejected(4001)
    s.call['step']='decrease';s.attempted=False
    with pytest.raises(ValueError):s.wallet_rejected(4001)
    assert s.child.stdin.getvalue()==b''


def test_unknown_result_retains_lock_and_reports_it():
    s=rejection_session();s.wallet_unknown()
    assert s.attempted and s.awaiting and s.call and s.child.stdin.getvalue()==b''
    assert 'Результат неизвестен' in s.status


def test_restored_hash_never_waits_for_cancel_input():
    import threading
    class Stream:
        def __init__(self):self.data=iter('Ожидается проверка сохранённого hash: decrease\n'.encode());self.done=threading.Event()
        def read(self,n):
            try:return bytes([next(self.data)])
            except StopIteration:self.done.wait(2);return b''
    class RestoredChild:
        def __init__(self):self.stdout=Stream()
        def wait(self):return 0
        def poll(self):return None
    s=r.Session.__new__(r.Session);s.cfg=SimpleNamespace(rpc='private');s.lock=threading.RLock();s.call={'step':'decrease'}
    s.attempted=False;s.awaiting=True;s.status='';s.error=None;s.hash_saved=threading.Event();s.child=RestoredChild()
    t=threading.Thread(target=s._read_utf8);t.start()
    for _ in range(1000):
        if 'Hash сохранён' in s.status:break
        threading.Event().wait(.001)
    assert 'Hash сохранён' in s.status and s.attempted and not s.awaiting and s.call is None
    with pytest.raises(ValueError,match='Нет ожидающего'):s.cancel()
    s.child.stdout.done.set();t.join()


def test_submit_waits_for_durable_hash_ack(monkeypatch):
    import io
    s=rejection_session();rejection_mocks(monkeypatch,s);s.hash_saved=threading.Event()
    class AckPipe(io.BytesIO):
        def flush(self):s.hash_saved.set()
    s.child.stdin=AckPipe();value='0x'+'aa'*32
    s.submit(value)
    assert s.child.stdin.getvalue()==(value+'\n').encode() and not s.awaiting and s.attempted


def test_submit_reports_missing_ack_without_clearing_lock(monkeypatch):
    import io
    class NoAck:
        def clear(self):pass
        def wait(self,timeout):return False
    s=rejection_session();rejection_mocks(monkeypatch,s);s.hash_saved=NoAck();s.child.stdin=io.BytesIO()
    with pytest.raises(ValueError,match='сохранение hash'):s.submit('0x'+'aa'*32)
    assert not s.awaiting and s.attempted and s.call is None


def test_completed_hash_not_sent_to_next_step(monkeypatch):
    s=rejection_session();saved=rejection_mocks(monkeypatch,s);value='0x'+'ab'*32
    saved['completed']=[{'hash':value,'call':{'step':'collect'}}]
    with pytest.raises(ValueError,match='уже подтверждён'):s.submit(value.upper().replace('0X','0x'))
    assert s.child.stdin.getvalue()==b'' and s.awaiting and s.call is not None


@pytest.mark.parametrize('value',['',' ','0x','0x123','0x'+'g'*64])
def test_blank_or_invalid_hash_never_reaches_child(value):
    s=rejection_session()
    with pytest.raises(ValueError):s.submit(value)
    assert s.child.stdin.getvalue()==b'' and s.awaiting and s.call is not None
