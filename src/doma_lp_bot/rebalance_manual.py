"""Manually confirmed rebalance; no transaction is sent here."""
from .protected import PROTECTED_NFTS
import fcntl
import json
import math
import os
import time
from decimal import Decimal
from pathlib import Path

import rlp
from eth_abi import decode
from web3 import Web3

from .contracts import MANAGER, FACTORY, POOL
from .manual_open import (Blocked, FACTORY_ADDRESS, MANAGER_ADDRESS, POOL_ADDRESS,
                          USDC, WETH, MAX_STEP_FEE_WEI, minted_token_id, oracle)
from .preflight import TOKEN, QUOTER, QUOTER_ABI
from .rebalance_preview import DECREASE_ABI, MINT_HASH, TOKEN_ID, estimate_swap
from .rpc import check_chain, connect
from .storage import load_state, save_state
from .v3_integer import amounts_for_liquidity, ticks_for_pool
from .v3_math import sqrt_ratio_at_tick, raw_price_from_sqrt, range_for_quote

COLLECT_ABI = [{'type':'function','name':'collect','stateMutability':'payable',
 'inputs':[{'name':'params','type':'tuple','components':[{'name':'tokenId','type':'uint256'},
 {'name':'recipient','type':'address'},{'name':'amount0Max','type':'uint128'},
 {'name':'amount1Max','type':'uint128'}]}],
 'outputs':[{'name':'amount0','type':'uint256'},{'name':'amount1','type':'uint256'}]}]
ABI = DECREASE_ABI + COLLECT_ABI

class FailedTransaction(Blocked):
    pass


def current_id(cfg):
    token_id=getattr(cfg, 'token_id', None)
    token_id=TOKEN_ID if token_id is None else token_id
    if type(token_id) is not int or token_id <= 0 or token_id in PROTECTED_NFTS:
        raise Blocked('запрещённый tokenId')
    return token_id


def cycle_base(cfg):
    return str(cfg.log)+f'.rebalance-{current_id(cfg)}'


def scope(cfg):
    token_id=current_id(cfg)
    return {'purpose':f'manual_rebalance_{token_id}','chain':97477,'wallet':cfg.wallet.lower(),
            'manager':MANAGER_ADDRESS.lower(),'nft':token_id}


def with_token(cfg, token_id):
    from dataclasses import is_dataclass, replace
    from copy import copy
    if is_dataclass(cfg): return replace(cfg, token_id=token_id)
    result=copy(cfg); result.token_id=token_id; return result


def active_base(cfg):
    return str(cfg.log)+'.rebalance-active'


def active_scope(cfg):
    return {'purpose':'manual_rebalance_active','chain':97477,'wallet':cfg.wallet.lower(),
            'manager':MANAGER_ADDRESS.lower()}


def journal_active_hint(cfg):
    state=load_state(active_base(cfg))
    if state is None:
        legacy=load_state(str(cfg.log)+f'.rebalance-{TOKEN_ID}')
        if legacy and any(x.get('call',{}).get('step')=='mint_new' for x in legacy.get('completed',[])):
            raise Blocked('активная NFT ещё не проверена и не восстановлена из журнала')
        return None
    if state.get('scope')!=active_scope(cfg): raise Blocked('active journal относится к другому кошельку')
    token=state.get('active_token_id')
    if type(token) is not int or token<=0 or token in PROTECTED_NFTS: raise Blocked('неверный active_token_id')
    previous=state.get('previous_token_id')
    if type(previous) is not int or previous<=0 or previous in PROTECTED_NFTS or previous==token:
        raise Blocked('неверный источник active_token_id')
    old_cfg=with_token(cfg,previous); old=load_state(cycle_base(old_cfg))
    done=old.get('completed',[]) if old and old.get('scope')==scope(old_cfg) else []
    if (not done or old.get('pending') or old.get('active_token_id')!=token or
        done[-1].get('call',{}).get('step')!='mint_new' or done[-1].get('new_token_id')!=token or not done[-1].get('hash')):
        raise Blocked('источник active_token_id не совпал с журналом')
    return token


def resolve_cycle(w,cfg,persist=False):
    """Follow verified mint records, recovering a crash between journal writes."""
    from . import rebalance_mint as mint
    index=load_state(active_base(cfg))
    if index:
        current=journal_active_hint(cfg)
        previous=index.get('previous_token_id')
        if type(previous) is not int or previous<=0 or previous in PROTECTED_NFTS: raise Blocked('нет источника active_token_id')
        prev_cfg=with_token(cfg,previous); prev=load_state(cycle_base(prev_cfg))
        if not prev or prev.get('scope')!=scope(prev_cfg) or prev.get('pending'):
            raise Blocked('исходный журнал active_token_id не найден')
        done=prev.get('completed',[])
        if not done or done[-1].get('call',{}).get('step')!='mint_new': raise Blocked('нет подтверждённого mint')
        item=done[-1]
        if (mint.reconcile(w,prev_cfg,item['call'],item['hash'],done)!=current or
            item.get('new_token_id')!=current or prev.get('active_token_id')!=current):
            raise Blocked('active_token_id не совпал с mint')
    else:
        # Migration only: preserve and verify the existing legacy cycle journal.
        current=TOKEN_ID
    seen=set()
    while True:
        if current in seen or current in PROTECTED_NFTS: raise Blocked('неверная цепочка NFT в журнале')
        seen.add(current); c=with_token(cfg,current)
        state=load_state(cycle_base(c))
        if state is None:
            if index is None and not seen-{TOKEN_ID}: raise Blocked('нет проверенного журнала исходной NFT')
            # A newly minted NFT must still be live before a new cycle starts.
            manager=w.eth.contract(address=MANAGER_ADDRESS,abi=MANAGER)
            p=manager.functions.positions(current).call()
            if (manager.functions.ownerOf(current).call().lower()!=cfg.wallet.lower() or
                tuple(p[2:5])!=(USDC,WETH,100) or not p[7]):
                raise Blocked('активная NFT не принадлежит кошельку или ликвидность нулевая')
            return c
        if state.get('scope')!=scope(c): raise Blocked('журнал относится к другой позиции или кошельку')
        done=state.get('completed')
        if not isinstance(done,list): raise Blocked('повреждён журнал цикла')
        minted=[x for x in done if x.get('call',{}).get('step')=='mint_new']
        if not minted: return c
        if len(minted)!=1 or done[-1] is not minted[0] or state.get('pending'):
            raise Blocked('неверное завершение цикла')
        item=minted[0]
        token=mint.reconcile(w,c,item['call'],item['hash'],done)
        if type(token) is not int or token<=0 or token!=item.get('new_token_id') or token in seen or token in PROTECTED_NFTS:
            raise Blocked('новая NFT не совпала с журналом')
        recorded=state.get('active_token_id')
        if recorded is not None and recorded!=token: raise Blocked('active_token_id не совпадает с mint')
        if persist:
            if load_state(cycle_base(with_token(cfg,token))) is None:
                manager=w.eth.contract(address=MANAGER_ADDRESS,abi=MANAGER)
                target=manager.functions.positions(token).call()
                if (manager.functions.ownerOf(token).call().lower()!=cfg.wallet.lower() or
                    tuple(target[2:5])!=(USDC,WETH,100) or not target[7]):
                    raise Blocked('новая NFT не имеет активной ликвидности или не принадлежит кошельку')
            if recorded is None:
                state['active_token_id']=token;save_state(cycle_base(c),state)
            index={'version':1,'scope':active_scope(cfg),'signals':[],'latch':None,'watch':None,'latest':None,
                   'active_token_id':token,'previous_token_id':current}
            save_state(active_base(cfg),index)
        current=token


def read(w,cfg):
    if cfg.chain!=97477 or cfg.manager!=MANAGER_ADDRESS or cfg.usdc!=USDC or cfg.weth!=WETH:
        raise Blocked('неверная конфигурация сети или контрактов')
    check_chain(w,97477)
    block=w.eth.get_block('latest'); n=block.number
    if not -30<=time.time()-block.timestamp<=min(cfg.max_age,120):raise Blocked('устаревший блок')
    call=lambda f:f.call(block_identifier=n)
    m=w.eth.contract(address=MANAGER_ADDRESS,abi=MANAGER)
    p=call(m.functions.positions(current_id(cfg)))
    if (call(m.functions.ownerOf(current_id(cfg))).lower()!=cfg.wallet.lower() or
        tuple(p[2:5])!=(USDC,WETH,100) or call(m.functions.factory())!=FACTORY_ADDRESS):
        raise Blocked('NFT не принадлежит кошельку или параметры позиции изменились')
    f=w.eth.contract(address=FACTORY_ADDRESS,abi=FACTORY)
    pool=w.eth.contract(address=POOL_ADDRESS,abi=POOL)
    if (call(f.functions.getPool(USDC,WETH,100))!=POOL_ADDRESS or
        (call(pool.functions.token0()),call(pool.functions.token1()),call(pool.functions.fee()),call(pool.functions.tickSpacing()))!=(USDC,WETH,100,1)):
        raise Blocked('контракты пула не совпадают')
    slot=call(pool.functions.slot0()); sqrt,tick=slot[:2]
    if not slot[6] or not sqrt_ratio_at_tick(tick)<=sqrt<=sqrt_ratio_at_tick(tick+1):raise Blocked('неверная цена пула')
    ticks_for_pool(p[5],p[6],1)
    tokens=[w.eth.contract(address=x,abi=TOKEN) for x in (USDC,WETH)]
    if [call(t.functions.decimals()) for t in tokens]!=[6,18]:raise Blocked('неверные decimals')
    balances=[call(t.functions.balanceOf(cfg.wallet)) for t in tokens]
    nonce=w.eth.get_transaction_count(cfg.wallet,block_identifier=n)
    if nonce!=w.eth.get_transaction_count(cfg.wallet,'pending'):raise Blocked('у кошелька есть ожидающая транзакция')
    native=w.eth.get_balance(cfg.wallet,block_identifier=n)
    if w.eth.get_block(n).hash!=block.hash or time.time()-block.timestamp>min(cfg.max_age,120):raise Blocked('блок изменился или устарел')
    return {'block':n,'hash':block.hash.hex(),'timestamp':block.timestamp,'base_fee':block.baseFeePerGas,
            'sqrt':sqrt,'tick':tick,'position':p,'balances':balances,'nonce':nonce,'native':native}


def check_mint(w,cfg):
    if current_id(cfg)!=TOKEN_ID: return  # successor mint was verified by resolve_cycle
    receipt=w.eth.get_transaction_receipt(MINT_HASH); tx=w.eth.get_transaction(MINT_HASH)
    if (receipt.status!=1 or tx['to']!=MANAGER_ADDRESS or tx['from'].lower()!=cfg.wallet.lower()
        or tx.get('chainId')!=97477 or minted_token_id(receipt,cfg.wallet)!=TOKEN_ID
        or w.eth.get_block(receipt.blockNumber).hash!=receipt.blockHash
        or w.eth.block_number<receipt.blockNumber+1):raise Blocked('исходный mint NFT не подтверждён')


def make_data(step,cfg,s):
    p=s['position']; c=Web3().eth.contract(abi=ABI)
    if step=='decrease':
        if not p[7]:raise Blocked('ликвидность уже нулевая; проверьте историю вручную')
        a,b=ticks_for_pool(p[5],p[6],1); sq=s['sqrt']**2
        low=math.isqrt(sq*9990//10000);high=math.isqrt(sq*10010//10000)
        mins=(amounts_for_liquidity(high,a,b,p[7])[0],amounts_for_liquidity(low,a,b,p[7])[1])
        params=(current_id(cfg),p[7],*mins,int(time.time())+180)
        expected=amounts_for_liquidity(s['sqrt'],a,b,p[7])
        details={'tokenId':current_id(cfg),'liquidity':p[7],'amount0Min':mins[0],'amount1Min':mins[1],
                 'deadline':params[-1],'priceToleranceBps':10,'sqrtReference':str(s['sqrt']),'sqrtMin':str(low),'sqrtMax':str(high),'expected_raw':list(expected)}
        return c.encode_abi('decreaseLiquidity',args=[params]),details
    if step=='collect':
        if p[7]!=0:raise Blocked('ликвидность не уменьшена до нуля')
        if not (p[10] or p[11]):raise Blocked('нет подтверждённых сумм для сбора')
        params=(current_id(cfg),cfg.wallet,p[10],p[11])
        return c.encode_abi('collect',args=[params]),{'tokenId':current_id(cfg),'recipient':cfg.wallet,'amount0Max':p[10],'amount1Max':p[11]}
    raise Blocked('этап ещё не реализован')


def validate_call(cfg,record,s):
    if (record['chainId']!=97477 or record['from'].lower()!=cfg.wallet.lower() or
        record['to']!=MANAGER_ADDRESS or record['value']!=0 or record['nonce']!=s['nonce']):
        raise Blocked('неверный адрес, сеть или nonce')
    try: fn,args=Web3().eth.contract(abi=ABI).decode_function_input(record['data']);p=args['params']
    except Exception as e:raise Blocked('неверные calldata') from e
    if p['tokenId']!=current_id(cfg) or p['tokenId'] in PROTECTED_NFTS:raise Blocked('неверный tokenId')
    d=record['details'];position=s['position']
    if record['step']=='decrease':
        a,b=ticks_for_pool(position[5],position[6],1)
        low=int(d['sqrtMin']); high=int(d['sqrtMax'])
        ref=int(d['sqrtReference'])
        if (fn.fn_name!='decreaseLiquidity' or not position[7] or p['liquidity']!=position[7] or
            p['deadline']!=d['deadline'] or time.time()>=p['deadline']-30 or d['priceToleranceBps']!=10 or
            low!=math.isqrt(ref*ref*9990//10000) or high!=math.isqrt(ref*ref*10010//10000) or not low<=s['sqrt']<=high or
            p['amount0Min']!=amounts_for_liquidity(high,a,b,position[7])[0] or
            p['amount1Min']!=amounts_for_liquidity(low,a,b,position[7])[1]):
            raise Blocked('цена или параметры decrease изменились; пересчитайте')
    elif record['step']=='collect':
        if (fn.fn_name!='collect' or p['recipient'].lower()!=cfg.wallet.lower() or position[7]!=0 or
            (p['amount0Max'],p['amount1Max'])!=(position[10],position[11]) or not (position[10] or position[11])):
            raise Blocked('суммы collect или получатель изменились')
    else:raise Blocked('неизвестный этап')
    return p


def fee_check(w,s,record):
    limit=record['gasLimit']; maxfee=record['maxFeePerGas']; priority=record['maxPriorityFeePerGas']
    if not 21000<=limit<=800000 or not 0<priority<=maxfee or s['base_fee']>maxfee:raise Blocked('неверные gas-параметры')
    unsigned=b'\x02'+rlp.encode([97477,s['nonce'],priority,maxfee,limit,bytes.fromhex(record['to'][2:]),0,bytes.fromhex(record['data'][2:]),[]])
    l1=oracle(w,'getL1FeeUpperBound(uint256)',len(unsigned),s['block']);op=oracle(w,'getOperatorFee(uint256)',limit,s['block'])
    exposure=limit*maxfee+2*(l1+op)
    if exposure>MAX_STEP_FEE_WEI or s['native']<exposure:raise Blocked('комиссия выше лимита или недостаточно ETH')
    return l1,op


def verify_before_wallet(w,cfg,record):
    if record['step'] in ('approve_swap_token','approve_swap_permit2','swap'):
        from . import rebalance_swap as swap
        from .rebalance_mint import available
        saved=load_state(cycle_base(cfg))
        if not saved or saved.get('scope')!=scope(cfg) or not saved.get('pending') or saved['pending']['call']!=record:raise Blocked('ожидающий вызов не совпадает с журналом')
        return swap.verify(w,cfg,record,saved.get('swap_plan'),available(saved['completed'][:2]))
    if record['step'] in ('approve_mint_usdc','approve_mint_weth','mint_new'):
        from .rebalance_mint import verify
        saved=load_state(cycle_base(cfg))
        if not saved or saved.get('scope')!=scope(cfg) or not saved.get('pending') or saved['pending']['call']!=record:raise Blocked('ожидающий вызов не совпадает с журналом')
        return verify(w,cfg,record,saved['completed'])
    s=read(w,cfg);p=validate_call(cfg,record,s)
    tx={'from':cfg.wallet,'to':MANAGER_ADDRESS,'value':0,'data':record['data']}
    result=decode(['uint256','uint256'],w.eth.call(tx,block_identifier=s['block']))
    if record['step']=='decrease' and (result[0]<p['amount0Min'] or result[1]<p['amount1Min']):raise Blocked('симуляция ниже minima')
    if record['step']=='collect' and (not any(result) or any(result[i]>(p['amount0Max'],p['amount1Max'])[i] for i in (0,1))):raise Blocked('не совпала симуляция collect')
    gas=w.eth.estimate_gas(tx,block_identifier=s['block'])
    if gas>record['gasLimit']:raise Blocked('gas вырос; пересчитайте')
    l1,op=fee_check(w,s,record)
    return {'block':s['block'],'tick':s['tick'],'simulated_amounts_raw':list(result),'balances_raw':s['balances'],
            'gasEstimate':gas,'l1EstimateWei':l1,'operatorEstimateWei':op}


def prepare(w,cfg,step):
    s=read(w,cfg);data,details=make_data(step,cfg,s)
    tx={'from':cfg.wallet,'to':MANAGER_ADDRESS,'value':0,'data':data}
    result=decode(['uint256','uint256'],w.eth.call(tx,block_identifier=s['block']))
    if step=='decrease' and list(result)!=details['expected_raw']:raise Blocked('симуляция decrease не совпала с расчётом')
    if step=='collect' and (not any(result) or any(result[i]>(details['amount0Max'],details['amount1Max'])[i] for i in (0,1))):raise Blocked('симуляция collect не совпала')
    gas=w.eth.estimate_gas(tx,block_identifier=s['block']);limit=(gas*13+9)//10
    priority=1_000_000;maxfee=max(w.eth.gas_price*2,s['base_fee']*2+priority)
    record={'step':step,'chainId':97477,'from':cfg.wallet,'to':MANAGER_ADDRESS,'data':data,'value':0,'nonce':s['nonce'],
            'gasLimit':limit,'maxFeePerGas':maxfee,'maxPriorityFeePerGas':priority,'details':details}
    record['revalidation']=verify_before_wallet(w,cfg,record)
    return record


def quote_after_collect(w,cfg,received):
    if len(received)!=2 or any(type(x) is not int or x<0 for x in received) or not any(received):
        raise Blocked('нет подтверждённых сумм collect')
    s=read(w,cfg)
    if any(s['balances'][i]<received[i] for i in (0,1)):raise Blocked('полученные токены уже перемещены')
    raw=raw_price_from_sqrt(s['sqrt']);lo,hi=range_for_quote(raw,Decimal('0.7'),1,False)
    a,b=ticks_for_pool(lo,hi,1); target=amounts_for_liquidity(s['sqrt'],a,b,10**18)
    tin,tout,amount=estimate_swap(*received,*target,raw)
    result={'range':'временный ±0,7%, не оценка волатильности','ticks':[lo,hi],
            'collected_raw':received,'block':s['block'],'swap':'не нужен' if not amount else 'только предварительный расчёт'}
    if amount:
        q=w.eth.contract(address=QUOTER,abi=QUOTER_ABI)
        if q.functions.factory().call(block_identifier=s['block'])!=FACTORY_ADDRESS:raise Blocked('неверный quoter')
        out=q.functions.quoteExactInputSingle((tin,tout,amount,100,0)).call(block_identifier=s['block'])
        result['quote']={'token_in':tin,'token_out':tout,'amount_in_raw':amount,'amount_out_raw':out[0],
                         'economic_justification':'UNKNOWN: расходы полного цикла не определены'}
    if w.eth.get_block(s['block']).hash.hex()!=s['hash'] or time.time()-s['timestamp']>min(cfg.max_age,120):raise Blocked('котировка устарела')
    return result


def received_from_receipt(r,wallet,expected_max=None,token_id=TOKEN_ID):
    topic=Web3.keccak(text='Collect(uint256,address,uint256,uint256)')
    hits=[x for x in r['logs'] if x['address'].lower()==MANAGER_ADDRESS.lower() and len(x['topics'])==2
          and x['topics'][0]==topic and int.from_bytes(x['topics'][1],'big')==token_id]
    if len(hits)!=1:raise Blocked('нет однозначного события Collect для текущей NFT')
    recipient,a,b=decode(['address','uint256','uint256'],hits[0]['data'])
    if recipient.lower()!=wallet.lower() or not (a or b) or (expected_max is not None and [a,b]!=list(expected_max)):
        raise Blocked('неверный получатель или сумма события Collect')
    transfers=Web3.keccak(text='Transfer(address,address,uint256)'); totals=[0,0]
    for log in r['logs']:
        if (len(log['topics'])!=3 or log['topics'][0]!=transfers or
            bytes(log['topics'][1])[-20:]!=bytes.fromhex(POOL_ADDRESS[2:]) or
            bytes(log['topics'][2])[-20:]!=bytes.fromhex(wallet[2:])):continue
        for i,token in enumerate((USDC,WETH)):
            if log['address'].lower()==token.lower():totals[i]+=int.from_bytes(log['data'],'big')
    # The verified manager emits the requested amount, but V3 core can return
    # slightly less due to rounding. Only the actual token Transfer is credited.
    if not any(totals) or any(totals[i]>[a,b][i] for i in (0,1)):
        raise Blocked('суммы Transfer превышают Collect или отсутствуют')
    return totals


def reconcile(w,cfg,record,h):
    try:
        fn,args=Web3().eth.contract(abi=ABI).decode_function_input(record['data'])
        params=args['params']
    except Exception as e:raise Blocked('повреждены сохранённые calldata') from e
    if (record['chainId']!=97477 or record['from'].lower()!=cfg.wallet.lower() or
        record['to']!=MANAGER_ADDRESS or record['value']!=0 or params['tokenId']!=current_id(cfg) or
        (record['step'],fn.fn_name) not in (('decrease','decreaseLiquidity'),('collect','collect')) or
        (record['step']=='collect' and params['recipient'].lower()!=cfg.wallet.lower())):
        raise Blocked('неверная сохранённая операция или NFT')
    check_chain(w,97477);tx=w.eth.get_transaction(h);r=w.eth.get_transaction_receipt(h)
    if (tx['from'].lower()!=cfg.wallet.lower() or not tx['to'] or tx['to']!=MANAGER_ADDRESS or
        tx.get('chainId')!=97477 or tx['value']!=0 or tx['nonce']!=record['nonce'] or
        Web3.to_hex(tx['input']).lower()!=record['data'].lower()):raise Blocked('транзакция не совпала с ожидавшейся')
    if w.eth.get_block(r['blockNumber']).hash!=r['blockHash'] or w.eth.block_number<r['blockNumber']+1:
        raise Blocked('receipt не имеет двух подтверждений или реорганизован')
    if r['status']!=1:raise FailedTransaction('транзакция подтверждена с ошибкой')
    gas=tx['gas'];unit=tx.get('maxFeePerGas') if tx.get('type')==2 else tx.get('gasPrice')
    l1=r.get('l1Fee')
    if isinstance(l1,str):l1=int(l1,16) if l1.startswith('0x') else int(l1)
    if (not isinstance(gas,int) or not 21000<=gas<=min(800000,2*record['gasLimit']) or
        not isinstance(unit,int) or unit<=0 or not isinstance(l1,int) or l1<0 or
        gas*unit+l1+oracle(w,'getOperatorFee(uint256)',gas,r['blockNumber'])>MAX_STEP_FEE_WEI):
        raise Blocked('фактические gas-параметры вне лимита')
    manager=w.eth.contract(address=MANAGER_ADDRESS,abi=MANAGER)
    p=manager.functions.positions(current_id(cfg)).call(block_identifier=r['blockNumber'])
    latest=manager.functions.positions(current_id(cfg)).call()
    if p[7]!=0 or latest[7]!=0 or manager.functions.ownerOf(current_id(cfg)).call().lower()!=cfg.wallet.lower():
        raise Blocked('ликвидность или владелец позиции после операции не совпадают')
    if record['step']=='collect':
        amounts=received_from_receipt(r,cfg.wallet,(params['amount0Max'],params['amount1Max']),current_id(cfg))
        if (p[10],p[11])!=(0,0) or (latest[10],latest[11])!=(0,0):raise Blocked('сбор не обнулён')
        return amounts
    if record['step']!='decrease':raise Blocked('неизвестный этап')
    return None


def wait_confirmations(w,h,timeout=90):
    """Wait after the hash is durable; a timeout leaves the pending step intact."""
    from web3.exceptions import TimeExhausted
    try:
        receipt=w.eth.wait_for_transaction_receipt(h,timeout=timeout,poll_latency=2)
    except TimeExhausted as exc:
        raise Blocked('receipt ещё не получен; hash сохранён, повторная отправка запрещена') from exc
    end=time.monotonic()+timeout
    while w.eth.block_number<receipt['blockNumber']+1:
        if time.monotonic()>=end:raise Blocked('ожидаются два подтверждения; hash сохранён')
        time.sleep(2)


def check_new_hash(w,h,record,completed):
    """Reject known mismatches before binding a hash to a new pending step."""
    from requests.exceptions import RequestException
    from web3.exceptions import TransactionNotFound, Web3RPCError
    normalized=Web3.to_hex(Web3.to_bytes(hexstr=h)).lower()
    if any(str(item.get('hash','')).lower()==normalized for item in completed):
        raise Blocked('этот hash уже относится к подтверждённому шагу; hash не сохранён в текущий шаг')
    try:
        tx=w.eth.get_transaction(normalized)
    except (TransactionNotFound,Web3RPCError,RequestException,TimeoutError,ConnectionError):
        # The wallet may return a hash before this RPC sees the transaction.
        # Save it durably and retain the strict receipt checks below.
        return
    expected={'chainId':record['chainId'],'from':record['from'],'to':record['to'],
              'nonce':record['nonce'],'value':record['value']}
    for key,value in expected.items():
        actual=tx.get(key)
        if isinstance(value,str):
            if not isinstance(actual,str) or actual.lower()!=value.lower():raise Blocked(f'транзакция не соответствует текущему шагу: поле {key}; hash не сохранён')
        elif actual!=value:raise Blocked(f'транзакция не соответствует текущему шагу: поле {key}; hash не сохранён')
    if Web3.to_hex(tx['input']).lower()!=record['data'].lower():raise Blocked('транзакция не соответствует текущему шагу: calldata; hash не сохранён')


def run(cfg):
    base=str(cfg.log)+'.rebalance';Path(base).parent.mkdir(parents=True,exist_ok=True)
    fd=os.open(base+'.lock',os.O_CREAT|os.O_RDWR,0o600)
    try:
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise Blocked('сценарий уже запущен')
        w=connect(cfg);check_chain(w,97477)
        cfg=resolve_cycle(w,cfg,persist=True);base=cycle_base(cfg)
        state=load_state(base)
        if state is None:state={'version':1,'scope':scope(cfg),'signals':[],'latch':None,'watch':None,'latest':None,'pending':None,'completed':[]}
        if state['scope']!=scope(cfg):raise Blocked('журнал относится к другому кошельку')
        check_mint(w,cfg)
        while True:
            done=state['completed']
            steps=[x['call']['step'] for x in done]
            order=('decrease','collect','approve_swap_token','approve_swap_permit2','swap','approve_mint_usdc','approve_mint_weth','mint_new')
            if steps[:min(2,len(steps))]!=['decrease','collect'][:min(2,len(steps))] or any(x not in order for x in steps) or len(set(steps))!=len(steps) or [order.index(x) for x in steps]!=sorted(order.index(x) for x in steps) or ('mint_new' in steps and steps[-1]!='mint_new'):
                raise Blocked('неверная последовательность журнала')
            from . import rebalance_mint as mint
            from . import rebalance_swap as swap
            for item in done:
                if item['call']['step'] in ('decrease','collect'):
                    value=reconcile(w,cfg,item['call'],item['hash'])
                    if item['call']['step']=='collect' and value!=item['received_raw']:raise Blocked('суммы collect в журнале не совпадают с receipt')
                elif item['call']['step'] in swap.SWAP_STEPS:
                    value=swap.reconcile(w,cfg,item['call'],item['hash'],state.get('swap_plan'),mint.available(done[:2]))
                    if item['call']['step']=='swap' and value!=item.get('swap_received_raw'):raise Blocked('сумма swap в журнале не совпадает с receipt')
                else:
                    value=mint.reconcile(w,cfg,item['call'],item['hash'],done)
                    if item['call']['step']=='mint_new' and value!=item.get('new_token_id'):raise Blocked('новая NFT не совпала с журналом')
            if 'mint_new' in steps:
                print(f"Новая NFT #{done[-1]['new_token_id']} подтверждена; повторный mint заблокирован.",flush=True);return 0
            if state['pending']:
                pending=state['pending']
                if pending.get('hash'):print('Ожидается проверка сохранённого hash:',pending['call']['step'],flush=True)
                else:print('Ожидается подтверждение ранее показанного вызова:',pending['call']['step'],flush=True)
                step=pending['call']['step']
                if (len(done)<2 and step!=('decrease','collect')[len(done)]) or (len(done)>=2 and (step not in order[2:] or step in steps or (steps and order.index(step)<=order.index(steps[-1])))):raise Blocked('pending не соответствует этапу журнала')
                h=pending.get('hash') or input('Вставьте hash; q — выход; cancel — только если точно не подписывали: ').strip()
                if h.lower()=='q':return 0
                if h.lower()=='cancel' and not pending.get('hash'):
                    nonce=pending['call']['nonce']
                    if w.eth.get_transaction_count(cfg.wallet,'latest')!=nonce or w.eth.get_transaction_count(cfg.wallet,'pending')!=nonce:raise Blocked('nonce изменился; проверьте Rabby')
                    state['pending']=None;save_state(base,state);continue
                if not pending.get('hash'):
                    try:valid=len(Web3.to_bytes(hexstr=h))==32
                    except (ValueError,TypeError):valid=False
                    if not valid:raise Blocked('неверный hash')
                    try:check_new_hash(w,h,pending['call'],done)
                    except Blocked as exc:
                        print('HASH_REJECTED: '+str(exc),flush=True)
                        continue
                    pending['hash']=h;save_state(base,state)
                print('HASH_SAVED',flush=True)
                wait_confirmations(w,h)
                try:
                    received=(reconcile(w,cfg,pending['call'],h) if len(done)<2 else swap.reconcile(w,cfg,pending['call'],h,state.get('swap_plan'),mint.available(done[:2])) if step in swap.SWAP_STEPS else mint.reconcile(w,cfg,pending['call'],h,done))
                except FailedTransaction:
                    print('ПОДТВЕРЖДЁННЫЙ ОТКАЗ: после проверки сети можно пересчитать шаг.',flush=True)
                    action=input('retry — проверить и пересчитать, q — выход: ').strip().lower()
                    if action=='q':return 0
                    if action!='retry':raise Blocked('шаг остаётся в pending')
                    fresh=read(w,cfg);old=pending['call'];pos=fresh['position']
                    if fresh['nonce']<=old['nonce']:raise Blocked('nonce отклонённой транзакции не подтверждён')
                    if old['step'] in ('decrease','collect'):
                        fn,args=Web3().eth.contract(abi=ABI).decode_function_input(old['data']);params=args['params']
                        if (old['step']=='decrease' and pos[7]!=params['liquidity']) or (old['step']=='collect' and (pos[7]!=0 or (pos[10],pos[11])!=(params['amount0Max'],params['amount1Max']))):raise Blocked('состояние позиции изменилось; ручная проверка обязательна')
                    elif pos[7]!=0 or any(fresh['balances'][i]<mint.available(done)[i] for i in (0,1)):
                        raise Blocked('состояние позиции или балансы изменились; ручная проверка обязательна')
                    state['pending']=None;save_state(base,state);continue
                item={'call':pending['call'],'hash':h,'received_raw':received if step=='collect' else None}
                if step=='mint_new':
                    latest=w.eth.contract(address=MANAGER_ADDRESS,abi=MANAGER).functions.positions(received).call()
                    if not latest[7]: raise Blocked('новая NFT не имеет активной ликвидности')
                    item['new_token_id']=received
                    state['active_token_id']=received
                if step=='swap':item['swap_received_raw']=received
                state['completed'].append(item);state['pending']=None
                save_state(base,state)
                if step=='mint_new':
                    resolve_cycle(w,cfg,persist=True)
                    print(f'Новая NFT #{received} подтверждена; повторный mint заблокирован.',flush=True)
                    return 0
                continue
            if len(done)<2:
                step=('decrease','collect')[len(done)];call=prepare(w,cfg,step)
            else:
                fresh=read(w,cfg)
                if 'swap' not in steps and not any(x.startswith('approve_mint_') for x in steps):
                    if not state.get('swap_plan'):
                        plan=swap.plan_for(fresh,mint.available(done[:2]))
                        if plan:state['swap_plan']=plan;save_state(base,state)
                    if state.get('swap_plan'):
                        call=swap.prepare(w,cfg,fresh,state['swap_plan'],mint.available(done[:2]),steps)
                    else:call=mint.prepare(w,cfg,fresh,done)
                else:call=mint.prepare(w,cfg,fresh,done)
                if call['step'] in steps:raise Blocked('повтор подтверждённого шага заблокирован')
            state['pending']={'call':call,'hash':None};save_state(base,state)
            print('\nДиапазон ±0,7% временный. Проверьте все поля в кошельке.\n'+json.dumps(call,ensure_ascii=False,indent=2),flush=True)
    finally:os.close(fd)
