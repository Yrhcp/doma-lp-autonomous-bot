"""Read-only, isolated comparison for two explicitly selected LP NFTs.

First observation is not the mint receipt. Fees, durations and valuations are
estimates with explicit gaps; USD performance and actual gas remain UNKNOWN.
"""
from dataclasses import replace
from decimal import Decimal as D
import fcntl
import json
import os
from pathlib import Path
import time
from .storage import load_state,save_state
from .rpc import connect
from .pool import snapshot
from .monitor import backoff_delay
from .preflight import FACTORY_ADDRESS, MANAGER_ADDRESS, WETH, USDC

from .protected import PROTECTED_NFTS
EXPECTED_POOLS={100:'0xd604c96e51df995bb46fab0e3fc1b18d985aa8f5',500:'0xe8e9ca039b1a9467ed32e8b2337f657c8c794754'}

def validate_ids(ids):
    if len(ids)!=2 or any(not isinstance(i,int) or i<=0 or i in PROTECTED_NFTS for i in ids) or ids[0]==ids[1]:
        raise ValueError('provide two distinct new NFT IDs; protected NFTs are excluded')
    return tuple(ids)

def identity(s):
    return [s['token_id'],s['pool'].lower(),s['fee'],s['lower'],s['upper'],s['liquidity']]

def checkpoint(s):
    return [s[k] for k in ('fee_growth_last0_x128','fee_growth_last1_x128','tokens_owed0_raw','tokens_owed1_raw')]

def fees(s):
    f=s.get('fees',{})
    if f.get('status')!='estimated_from_fee_growth': return None
    w,u=D(str(f['accrued_since_update_weth'])),D(str(f['accrued_since_update_usdc']))
    if not w.is_finite() or not u.is_finite() or w<0 or u<0:return None
    return [str(w),str(u)]

def position_observation(s,prev=None,*,max_gap,broken=False):
    now=int(s['block_timestamp']); f=fees(s); value=D(str(s['usdc']))+D(str(s['weth']))*D(str(s['price_usdc_per_weth']))
    if not value.is_finite() or value<=0: raise ValueError('invalid position valuation')
    if prev is None:
        return {'identity':identity(s),'checkpoint':checkpoint(s),'baseline_fees':f,'fees_complete':f is not None,'initial_value_usdc_quote':str(value),'last_value_usdc_quote':str(value),'last_time':now,'last_block':s['block'],'last_status':s['status'],'estimated_in_range_seconds':0,'sample_count':1,'gaps':0}
    if s['block'] <= prev['last_block'] or now < prev['last_time']:raise ValueError('block or time did not advance')
    dt=now-prev['last_time']; gap=broken or dt>max_gap
    stable=prev['identity']==identity(s) and prev['checkpoint']==checkpoint(s)
    complete=prev['fees_complete'] and stable and not gap and f is not None
    base=prev['baseline_fees']
    if complete and any(D(f[i])<D(base[i]) for i in range(2)):complete=False
    return {**prev,'fees_complete':complete,'last_value_usdc_quote':str(value),'last_time':now,'last_block':s['block'],'last_status':s['status'],
            'estimated_in_range_seconds':prev['estimated_in_range_seconds']+(dt if not gap and stable and prev['last_status']=='IN_RANGE' and s['status']=='IN_RANGE' else 0),
            'sample_count':prev['sample_count']+1,'gaps':prev['gaps']+int(gap or not stable)}

def present(s,o):
    accrued=fees(s);earned='UNKNOWN'
    if o['fees_complete'] and accrued is not None:
        earned={'weth':str(D(accrued[0])-D(o['baseline_fees'][0])),'usdc_e':str(D(accrued[1])-D(o['baseline_fees'][1]))}
    return {'token_id':s['token_id'],'fee_tier':s['fee'],'pool':s['pool'],'block':s['block'],'block_time':s['block_timestamp'],
            'status':s['status'],'weth':str(s['weth']),'usdc_e':str(s['usdc']),'price_usdc_per_weth':str(s['price_usdc_per_weth']),
            'estimated_principal_usdc_quote':o['last_value_usdc_quote'],
            'principal_change_since_first_observation_usdc_quote':str(D(o['last_value_usdc_quote'])-D(o['initial_value_usdc_quote'])),
            'fees_since_first_observation_estimate':earned,'current_collectable_estimate':s['fees'],
            'estimated_in_range_seconds_between_matching_samples':o['estimated_in_range_seconds'], 'samples':o['sample_count'],'gaps_or_changes':o['gaps'],
            'mint_cost_and_gas_usd':'UNKNOWN','realized_fees_since_mint':'UNKNOWN','net_pnl_usd':'UNKNOWN','net_roi_percent':'UNKNOWN',
            'note':'first observation is not a mint receipt; fee-growth is an estimate; unchanged endpoint statuses do not prove continuous IN_RANGE; USDC.e quote is not USD'}

def run(cfg,ids,*,watch=False):
    ids=validate_ids(ids)
    if cfg.chain!=97477 or cfg.manager.lower()!=MANAGER_ADDRESS.lower() or cfg.weth.lower()!=WETH.lower() or cfg.usdc.lower()!=USDC.lower():
        raise ValueError('unexpected network or contract configuration')
    name=str(cfg.log)+'.compare.'+'.'.join(str(x) for x in sorted(ids));p=Path(name);p.parent.mkdir(parents=True,exist_ok=True)
    fd=os.open(name+'.lock',os.O_CREAT|os.O_RDWR,0o600)
    try:
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise RuntimeError('comparison already running')
        state=load_state(name);scope={'purpose':'two_nft_read_only','chain':cfg.chain,'wallet':cfg.wallet.lower(),'manager':cfg.manager.lower(),'ids':list(sorted(ids))}
        if state is None:state={'version':1,'scope':scope,'signals':[],'latch':None,'watch':None,'latest':None,'positions':{}}
        elif state['scope']!=scope or not isinstance(state.get('positions'),dict):raise ValueError('comparison state scope mismatch')
        broken=bool(state['positions']);failures=0;w=connect(cfg)
        while True:
            try:
                snapshots=[snapshot(w,replace(cfg,token_id=i)) for i in ids]
                if {s['fee'] for s in snapshots}!={100,500}:raise ValueError('NFTs must be in the 0.01% and 0.05% pools')
                if any(s['factory'].lower()!=FACTORY_ADDRESS.lower() or s['pool'].lower()!=EXPECTED_POOLS[s['fee']] for s in snapshots):
                    raise ValueError('factory or pool identity mismatch')
                updated={str(s['token_id']):position_observation(s,state['positions'].get(str(s['token_id'])),max_gap=cfg.max_observation_gap,broken=broken) for s in snapshots}
                state['positions']=updated;save_state(name,state)
                print(json.dumps({'event':'compare','positions':[present(s,updated[str(s['token_id'])]) for s in snapshots]},ensure_ascii=False,default=str),flush=True)
                broken=False;failures=0
                if not watch:return 0
                time.sleep(cfg.poll)
            except KeyboardInterrupt: return 0
            except Exception as exc:
                broken=True;failures+=1
                # Persist a gap immediately so a crash cannot restore false continuity.
                for item in state['positions'].values():item['fees_complete']=False
                save_state(name,state)
                print(json.dumps({'event':'error','type':type(exc).__name__,'message':str(exc).replace(cfg.rpc,'<RPC_URL>')}),flush=True)
                if not watch:return 1
                time.sleep(backoff_delay(failures));w=connect(cfg)
    finally:os.close(fd)
