import io
import json
import time
from types import SimpleNamespace

import pytest
from web3 import Web3
from eth_abi import encode
from hexbytes import HexBytes

from doma_lp_bot import rebalance_manual as m
from doma_lp_bot.storage import load_state, save_state
from doma_lp_bot.v3_math import sqrt_ratio_at_tick

WALLET=Web3.to_checksum_address('0x2222222222222222222222222222222222222222')

def sample():
    cfg=SimpleNamespace(wallet=WALLET)
    p=[0,None,m.USDC,m.WETH,100,198997,199138,13516264189505,0,0,0,0]
    s={'position':p,'sqrt':sqrt_ratio_at_tick(199065),'nonce':7}
    return cfg,s

def record(cfg,s,step):
    data,details=m.make_data(step,cfg,s)
    return {'step':step,'chainId':97477,'from':cfg.wallet,'to':m.MANAGER_ADDRESS,'data':data,'value':0,'nonce':s['nonce'],'details':details}

def test_decrease_only_allowed_nft_and_exact_liquidity():
    cfg,s=sample();r=record(cfg,s,'decrease')
    assert m.validate_call(cfg,r,s)['tokenId']==900003
    c=Web3().eth.contract(abi=m.ABI);_,args=c.decode_function_input(r['data']);p=args['params']
    for changes in ({'tokenId':900001},{'liquidity':1},{'amount0Min':0}):
        vals={**p,**changes};r2={**r,'data':c.encode_abi('decreaseLiquidity',args=[tuple(vals[k] for k in ('tokenId','liquidity','amount0Min','amount1Min','deadline'))])}
        with pytest.raises(m.Blocked):m.validate_call(cfg,r2,s)

def test_collect_limits_and_recipient():
    cfg,s=sample();s['position'][7]=0;s['position'][10:12]=[2500000,1100000000000000]
    r=record(cfg,s,'collect');p=m.validate_call(cfg,r,s)
    assert (p['amount0Max'],p['amount1Max'])==(2500000,1100000000000000)
    s['position'][10]+=1
    with pytest.raises(m.Blocked):m.validate_call(cfg,r,s)

def test_state_restart_reconciles_pending_once(tmp_path,monkeypatch,capsys):
    cfg=SimpleNamespace(wallet=WALLET,log=str(tmp_path/'monitor'))
    base=cfg.log+'.rebalance-900003';call={'step':'decrease','nonce':7}
    state={'version':1,'scope':m.scope(cfg),'signals':[],'latch':None,'watch':None,'latest':None,
           'pending':{'call':call,'hash':'0x'+'12'*32},'completed':[]}
    save_state(base,state); checks=[]
    monkeypatch.setattr(m,'connect',lambda _:object());monkeypatch.setattr(m,'check_mint',lambda *a:None);monkeypatch.setattr(m,'check_chain',lambda *a:None);monkeypatch.setattr(m,'wait_confirmations',lambda *a:None)
    monkeypatch.setattr(m,'reconcile',lambda w,c,call,h:checks.append(call['step']))
    monkeypatch.setattr(m,'prepare',lambda w,c,step: (_ for _ in ()).throw(m.Blocked('stop after restore')))
    with pytest.raises(m.Blocked,match='stop after restore'):m.run(cfg)
    saved=load_state(base)
    assert saved['pending'] is None and [x['call']['step'] for x in saved['completed']]==['decrease']
    assert checks==['decrease','decrease']
    with pytest.raises(m.Blocked,match='stop after restore'):m.run(cfg)
    assert [x['call']['step'] for x in load_state(base)['completed']]==['decrease']


def test_pending_wrong_step_fails_closed(tmp_path,monkeypatch):
    cfg=SimpleNamespace(wallet=WALLET,log=str(tmp_path/'monitor'));base=cfg.log+'.rebalance-900003'
    save_state(base,{'version':1,'scope':m.scope(cfg),'signals':[],'latch':None,'watch':None,'latest':None,
                     'pending':{'call':{'step':'collect'},'hash':None},'completed':[]})
    monkeypatch.setattr(m,'connect',lambda _:object());monkeypatch.setattr(m,'check_mint',lambda *a:None);monkeypatch.setattr(m,'check_chain',lambda *a:None);monkeypatch.setattr(m,'wait_confirmations',lambda *a:None)
    with pytest.raises(m.Blocked,match='pending'):m.run(cfg)
    assert load_state(base)['pending'] is not None

def test_collect_receipt_requires_matching_transfer_amounts():
    topic=lambda x:HexBytes(Web3.keccak(text=x))
    wallet_topic=HexBytes(bytes.fromhex(WALLET[2:]).rjust(32,b'\0'))
    pool_topic=HexBytes(bytes.fromhex(m.POOL_ADDRESS[2:]).rjust(32,b'\0'))
    logs=[{'address':m.MANAGER_ADDRESS,'topics':[topic('Collect(uint256,address,uint256,uint256)'),HexBytes((900003).to_bytes(32,'big'))],
           'data':HexBytes(encode(['address','uint256','uint256'],[WALLET,100,200]))}]
    for token,amount in ((m.USDC,100),(m.WETH,200)):
        logs.append({'address':token,'topics':[topic('Transfer(address,address,uint256)'),pool_topic,wallet_topic],
                     'data':HexBytes(amount.to_bytes(32,'big'))})
    assert m.received_from_receipt({'logs':logs},WALLET,(100,200))==[100,200]
    logs[-1]['data']=HexBytes((199).to_bytes(32,'big'))
    assert m.received_from_receipt({'logs':logs},WALLET,(100,200))==[100,199]
    logs[-1]['data']=HexBytes((201).to_bytes(32,'big'))
    with pytest.raises(m.Blocked,match='превышают'):m.received_from_receipt({'logs':logs},WALLET)

def test_error_does_not_clear_pending_hash(tmp_path,monkeypatch):
    cfg=SimpleNamespace(wallet=WALLET,log=str(tmp_path/'monitor'));base=cfg.log+'.rebalance-900003'
    pending={'call':{'step':'decrease'},'hash':'0x'+'ab'*32}
    save_state(base,{'version':1,'scope':m.scope(cfg),'signals':[],'latch':None,'watch':None,'latest':None,
                     'pending':pending,'completed':[]})
    monkeypatch.setattr(m,'connect',lambda _:object());monkeypatch.setattr(m,'check_mint',lambda *a:None);monkeypatch.setattr(m,'check_chain',lambda *a:None);monkeypatch.setattr(m,'wait_confirmations',lambda *a:None)
    monkeypatch.setattr(m,'reconcile',lambda *a:(_ for _ in ()).throw(TimeoutError('RPC')))
    with pytest.raises(TimeoutError):m.run(cfg)
    assert load_state(base)['pending']==pending

def test_failed_receipt_retry_requires_unchanged_position(tmp_path,monkeypatch):
    cfg,s=sample();cfg.log=str(tmp_path/'monitor');base=cfg.log+'.rebalance-900003';call=record(cfg,s,'decrease')
    pending={'call':call,'hash':'0x'+'ab'*32}
    save_state(base,{'version':1,'scope':m.scope(cfg),'signals':[],'latch':None,'watch':None,'latest':None,
                     'pending':pending,'completed':[]})
    monkeypatch.setattr(m,'connect',lambda _:object());monkeypatch.setattr(m,'check_mint',lambda *a:None);monkeypatch.setattr(m,'check_chain',lambda *a:None);monkeypatch.setattr(m,'wait_confirmations',lambda *a:None)
    monkeypatch.setattr(m,'reconcile',lambda *a:(_ for _ in ()).throw(m.FailedTransaction('failed')))
    monkeypatch.setattr('builtins.input',lambda _: 'retry')
    monkeypatch.setattr(m,'read',lambda *a:{**s,'nonce':8})
    monkeypatch.setattr(m,'prepare',lambda *a:(_ for _ in ()).throw(m.Blocked('recalculate')))
    with pytest.raises(m.Blocked,match='recalculate'):m.run(cfg)
    assert load_state(base)['pending'] is None

def test_failed_receipt_retry_blocks_if_position_changed(tmp_path,monkeypatch):
    cfg,s=sample();cfg.log=str(tmp_path/'monitor');base=cfg.log+'.rebalance-900003';call=record(cfg,s,'decrease')
    pending={'call':call,'hash':'0x'+'ab'*32}
    save_state(base,{'version':1,'scope':m.scope(cfg),'signals':[],'latch':None,'watch':None,'latest':None,
                     'pending':pending,'completed':[]})
    monkeypatch.setattr(m,'connect',lambda _:object());monkeypatch.setattr(m,'check_mint',lambda *a:None);monkeypatch.setattr(m,'check_chain',lambda *a:None);monkeypatch.setattr(m,'wait_confirmations',lambda *a:None)
    monkeypatch.setattr(m,'reconcile',lambda *a:(_ for _ in ()).throw(m.FailedTransaction('failed')))
    monkeypatch.setattr('builtins.input',lambda _: 'retry')
    s['position'][7]-=1
    monkeypatch.setattr(m,'read',lambda *a:{**s,'nonce':8})
    with pytest.raises(m.Blocked,match='изменилось'):m.run(cfg)
    assert load_state(base)['pending']==pending


def test_new_hash_saved_before_wait_failure(tmp_path,monkeypatch):
    cfg=SimpleNamespace(wallet=WALLET,log=str(tmp_path/'monitor'));base=cfg.log+'.rebalance-900003'
    pending={'call':{'step':'decrease'},'hash':None}
    save_state(base,{'version':1,'scope':m.scope(cfg),'signals':[],'latch':None,'watch':None,'latest':None,'pending':pending,'completed':[]})
    value='0x'+'cd'*32
    monkeypatch.setattr(m,'connect',lambda _:object());monkeypatch.setattr(m,'check_mint',lambda *a:None);monkeypatch.setattr(m,'check_chain',lambda *a:None)
    monkeypatch.setattr('builtins.input',lambda _:value)
    monkeypatch.setattr(m,'check_new_hash',lambda *a:None)
    def failed(w,h):
        assert load_state(base)['pending']['hash']==value
        raise TimeoutError('RPC')
    monkeypatch.setattr(m,'wait_confirmations',failed)
    with pytest.raises(TimeoutError):m.run(cfg)
    assert load_state(base)['pending']['hash']==value


def test_wait_confirmations_requires_second_block():
    from types import SimpleNamespace
    state={'n':10}
    class Eth:
        def wait_for_transaction_receipt(self,*a,**k):return {'blockNumber':10}
        @property
        def block_number(self):state['n']+=1;return state['n']
    m.wait_confirmations(SimpleNamespace(eth=Eth()),'0x'+'01'*32,timeout=1)
    assert state['n']==11


def test_wait_confirmation_timeout_keeps_transaction_pending():
    from types import SimpleNamespace
    from web3.exceptions import TimeExhausted
    def no_receipt(*a,**k):raise TimeExhausted('not mined')
    with pytest.raises(m.Blocked,match='hash сохранён'):
        m.wait_confirmations(SimpleNamespace(eth=SimpleNamespace(wait_for_transaction_receipt=no_receipt)),'0x'+'01'*32,timeout=1)


def test_check_new_hash_matches_complete_transaction_and_rejects_mismatched_fields():
    from types import SimpleNamespace
    cfg,s=sample();recorded=record(cfg,s,'collect') if not s['position'][7] else None
    s['position'][7]=0;s['position'][10:12]=[42,59];recorded=record(cfg,s,'collect')
    h='0x'+'ab'*32
    tx={'chainId':97477,'from':cfg.wallet,'to':recorded['to'],'nonce':recorded['nonce'],'value':0,'input':bytes.fromhex(recorded['data'][2:])}
    w=SimpleNamespace(eth=SimpleNamespace(get_transaction=lambda _:tx))
    m.check_new_hash(w,h,recorded,[])
    with pytest.raises(m.Blocked,match='подтверждённому шагу'):m.check_new_hash(w,h,recorded,[{'hash':h}])
    for key,value in (('nonce',398),('to',m.USDC),('chainId',1),('input',b'bad')):
        altered={**tx,key:value};w.eth.get_transaction=lambda _,altered=altered:altered
        with pytest.raises(m.Blocked,match='не соответствует'):m.check_new_hash(w,h,recorded,[])


def test_new_hash_not_yet_propagated_keeps_existing_recovery_path():
    from types import SimpleNamespace
    from web3.exceptions import TransactionNotFound
    def unavailable(_):raise TransactionNotFound('not yet visible')
    m.check_new_hash(SimpleNamespace(eth=SimpleNamespace(get_transaction=unavailable)),'0x'+'ab'*32,{},[])


def test_wrong_known_hash_does_not_poison_pending(tmp_path,monkeypatch):
    from types import SimpleNamespace
    cfg,s=sample();cfg.log=str(tmp_path/'monitor');base=cfg.log+'.rebalance-900003'
    call=record(cfg,s,'decrease');state={'version':1,'scope':m.scope(cfg),'signals':[],'latch':None,'watch':None,'latest':None,'pending':{'call':call,'hash':None},'completed':[]}
    save_state(base,state);h='0x'+'ab'*32
    wrong={'chainId':97477,'from':cfg.wallet,'to':m.USDC,'nonce':call['nonce'],'value':0,'input':bytes.fromhex(call['data'][2:])}
    monkeypatch.setattr(m,'connect',lambda _:SimpleNamespace(eth=SimpleNamespace(get_transaction=lambda _:wrong)))
    monkeypatch.setattr(m,'check_mint',lambda *a:None);monkeypatch.setattr(m,'check_chain',lambda *a:None)
    answers=iter([h,'q']);monkeypatch.setattr('builtins.input',lambda _:next(answers))
    assert m.run(cfg)==0
    assert load_state(base)['pending']['hash'] is None


def test_active_chain_restart_and_pending_recovery(tmp_path,monkeypatch):
    from doma_lp_bot import rebalance_mint
    cfg=SimpleNamespace(wallet=WALLET,log=str(tmp_path/'monitor'))
    def journal(n,done,pending=None):
        c=m.with_token(cfg,n)
        save_state(m.cycle_base(c),{'version':1,'scope':m.scope(c),'signals':[],'latch':None,
                    'watch':None,'latest':None,'pending':pending,'completed':done})
    def completed(new):
        return [{'call':{'step':'decrease'},'hash':'d'}, {'call':{'step':'collect'},'hash':'c'},
                {'call':{'step':'mint_new'},'hash':f'mint-{new}','new_token_id':new}]
    calls=[]
    class F:
        def positions(self,n):return SimpleNamespace(call=lambda:[0,0,m.USDC,m.WETH,100,1,2,5])
        def ownerOf(self,n):return SimpleNamespace(call=lambda:WALLET)
    w=SimpleNamespace(eth=SimpleNamespace(contract=lambda **kw:SimpleNamespace(functions=F())))
    def verified(w,c,record,h,done):
        calls.append((m.current_id(c),h)); return int(h.split('-')[1])
    monkeypatch.setattr(rebalance_mint,'reconcile',verified)
    journal(900003,completed(900007))
    assert m.current_id(m.resolve_cycle(w,cfg,persist=True))==900007
    assert load_state(m.cycle_base(m.with_token(cfg,900003)))['active_token_id']==900007
    assert m.journal_active_hint(cfg)==900007
    # A restart while decrease is pending must keep that pending step intact.
    pending={'call':{'step':'decrease','nonce':406},'hash':'0x'+'12'*32}
    journal(900007,[],pending)
    assert m.current_id(m.resolve_cycle(w,cfg,persist=True))==900007
    assert load_state(m.cycle_base(m.with_token(cfg,900007)))['pending']==pending
    # Crash after saving confirmed mint but before updating the active pointer.
    journal(900007,completed(900005))
    assert m.current_id(m.resolve_cycle(w,cfg,persist=True))==900005
    assert m.journal_active_hint(cfg)==900005
    assert m.current_id(m.resolve_cycle(w,cfg,persist=True))==900005
    assert calls.count((900007,'mint-900005'))>=1
    assert load_state(m.cycle_base(m.with_token(cfg,900007)))['completed'][-1]['new_token_id']==900005


def test_active_chain_rejects_bad_mint_and_protected_nft(tmp_path,monkeypatch):
    from doma_lp_bot import rebalance_mint
    cfg=SimpleNamespace(wallet=WALLET,log=str(tmp_path/'monitor'))
    done=[{'call':{'step':'mint_new'},'hash':'bad','new_token_id':900001}]
    state={'version':1,'scope':m.scope(cfg),'signals':[],'latch':None,'watch':None,'latest':None,'pending':None,'completed':done}
    save_state(m.cycle_base(cfg),state)
    monkeypatch.setattr(rebalance_mint,'reconcile',lambda *a:900001)
    with pytest.raises(m.Blocked):m.resolve_cycle(None,cfg,persist=True)
    assert load_state(m.active_base(cfg)) is None
    with pytest.raises(m.Blocked):m.current_id(m.with_token(cfg,900001))
    monkeypatch.setattr(rebalance_mint,'reconcile',lambda *a:(_ for _ in ()).throw(m.Blocked('receipt')))
    with pytest.raises(m.Blocked,match='receipt'):m.resolve_cycle(None,cfg,persist=True)
    assert load_state(m.active_base(cfg)) is None
