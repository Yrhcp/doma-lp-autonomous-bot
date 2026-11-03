"""Bounded mint preparation after collecting the active NFT; no signing or sending."""
import math
import time
from decimal import Decimal
from eth_abi import decode
from web3 import Web3

from .manual_open import (Blocked, MANAGER_ADDRESS, USDC, WETH, MAX_STEP_FEE_WEI,
                          APPROVE_ABI, mint_minima, minted_token_id, oracle)
from .preflight import TOKEN, MINT_ABI
from .contracts import MANAGER
from .rebalance_preview import estimate_swap
from .v3_integer import amounts_for_liquidity, liquidity_for_amounts, ticks_for_pool
from .v3_math import raw_price_from_sqrt, range_for_quote, range_for_offset
from .protected import PROTECTED_NFTS

def target_ticks(raw, width, offset=None):
    return range_for_offset(raw, offset, 1) if offset is not None else range_for_quote(raw, width, 1, False)


def available(done):
    """Use only amounts actually received in the verified Collect event."""
    if len(done)<2 or [x['call']['step'] for x in done[:2]]!=['decrease','collect']:
        raise Blocked('collect не подтверждён')
    value=done[1].get('received_raw')
    if not isinstance(value,list) or len(value)!=2 or any(type(x) is not int or x<0 for x in value) or not any(value):
        raise Blocked('нет подтверждённых сумм collect')
    value=list(value)
    swaps=[x for x in done if x['call']['step']=='swap']
    if len(swaps)>1:raise Blocked('повторный swap заблокирован')
    if swaps:
        item=swaps[0];d=item['call']['details'];received=item.get('swap_received_raw')
        if (d.get('tokenIn'),d.get('tokenOut')) not in ((USDC,WETH),(WETH,USDC)) or type(received) is not int or received<=0 or type(d.get('amountIn')) is not int:raise Blocked('неверные результаты swap')
        i=0 if d['tokenIn']==USDC else 1;j=1-i
        if not 0<d['amountIn']<=value[i]:raise Blocked('swap превысил собранные токены')
        value[i]-=d['amountIn'];value[j]+=received
    return value


def check_no_swap(s,budget,range_percent=Decimal('0.7'),offset=None):
    raw=raw_price_from_sqrt(s['sqrt']);lo,hi=target_ticks(raw,range_percent,offset)
    a,b=ticks_for_pool(lo,hi,1)
    target=amounts_for_liquidity(s['sqrt'],a,b,10**18)
    tin,tout,amount=estimate_swap(*budget,*target,raw)
    # Up to 0.01 USDC.e of indicative input is left as dust. Larger
    # imbalances need the missing swap implementation, not a fake quote.
    value=Decimal(amount) if tin==USDC else Decimal(amount)/raw if tin==WETH else Decimal(0)
    if value>10000:raise Blocked('требуется swap: подготовка ограниченного Permit2 и router ещё не реализована')
    return lo,hi


def mint_data(cfg,s,budget,after_swap=False):
    width=getattr(cfg,'range_percent',Decimal('0.7'))
    offset=getattr(cfg,'range_quote_offset',None)
    lo,hi=(target_ticks(raw_price_from_sqrt(s['sqrt']),width,offset) if after_swap else check_no_swap(s,budget,width,offset));a,b=ticks_for_pool(lo,hi,1)
    if any(s['balances'][i]<budget[i] for i in (0,1)):raise Blocked('полученные средства уже перемещены')
    liq=liquidity_for_amounts(s['sqrt'],a,b,*budget)
    used=amounts_for_liquidity(s['sqrt'],a,b,liq,True)
    if not liq or any(used[i]>budget[i] for i in (0,1)):raise Blocked('неверные суммы mint')
    min0,min1,low,high=mint_minima(s['sqrt'],lo,hi,10,*budget)
    deadline=int(time.time())+180
    params=(USDC,WETH,100,lo,hi,*budget,min0,min1,cfg.wallet,deadline)
    data=Web3().eth.contract(abi=MINT_ABI).encode_abi('mint',args=[params])
    return data,{'tickLower':lo,'tickUpper':hi,'amount0Desired':budget[0],'amount1Desired':budget[1],
      'amount0Min':min0,'amount1Min':min1,'recipient':cfg.wallet,'deadline':deadline,
      'sqrtReference':str(s['sqrt']),'sqrtMin':str(low),'sqrtMax':str(high),'liquidity':liq,
      'expected_raw':list(used),'range':f'±{offset} USDC.e' if offset is not None else f'временный ±{width}%, не оценка волатильности'}


def next_step(w,cfg,s,done):
    budget=available(done)
    if any(s['balances'][i]<budget[i] for i in (0,1)):raise Blocked('полученные токены уже перемещены')
    steps=[x['call']['step'] for x in done]
    if 'swap' not in steps:check_no_swap(s,budget,getattr(cfg,'range_percent',Decimal('0.7')),getattr(cfg,'range_quote_offset',None))
    for i,token in enumerate((USDC,WETH)):
        allowance=w.eth.contract(address=token,abi=TOKEN).functions.allowance(cfg.wallet,MANAGER_ADDRESS).call(block_identifier=s['block'])
        name=('approve_mint_usdc','approve_mint_weth')[i]
        if name in steps:
            if allowance!=budget[i]:raise Blocked('подтверждённое разрешение mint изменилось; ручная проверка')
        elif allowance!=budget[i]:
            c=Web3().eth.contract(abi=APPROVE_ABI);data=c.encode_abi('approve',args=[MANAGER_ADDRESS,budget[i]])
            return name,token,data,{'spender':MANAGER_ADDRESS,'amount_raw':budget[i],'budget_raw':budget}
    data,details=mint_data(cfg,s,budget,after_swap='swap' in steps)
    return 'mint_new',MANAGER_ADDRESS,data,details


def check_record(cfg,s,record,budget,after_swap=False):
    if (record['chainId']!=97477 or record['from'].lower()!=cfg.wallet.lower() or record['value']!=0 or record['nonce']!=s['nonce']):raise Blocked('неверный sender, сеть, value или nonce')
    if any(s['balances'][i]<budget[i] for i in (0,1)):raise Blocked('недостаточно полученных токенов')
    if not after_swap:check_no_swap(s,budget,getattr(cfg,'range_percent',Decimal('0.7')),getattr(cfg,'range_quote_offset',None))
    step=record['step']
    if step in ('approve_mint_usdc','approve_mint_weth'):
        i=0 if step=='approve_mint_usdc' else 1;token=(USDC,WETH)[i]
        if record['to']!=token:raise Blocked('неверный токен approve')
        c=Web3().eth.contract(abi=APPROVE_ABI);fn,args=c.decode_function_input(record['data'])
        if fn.fn_name!='approve' or args!={'spender':MANAGER_ADDRESS,'value':budget[i]}:raise Blocked('неверное разрешение')
        return args
    if step!='mint_new' or record['to']!=MANAGER_ADDRESS:raise Blocked('неверный этап')
    fn,args=Web3().eth.contract(abi=MINT_ABI).decode_function_input(record['data']);p=args['params'];d=record['details']
    if (fn.fn_name!='mint' or (p['token0'],p['token1'],p['fee'],p['recipient'])!=(USDC,WETH,100,cfg.wallet) or
        (p['amount0Desired'],p['amount1Desired'])!=tuple(budget) or p['deadline']!=d['deadline'] or time.time()>=p['deadline']-30):raise Blocked('неверные параметры mint')
    lo,hi=ticks_for_pool(p['tickLower'],p['tickUpper'],1)
    ref=int(d['sqrtReference']);low=int(d['sqrtMin']);high=int(d['sqrtMax'])
    expected=mint_minima(ref,p['tickLower'],p['tickUpper'],10,*budget)
    expected_ticks=target_ticks(raw_price_from_sqrt(ref),getattr(cfg,'range_percent',Decimal('0.7')),getattr(cfg,'range_quote_offset',None))
    if ((p['tickLower'],p['tickUpper'])!=expected_ticks or expected!=(p['amount0Min'],p['amount1Min'],low,high) or not low<=s['sqrt']<=high):raise Blocked('диапазон, цена или minima изменились; пересчитайте')
    return p


def prepare(w,cfg,s,done):
    from .rebalance_manual import read, fee_check
    budget=available(done);step,target,data,details=next_step(w,cfg,s,done)
    tx={'from':cfg.wallet,'to':target,'value':0,'data':data}
    gas=w.eth.estimate_gas(tx,block_identifier=s['block']);limit=(gas*13+9)//10
    priority=1_000_000;maxfee=max(w.eth.gas_price*2,s['base_fee']*2+priority)
    record={'step':step,'chainId':97477,'from':cfg.wallet,'to':target,'data':data,'value':0,'nonce':s['nonce'],
            'gasLimit':limit,'maxFeePerGas':maxfee,'maxPriorityFeePerGas':priority,'details':details}
    record['revalidation']=verify(w,cfg,record,done)
    return record


def verify(w,cfg,record,done):
    from .rebalance_manual import read,fee_check,current_id
    s=read(w,cfg);budget=available(done);p=check_record(cfg,s,record,budget,after_swap=any(x['call']['step']=='swap' for x in done))
    tx={'from':cfg.wallet,'to':record['to'],'value':0,'data':record['data']}
    if record['step']=='mint_new':
        for i,token in enumerate((USDC,WETH)):
            allowance=w.eth.contract(address=token,abi=TOKEN).functions.allowance(cfg.wallet,MANAGER_ADDRESS).call(block_identifier=s['block'])
            if allowance!=budget[i]:raise Blocked('разрешение менеджеру не соответствует ограниченной сумме')
        result=decode(['uint256','uint128','uint256','uint256'],w.eth.call(tx,block_identifier=s['block']))
        if ((result[0] in PROTECTED_NFTS or result[0]==current_id(cfg)) or not result[1] or not p['amount0Min']<=result[2]<=budget[0] or not p['amount1Min']<=result[3]<=budget[1]):
            raise Blocked('симуляция mint вне лимитов')
        amounts=list(result[2:])
    else:
        result=decode(['bool'],w.eth.call(tx,block_identifier=s['block']))
        if not result[0]:raise Blocked('approve отклонён')
        amounts=[]
    estimate=w.eth.estimate_gas(tx,block_identifier=s['block'])
    if estimate>record['gasLimit']:raise Blocked('gas вырос; пересчитайте')
    l1,op=fee_check(w,s,record)
    return {'block':s['block'],'tick':s['tick'],'simulated_amounts_raw':amounts,'gasEstimate':estimate,'l1EstimateWei':l1,'operatorEstimateWei':op}


def reconcile(w,cfg,record,h,done):
    from .rebalance_manual import FailedTransaction,current_id
    budget=available(done);step=record['step']
    if record['chainId']!=97477 or record['from'].lower()!=cfg.wallet.lower() or record['value']!=0:raise Blocked('неверные параметры журнала')
    if step in ('approve_mint_usdc','approve_mint_weth'):
        i=0 if step=='approve_mint_usdc' else 1;token=(USDC,WETH)[i]
        fn,args=Web3().eth.contract(abi=APPROVE_ABI).decode_function_input(record['data'])
        if record['to']!=token or fn.fn_name!='approve' or args!={'spender':MANAGER_ADDRESS,'value':budget[i]}:raise Blocked('неверное разрешение в журнале')
    elif step=='mint_new':
        fn,args=Web3().eth.contract(abi=MINT_ABI).decode_function_input(record['data']);p=args['params']
        if (record['to']!=MANAGER_ADDRESS or fn.fn_name!='mint' or (p['token0'],p['token1'],p['fee'],p['recipient'])!=(USDC,WETH,100,cfg.wallet) or
            (p['amount0Desired'],p['amount1Desired'])!=tuple(budget) or not (0<p['amount0Min']<=budget[0] and 0<p['amount1Min']<=budget[1])):raise Blocked('неверный mint в журнале')
        ticks_for_pool(p['tickLower'],p['tickUpper'],1)
    else:raise Blocked('неизвестный этап')
    tx=w.eth.get_transaction(h);r=w.eth.get_transaction_receipt(h)
    if (tx['from'].lower()!=cfg.wallet.lower() or tx.get('chainId')!=97477 or tx['to']!=record['to'] or tx['value']!=0 or
        tx['nonce']!=record['nonce'] or Web3.to_hex(tx['input']).lower()!=record['data'].lower()):raise Blocked('транзакция не соответствует журналу')
    if w.eth.get_block(r['blockNumber']).hash!=r['blockHash'] or w.eth.block_number<r['blockNumber']+1:raise Blocked('receipt не имеет двух подтверждений')
    if r['status']!=1:raise FailedTransaction('транзакция подтверждена с ошибкой')
    gas=tx['gas'];unit=tx.get('maxFeePerGas') if tx.get('type')==2 else tx.get('gasPrice');l1=r.get('l1Fee')
    if isinstance(l1,str):l1=int(l1,16) if l1.startswith('0x') else int(l1)
    if (not isinstance(gas,int) or not 21000<=gas<=min(800000,2*record['gasLimit']) or
        not isinstance(unit,int) or unit<=0 or not isinstance(l1,int) or l1<0 or
        gas*unit+l1+oracle(w,'getOperatorFee(uint256)',gas,r['blockNumber'])>MAX_STEP_FEE_WEI):raise Blocked('gas-параметры вне лимита')
    if step.startswith('approve_'):
        allowance=w.eth.contract(address=token,abi=TOKEN).functions.allowance(cfg.wallet,MANAGER_ADDRESS).call(block_identifier=r['blockNumber'])
        if allowance!=budget[i]:raise Blocked('разрешение не подтверждено в блоке receipt')
        return None
    token_id=minted_token_id(r,cfg.wallet)
    if token_id in PROTECTED_NFTS or token_id==current_id(cfg):raise Blocked('неверный новый NFT')
    manager=w.eth.contract(address=MANAGER_ADDRESS,abi=MANAGER)
    position=manager.functions.positions(token_id).call(block_identifier=r['blockNumber'])
    from .contracts import FACTORY
    from .manual_open import FACTORY_ADDRESS, POOL_ADDRESS
    factory=w.eth.contract(address=FACTORY_ADDRESS,abi=FACTORY)
    if (manager.functions.factory().call(block_identifier=r['blockNumber'])!=FACTORY_ADDRESS or
        factory.functions.getPool(USDC,WETH,100).call(block_identifier=r['blockNumber'])!=POOL_ADDRESS):
        raise Blocked('пул новой NFT не совпал с factory')
    if (manager.functions.ownerOf(token_id).call(block_identifier=r['blockNumber']).lower()!=cfg.wallet.lower() or
        manager.functions.ownerOf(token_id).call().lower()!=cfg.wallet.lower() or
        tuple(position[2:5])!=(USDC,WETH,100) or (position[5],position[6])!=(p['tickLower'],p['tickUpper']) or not position[7]):
        raise Blocked('новая NFT не совпала с mint')
    return token_id
