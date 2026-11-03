import json
import pytest
from doma_lp_bot.operation_lab import *
from doma_lp_bot.storage import state_path
W='0x0000000000000000000000000000000000000001'
T='0x0000000000000000000000000000000000000002'
H='0x'+'11'*32
B='0x'+'22'*32
S={'from':W,'to':T,'data':'0x095ea7b3','value':0}

def state(p,ident):return json.loads(state_path(p).read_text())['operations'][ident]

def test_duplicate_and_partial_restart_reconciliation(tmp_path):
    p=tmp_path/'journal';ident=create_lab_operation(p,chain_id=31337,steps=[S,S])
    with pytest.raises(OperationBlocked,match='duplicate'):create_lab_operation(p,chain_id=31337,steps=[S,S])
    with pytest.raises(OperationBlocked,match='previous'):record_lab_submission(p,ident,1,H)
    record_lab_submission(p,ident,0,H);record_lab_submission(p,ident,0,H)
    with pytest.raises(OperationBlocked,match='duplicate'):record_lab_submission(p,ident,0,'0x'+'33'*32)
    tx={**S,'input':S['data'],'hash':H};tx.pop('data')
    receipt={'transactionHash':H,'blockHash':B,'blockNumber':100,'status':1}
    assert reconcile_lab_step(p,ident,0,chain_id=31337,transaction=tx,receipt=receipt,canonical_block_hash=B,head_block=100)=='SUBMITTED'
    assert reconcile_lab_step(p,ident,0,chain_id=31337,transaction=tx,receipt=receipt,canonical_block_hash=B,head_block=101)=='CONFIRMED'
    assert state(p,ident)['steps'][0]['status']=='CONFIRMED'
    record_lab_submission(p,ident,1,'0x'+'44'*32)
    assert state(p,ident)['steps'][1]['status']=='SUBMITTED'

def test_failed_or_mismatched_transaction_halts(tmp_path):
    p=tmp_path/'j'; ident=create_lab_operation(p,chain_id=31337,steps=[S])
    record_lab_submission(p,ident,0,H)
    tx={'from':W,'to':T,'input':S['data'],'value':0,'hash':H}
    receipt={'transactionHash':H,'blockHash':B,'blockNumber':100,'status':0}
    assert reconcile_lab_step(p,ident,0,chain_id=31337,transaction=tx,receipt=receipt,canonical_block_hash=B,head_block=101)=='HOLD_FAILED'
    assert state(p,ident)['status']=='HOLD_FAILED'
    p2=tmp_path/'k'; i=create_lab_operation(p2,chain_id=31337,steps=[S]);record_lab_submission(p2,i,0,H)
    with pytest.raises(OperationBlocked,match='mismatch'):
        reconcile_lab_step(p2,i,0,chain_id=31337,transaction={**tx,'input':'0x12345678'},receipt={**receipt,'status':1},canonical_block_hash=B,head_block=101)
    assert state(p2,i)['status']=='HOLD_MISMATCH'

def test_reorg_blocks_and_mainnet_never_enabled(tmp_path):
    p=tmp_path/'j'
    with pytest.raises(OperationBlocked,match='isolated'):create_lab_operation(p,chain_id=97477,steps=[S])
    ident=create_lab_operation(p,chain_id=31337,steps=[S,S]);record_lab_submission(p,ident,0,H)
    tx={'from':W,'to':T,'input':S['data'],'value':0,'hash':H};receipt={'transactionHash':H,'blockHash':B,'blockNumber':100,'status':1}
    reconcile_lab_step(p,ident,0,chain_id=31337,transaction=tx,receipt=receipt,canonical_block_hash=B,head_block=101)
    assert reconcile_lab_step(p,ident,0,chain_id=31337,transaction=None,receipt=None,canonical_block_hash=B,head_block=101)=='HOLD_REORG'
    with pytest.raises(OperationBlocked):record_lab_submission(p,ident,1,'0x'+'44'*32)

def _confirm(p, ident, index, value, status=1):
    txhash='0x'+f'{value:02x}'*32
    record_lab_submission(p,ident,index,txhash)
    tx={'from':W,'to':T,'input':S['data'],'value':0,'hash':txhash}
    receipt={'transactionHash':txhash,'blockHash':B,'blockNumber':100,'status':status}
    return reconcile_lab_step(p,ident,index,chain_id=31337,transaction=tx,receipt=receipt,canonical_block_hash=B,head_block=101)

def test_swap_confirmed_then_mint_failed_stays_held_after_restart(tmp_path):
    p=tmp_path/'j'; ident=create_lab_operation(p,chain_id=31337,steps=[S,S,S])
    assert _confirm(p,ident,0,1)=='CONFIRMED'  # approval
    assert _confirm(p,ident,1,2)=='CONFIRMED'  # swap
    assert _confirm(p,ident,2,3,status=0)=='HOLD_FAILED'  # mint
    assert [v['status'] for v in state(p,ident)['steps']]==['CONFIRMED','CONFIRMED','HOLD_FAILED']
    with pytest.raises(OperationBlocked):record_lab_submission(p,ident,1,'0x'+'04'*32)
    with pytest.raises(OperationBlocked):create_lab_operation(p,chain_id=31337,steps=[S,S,S])

def test_approval_confirmed_then_swap_failed_does_not_retry(tmp_path):
    p=tmp_path/'j'; ident=create_lab_operation(p,chain_id=31337,steps=[S,S,S])
    assert _confirm(p,ident,0,1)=='CONFIRMED'
    assert _confirm(p,ident,1,2,status=0)=='HOLD_FAILED'
    with pytest.raises(OperationBlocked):record_lab_submission(p,ident,2,'0x'+'03'*32)
    assert state(p,ident)['steps'][2]['status']=='CREATED'

@pytest.mark.parametrize('known_hash',[True,False])
def test_unknown_outcome_requires_hold_and_cannot_restart_or_advance(tmp_path,known_hash):
    p=tmp_path/'j'; ident=create_lab_operation(p,chain_id=31337,steps=[S,S])
    assert _confirm(p,ident,0,1)=='CONFIRMED'
    if known_hash:record_lab_submission(p,ident,1,'0x'+'02'*32)
    hold_lab_unknown(p,ident,1)
    assert state(p,ident)['status']=='HOLD_UNKNOWN'
    with pytest.raises(OperationBlocked):record_lab_submission(p,ident,1,'0x'+'03'*32)
    with pytest.raises(OperationBlocked):create_lab_operation(p,chain_id=31337,steps=[S,S])
