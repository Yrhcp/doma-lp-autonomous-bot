"""Isolated, durable laboratory journal. Not enabled for Doma/mainnet.

No signing or sending. External wallet transactions cannot be prevented by this
journal. Reconciliation and semantics require independent audit before use.
"""
import fcntl
import hashlib
import json
import os
from contextlib import contextmanager
from pathlib import Path
from web3 import Web3
from .storage import save_state,load_state

class OperationBlocked(ValueError): pass

@contextmanager
def _locked(base):
    p=Path(base);p.parent.mkdir(parents=True,exist_ok=True)
    fd=os.open(str(p)+'.op.lock',os.O_CREAT|os.O_RDWR,0o600)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX)
        yield
    finally:os.close(fd)

def _read(base):
    s=load_state(base)
    if s is None:
        return {'version':1,'scope':{'purpose':'LOCAL_LAB_ONLY'},'signals':[],'latch':None,'watch':None,'latest':None,'operations':{}}
    if s.get('scope')!={'purpose':'LOCAL_LAB_ONLY'} or not isinstance(s.get('operations'),dict):
        raise OperationBlocked('invalid or wrong-scope journal; stop')
    return s

def _normalize(steps):
    if not isinstance(steps,list) or not steps:raise OperationBlocked('empty steps')
    out=[]
    for t in steps:
        if set(t)!={'from','to','data','value'}:raise OperationBlocked('invalid transaction fields')
        a=Web3.to_checksum_address(t['from']);b=Web3.to_checksum_address(t['to'])
        data=t['data']
        if not isinstance(data,str) or not data.startswith('0x') or len(data)<10 or len(data)%2:raise OperationBlocked('invalid data')
        try:bytes.fromhex(data[2:])
        except ValueError as e:raise OperationBlocked('invalid hex') from e
        if t['value']!=0:raise OperationBlocked('native value not supported')
        out.append({'from':a,'to':b,'data':data.lower(),'value':0})
    return out

def create_lab_operation(base,*,chain_id,steps):
    if chain_id!=31337:raise OperationBlocked('journal enabled only on isolated chain 31337')
    steps=_normalize(steps)
    if len({t['from'].lower() for t in steps})!=1:raise OperationBlocked('mixed signers')
    canonical=json.dumps({'chain_id':chain_id,'steps':steps},sort_keys=True,separators=(',',':'))
    ident=hashlib.sha256(canonical.encode()).hexdigest()
    with _locked(base):
        state=_read(base)
        if ident in state['operations']:raise OperationBlocked('duplicate plan; inspect existing operation')
        state['operations'][ident]={'chain_id':chain_id,'status':'OPEN','steps':[{'transaction':x,'status':'CREATED','tx_hash':None} for x in steps]}
        save_state(base,state)
    return ident

def record_lab_submission(base,ident,index,tx_hash):
    if not isinstance(tx_hash,str) or len(tx_hash)!=66 or not tx_hash.startswith('0x'):raise OperationBlocked('invalid transaction hash')
    try:bytes.fromhex(tx_hash[2:])
    except ValueError as e:raise OperationBlocked('invalid transaction hash') from e
    with _locked(base):
        state=_read(base); op=state['operations'][ident]
        if op['status']!='OPEN' or not 0<=index<len(op['steps']):raise OperationBlocked('operation held or index invalid')
        step=op['steps'][index]
        if step['status']=='SUBMITTED' and step['tx_hash']==tx_hash.lower():return
        if step['status']!='CREATED' or any(s['status']!='CONFIRMED' for s in op['steps'][:index]):raise OperationBlocked('duplicate or incomplete previous step')
        step['status']='SUBMITTED';step['tx_hash']=tx_hash.lower();save_state(base,state)

def hold_lab_unknown(base,ident,index):
    """Stop the lab workflow when an external action may have happened.

    No attempt is made to send, retry, infer a nonce, or resolve uncertainty.
    This cannot eliminate the crash window before an external wallet action is
    recorded and is not a production transaction-recovery implementation.
    """
    with _locked(base):
        state=_read(base);op=state['operations'][ident]
        if op['status']!='OPEN' or not 0<=index<len(op['steps']):
            raise OperationBlocked('operation held or index invalid')
        step=op['steps'][index]
        if step['status'] not in ('CREATED','SUBMITTED') or any(s['status']!='CONFIRMED' for s in op['steps'][:index]):
            raise OperationBlocked('invalid unknown-outcome step')
        op['status']='HOLD_UNKNOWN';step['status']='HOLD_UNKNOWN'
        save_state(base,state)

def reconcile_lab_step(base,ident,index,*,chain_id,transaction,receipt,canonical_block_hash,head_block,confirmations=2):
    if chain_id!=31337:raise OperationBlocked('only local chain')
    if not isinstance(confirmations,int) or confirmations<1:raise OperationBlocked('invalid confirmations')
    with _locked(base):
        state=_read(base);op=state['operations'][ident];step=op['steps'][index]
        if step['status'] not in ('SUBMITTED','CONFIRMED'):raise OperationBlocked('step cannot be reconciled')
        if transaction is None or receipt is None:
            if step['status']=='CONFIRMED': op['status']='HOLD_REORG';step['status']='HOLD_REORG'
            save_state(base,state);return step['status']
        expected=step['transaction']
        try:
            actual={'from':Web3.to_checksum_address(transaction['from']),'to':Web3.to_checksum_address(transaction['to']),
                    'data':transaction['input'].lower(),'value':transaction['value']}
            txhash=transaction['hash'].lower();rh=receipt['transactionHash'].lower();bh=receipt['blockHash'].lower()
            if actual!=expected or txhash!=step['tx_hash'] or rh!=txhash:raise ValueError('transaction differs')
            if bh!=canonical_block_hash.lower() or head_block < receipt['blockNumber']:raise ValueError('block differs')
        except (KeyError,ValueError,TypeError,AttributeError) as exc:
            op['status']='HOLD_MISMATCH';step['status']='HOLD_MISMATCH';save_state(base,state)
            raise OperationBlocked('transaction or chain mismatch; manual inspection required') from exc
        if receipt['status']!=1:
            op['status']='HOLD_FAILED';step['status']='HOLD_FAILED'
        elif head_block-receipt['blockNumber']+1>=confirmations:
            step['status']='CONFIRMED'
            if all(s['status']=='CONFIRMED' for s in op['steps']):op['status']='COMPLETE'
        else:step['status']='SUBMITTED'
        save_state(base,state)
        return step['status']
