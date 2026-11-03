import time
from types import SimpleNamespace
from eth_abi import encode
from hexbytes import HexBytes
import pytest
from web3 import Web3
from doma_lp_bot import rebalance_swap as s, rebalance_mint as m
from doma_lp_bot.manual_open import Blocked,POOL_ADDRESS
from doma_lp_bot.v3_math import sqrt_ratio_at_tick

W=Web3.to_checksum_address('0x2222222222222222222222222222222222222222')

def plan():
    return {'token_in':s.USDC,'token_out':s.WETH,'amount_in':100000,'budget_raw':[2500000,900000000000000],'permit_expiry':int(time.time())+1000}

def test_swap_calldata_bound_to_pair_recipient_amount_minimum():
    p=plan();c=SimpleNamespace(wallet=W);end=int(time.time())+100;data=s.encode_swap(c,p,250,end)
    r={'step':'swap','to':s.ROUTER,'data':data,'details':{'amountOutMinimum':250,'deadline':end}}
    assert s.validate_data(c,r,p)==(250,end)
    for bad in ({**p,'amount_in':100001},{**p,'token_out':s.USDC}):
        with pytest.raises(Blocked):s.validate_data(c,r,bad)
    with pytest.raises(Blocked):s.validate_data(SimpleNamespace(wallet=s.USDC),r,p)
    with pytest.raises(Blocked):s.validate_data(c,{**r,'to':s.PERMIT2},p)


def test_replay_budget_and_receipt_transfer_accounting():
    p=plan();event=Web3.keccak(text='Transfer(address,address,uint256)')
    topic=lambda addr:HexBytes(bytes.fromhex(addr[2:]).rjust(32,b'\0'))
    logs=[{'address':s.USDC,'topics':[event,topic(W),topic(POOL_ADDRESS)],'data':HexBytes((100000).to_bytes(32,'big'))},
          {'address':s.WETH,'topics':[event,topic(POOL_ADDRESS),topic(W)],'data':HexBytes((400).to_bytes(32,'big'))}]
    assert s.transfers({'logs':logs},W,p)==400
    done=[{'call':{'step':'decrease'}},{'call':{'step':'collect'},'received_raw':p['budget_raw']},
          {'call':{'step':'swap','details':{'tokenIn':s.USDC,'tokenOut':s.WETH,'amountIn':100000}},'swap_received_raw':400}]
    assert m.available(done)==[2400000,900000000000400]
    with pytest.raises(Blocked):m.available(done+[done[-1]])
    logs[0]['data']=HexBytes((100001).to_bytes(32,'big'))
    with pytest.raises(Blocked):s.transfers({'logs':logs},W,p)


def test_plan_within_received_budget_and_zero_side():
    snapshot={'sqrt':sqrt_ratio_at_tick(199065)}
    p=s.plan_for(snapshot,[2500000,0]);s.validate_plan(p,[2500000,0])
    assert p['token_in']==s.USDC and p['amount_in']<=2500000
    with pytest.raises(Blocked):s.validate_plan({**p,'amount_in':2500001},[2500000,0])
    assert m.available([{'call':{'step':'decrease'}},{'call':{'step':'collect'},'received_raw':[2500000,0]}])==[2500000,0]


def test_limited_approval_encoding():
    p=plan();c=SimpleNamespace(wallet=W);erc=Web3().eth.contract(abi=s.APPROVE_ABI);permit=Web3().eth.contract(abi=s.PERMIT_ABI)
    r={'step':'approve_swap_token','to':s.USDC,'data':erc.encode_abi('approve',args=[s.PERMIT2,p['amount_in']])}
    s.validate_data(c,r,p)
    with pytest.raises(Blocked):s.validate_data(c,{**r,'data':erc.encode_abi('approve',args=[s.PERMIT2,2**256-1])},p)
    r={'step':'approve_swap_permit2','to':s.PERMIT2,'data':permit.encode_abi('approve',args=[s.USDC,s.ROUTER,p['amount_in'],p['permit_expiry']])}
    s.validate_data(c,r,p)
    with pytest.raises(Blocked):s.validate_data(c,{**r,'data':permit.encode_abi('approve',args=[s.USDC,s.ROUTER,p['amount_in'],p['permit_expiry']+1])},p)


def test_swap_restart_reconciles_receipt_without_repeat(tmp_path,monkeypatch):
    from doma_lp_bot import rebalance_manual as rm
    from doma_lp_bot.storage import save_state,load_state
    cfg=SimpleNamespace(wallet=W,log=str(tmp_path/'monitor'));base=cfg.log+'.rebalance-900003';p=plan()
    swap_call={'step':'swap','details':{'tokenIn':s.USDC,'tokenOut':s.WETH,'amountIn':100000}}
    state={'version':1,'scope':rm.scope(cfg),'signals':[],'latch':None,'watch':None,'latest':None,
           'completed':[{'call':{'step':'decrease'},'hash':'decrease'},
                        {'call':{'step':'collect'},'hash':'collect','received_raw':p['budget_raw']}],
           'swap_plan':p,'pending':{'call':swap_call,'hash':'0x'+'ab'*32}}
    save_state(base,state)
    monkeypatch.setattr(rm,'connect',lambda _:object());monkeypatch.setattr(rm,'check_chain',lambda *a:None)
    monkeypatch.setattr(rm,'check_mint',lambda *a:None);monkeypatch.setattr(rm,'wait_confirmations',lambda *a:None)
    monkeypatch.setattr(rm,'reconcile',lambda w,c,record,h: p['budget_raw'] if record['step']=='collect' else None)
    monkeypatch.setattr(s,'reconcile',lambda *a:400)
    monkeypatch.setattr(rm,'read',lambda *a:{})
    monkeypatch.setattr(m,'prepare',lambda *a:(_ for _ in ()).throw(Blocked('mint requested')))
    with pytest.raises(Blocked,match='mint requested'):rm.run(cfg)
    saved=load_state(base)
    assert saved['pending'] is None and saved['completed'][-1]['swap_received_raw']==400
    with pytest.raises(Blocked,match='mint requested'):rm.run(cfg)
    assert [x['call']['step'] for x in load_state(base)['completed']].count('swap')==1


def test_prepare_requotes_only_stale_unsigned_swap(monkeypatch):
    from doma_lp_bot import rebalance_manual as rm
    snapshots=[{'block':1},{'block':2}];calls=[]
    def once(w,cfg,snapshot,plan,budget,steps):
        calls.append(snapshot['block'])
        if snapshot['block']==1:raise Blocked('minOut хуже свежей котировки более чем на 0,5%')
        return {'step':'swap','details':{'slippageBps':50,'amountOutMinimum':995,'quoteOut':1000}}
    monkeypatch.setattr(s,'_prepare_once',once);monkeypatch.setattr(rm,'read',lambda *a:snapshots[1])
    result=s.prepare(None,None,snapshots[0],None,None,None)
    assert calls==[1,2] and result['details']['amountOutMinimum']==result['details']['quoteOut']*995//1000


def test_prepare_stops_on_other_errors_and_bounds_retries(monkeypatch):
    from doma_lp_bot import rebalance_manual as rm
    seen=[]
    def fail(w,cfg,snapshot,plan,budget,steps):seen.append(1);raise Blocked('minOut хуже свежей котировки более чем на 0,5%')
    monkeypatch.setattr(s,'_prepare_once',fail);monkeypatch.setattr(rm,'read',lambda *a:{'block':1})
    with pytest.raises(Blocked,match='Вызов не подготовлен'):s.prepare(None,None,{'block':1},None,None,None)
    assert len(seen)==4
    def stop(*a):raise Blocked('разрешения swap изменились')
    monkeypatch.setattr(s,'_prepare_once',stop)
    with pytest.raises(Blocked,match='разрешения swap изменились'):s.prepare(None,None,{'block':1},None,None,None)
