from types import SimpleNamespace
import pytest
from web3 import Web3
from doma_lp_bot import manual_open as m
from doma_lp_bot.preflight import MINT_ABI
from doma_lp_bot.v3_math import raw_price_from_sqrt, sqrt_ratio_at_tick

W=Web3.to_checksum_address('0x0000000000000000000000000000000000000001')

def test_mint_caps_recipient_and_ticks():
    sqrt=sqrt_ratio_at_tick(199150);cfg=SimpleNamespace(wallet=W);data,p=m.encode_mint(cfg,{'raw':raw_price_from_sqrt(sqrt),'sqrt':sqrt})
    _,x=Web3().eth.contract(abi=MINT_ABI).decode_function_input(data);a=x['params']
    assert (a['token0'],a['token1'],a['fee'],a['recipient'])==(m.USDC,m.WETH,100,W)
    assert (a['amount0Desired'],a['amount1Desired'])==(m.CAP0,m.CAP1)
    assert 0<a['amount0Min']<=m.CAP0 and 0<a['amount1Min']<=m.CAP1 and a['tickLower']<a['tickUpper']

def test_approval_is_exact_and_never_unlimited():
    c=Web3().eth.contract(abi=m.APPROVE_ABI)
    for amount in (m.CAP0,m.CAP1):
        _,x=c.decode_function_input(m.encode_approval(amount));assert x=={'spender':m.MANAGER_ADDRESS,'value':amount}
    with pytest.raises(m.Blocked):m.encode_approval(2**256-1)

def test_mismatched_transaction_cannot_be_reconciled(monkeypatch):
    h='0x'+'12'*32;data=m.encode_approval(m.CAP0)
    tx={'from':W,'to':m.USDC,'input':Web3.to_bytes(hexstr=data),'value':0,'nonce':0,'chainId':97477,'gas':50000,'type':2,'maxFeePerGas':2000000}
    class E:
        def get_transaction(self,h):return tx
        def get_transaction_receipt(self,h):return {'status':1,'blockNumber':5,'blockHash':b'yes','l1Fee':'0x0'}
        def get_block(self,n):return SimpleNamespace(hash=b'yes')
        block_number=6
    w=SimpleNamespace(eth=E());cfg=SimpleNamespace(wallet=W)
    monkeypatch.setattr(m,'oracle',lambda *a:0)
    record={'chainId':97477,'from':W,'value':0,'to':m.USDC,'data':data,'nonce':0,'gasLimit':60000,'maxFeePerGas':2000000,'step':'approve_usdc'}
    assert m.reconcile(w,cfg,record,h)==h
    tx['to']=m.WETH
    with pytest.raises(m.Blocked,match='match'):m.reconcile(w,cfg,record,h)

def test_pending_call_is_not_regenerated_on_restart(tmp_path,monkeypatch,capsys):
    cfg=SimpleNamespace(log=str(tmp_path/'monitor'),wallet=W);calls=[]
    monkeypatch.setattr(m,'connect',lambda cfg:object())
    monkeypatch.setattr(m,'check_chain',lambda w,chain:None)
    monkeypatch.setattr(m,'prepare',lambda w,cfg:(calls.append(1) or {'step':'approve_usdc','nonce':3}))
    monkeypatch.setattr('builtins.input',lambda *a:'q')
    assert m.run(cfg)==0
    assert m.run(cfg)==0
    assert len(calls)==1

def test_extracts_only_new_owned_nft():
    from hexbytes import HexBytes
    topic=Web3.keccak(text='Transfer(address,address,uint256)')
    event={'address':m.MANAGER_ADDRESS,'topics':[topic,HexBytes(bytes(32)),HexBytes(bytes.fromhex(W[2:]).rjust(32,b'\0')),HexBytes((70000).to_bytes(32,'big'))]}
    assert m.minted_token_id({'logs':[event]},W)==70000
    event['topics'][3]=HexBytes((900001).to_bytes(32,'big'))
    with pytest.raises(m.Blocked):m.minted_token_id({'logs':[event]},W)

def test_legacy_rabby_gas_and_allowance_checks(monkeypatch):
    h='0x'+'13'*32;data=m.encode_approval(m.CAP0)
    record={'chainId':97477,'from':W,'value':0,'to':m.USDC,'data':data,'nonce':394,'gasLimit':50316,'maxFeePerGas':2000000,'step':'approve_usdc'}
    tx={'from':W,'to':m.USDC,'input':Web3.to_bytes(hexstr=data),'value':0,'nonce':394,'chainId':97477,'gas':58056,'type':0,'gasPrice':1100276}
    receipt={'status':1,'blockNumber':5,'blockHash':b'yes','l1Fee':'0x15fc93bb'}
    allowance={'v':m.CAP0}
    class E:
        block_number=6
        def get_transaction(self,h):return tx
        def get_transaction_receipt(self,h):return receipt
        def get_block(self,n):return SimpleNamespace(hash=b'yes')
        def contract(self,**kw):
            return SimpleNamespace(functions=SimpleNamespace(allowance=lambda *a:SimpleNamespace(call=lambda **k:allowance['v'])))
    w=SimpleNamespace(eth=E());cfg=SimpleNamespace(wallet=W);monkeypatch.setattr(m,'oracle',lambda *a:0)
    assert m.reconcile(w,cfg,record,h,check_approval_state=True)==h
    allowance['v']=m.CAP0-1
    with pytest.raises(m.Blocked,match='allowance'):m.reconcile(w,cfg,record,h,check_approval_state=True)
    allowance['v']=m.CAP0;tx['gasPrice']=10**12
    with pytest.raises(m.Blocked,match='gas plus'):m.reconcile(w,cfg,record,h,check_approval_state=True)
    tx['gasPrice']=1100276;receipt.pop('l1Fee')
    with pytest.raises(m.Blocked,match='L1 fee'):m.reconcile(w,cfg,record,h,check_approval_state=True)

def test_pending_retained_until_verified_then_progresses(tmp_path,monkeypatch):
    from doma_lp_bot.storage import save_state,load_state
    h='0x'+'13'*32;data=m.encode_approval(m.CAP0);cfg=SimpleNamespace(log=str(tmp_path/'monitor'),wallet=W)
    record={'chainId':97477,'from':W,'value':0,'to':m.USDC,'data':data,'nonce':394,'gasLimit':50316,'maxFeePerGas':2000000,'step':'approve_usdc'}
    tx={'from':W,'to':m.USDC,'input':Web3.to_bytes(hexstr=data),'value':0,'nonce':394,'chainId':97477,'gas':58056,'type':0,'gasPrice':1100276}
    amounts={'value':0}
    class E:
        block_number=6
        def get_transaction(self,h):return tx
        def get_transaction_receipt(self,h):return {'status':1,'blockNumber':5,'blockHash':b'yes','l1Fee':'0x15fc93bb'}
        def get_block(self,n):return SimpleNamespace(hash=b'yes')
        def contract(self,**kw):return SimpleNamespace(functions=SimpleNamespace(allowance=lambda *a:SimpleNamespace(call=lambda **k:amounts['value'])))
    base=str(cfg.log)+'.manual-open';s={'version':1,'scope':m.state_scope(cfg),'signals':[],'latch':None,'watch':None,'latest':None,'pending':{'call':record,'hash':h},'completed':[],'done':False};save_state(base,s)
    monkeypatch.setattr(m,'connect',lambda cfg:SimpleNamespace(eth=E()));monkeypatch.setattr(m,'check_chain',lambda *a:None);monkeypatch.setattr(m,'oracle',lambda *a:0)
    def stop(*a):raise RuntimeError('next step reached')
    monkeypatch.setattr(m,'prepare',stop)
    with pytest.raises(m.Blocked,match='allowance'):m.run(cfg)
    assert load_state(base)['pending']['hash']==h and not load_state(base)['completed']
    amounts['value']=m.CAP0
    with pytest.raises(RuntimeError,match='next step reached'):m.run(cfg)
    state=load_state(base);assert state['pending'] is None and state['completed'][0]['hash']==h

def test_slippage_uses_price_endpoints_not_one_percent_of_token_amounts():
    first=1671061517875299059768523082780579
    later=1670934954931085823243516695521102
    data,details=m.encode_mint(SimpleNamespace(wallet=W),{'sqrt':first,'raw':raw_price_from_sqrt(first)})
    lo,hi=m.ticks_for_pool(details['tickLower'],details['tickUpper'],1)
    liquidity=m.liquidity_for_amounts(later,lo,hi,m.CAP0,m.CAP1)
    used=m.amounts_for_liquidity(later,lo,hi,liquidity,True)
    assert used[1] < details['expected_raw'][1]*99//100  # prior erroneous minimum reverted
    assert used[0]>=details['amount0Min'] and used[1]>=details['amount1Min']
    assert details['slippage_price_bps']==10
    with pytest.raises(m.Blocked,match='exceeds'):m.mint_minima(first,details['tickLower'],details['tickUpper'],101)

def test_price_slippage_recalculates_and_then_stops(monkeypatch):
    from web3.exceptions import ContractLogicError
    calls=[]
    def transient(*a):
        calls.append(1)
        if len(calls)==1:raise ContractLogicError('execution reverted: Price slippage check')
        return {'step':'mint'}
    monkeypatch.setattr(m,'_prepare_once',transient)
    assert m.prepare(None,None)=={'step':'mint'} and len(calls)==2
    def fail(*a):raise ContractLogicError('execution reverted: Price slippage check')
    monkeypatch.setattr(m,'_prepare_once',fail)
    with pytest.raises(m.Blocked,match='Пересчитать'):m.prepare(None,None)

def test_one_percent_price_extremes_fail_at_least_one_minimum():
    import math
    sqrt=sqrt_ratio_at_tick(199150);_,d=m.encode_mint(SimpleNamespace(wallet=W),{'sqrt':sqrt,'raw':raw_price_from_sqrt(sqrt)})
    lo,hi=m.ticks_for_pool(d['tickLower'],d['tickUpper'],1)
    for multiplier in (9900,10100):
        moved=math.isqrt(sqrt*sqrt*multiplier//10000)
        liq=m.liquidity_for_amounts(moved,lo,hi,m.CAP0,m.CAP1)
        used=m.amounts_for_liquidity(moved,lo,hi,liq,True)
        assert used[0]<d['amount0Min'] or used[1]<d['amount1Min']
