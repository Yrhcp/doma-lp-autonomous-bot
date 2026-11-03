"""One manual Doma mint, no signing or sending.

Each displayed call is simulated and journalled before display; external wallet
transactions are reconciled by hash and the next call is prepared afresh.
"""
from .protected import PROTECTED_NFTS
import fcntl
import json
import math
import os
import time
from decimal import Decimal as D
from pathlib import Path
import rlp
from eth_abi import encode,decode
from web3 import Web3
from web3.exceptions import ContractLogicError
from .preflight import (MANAGER_ADDRESS, FACTORY_ADDRESS, WETH, USDC, MINT_ABI, TOKEN)
from .contracts import MANAGER, FACTORY, POOL
from .rpc import connect, check_chain
from .storage import load_state, save_state
from .v3_math import raw_price_from_sqrt, quote_price, range_for_quote, sqrt_ratio_at_tick
from .v3_integer import ticks_for_pool, liquidity_for_amounts, amounts_for_liquidity

POOL_ADDRESS=Web3.to_checksum_address('0xd604c96e51DF995bb46FAb0E3FC1b18d985AA8f5')
ORACLE=Web3.to_checksum_address('0x420000000000000000000000000000000000000F')
CAP0=2_500_000
CAP1=1_100_000_000_000_000
MAX_STEP_FEE_WEI=100_000_000_000_000  # 0.0001 ETH, current estimate plus L1 buffer
APPROVE_ABI=[{'type':'function','name':'approve','stateMutability':'nonpayable','inputs':[{'name':'spender','type':'address'},{'name':'value','type':'uint256'}],'outputs':[{'name':'','type':'bool'}]}]

class Blocked(ValueError): pass

def state_scope(cfg):return {'purpose':'manual_single_mint','chain':97477,'wallet':cfg.wallet.lower(),'manager':MANAGER_ADDRESS.lower()}

def read_snapshot(w,cfg):
    if cfg.chain!=97477 or cfg.manager!=MANAGER_ADDRESS or cfg.usdc!=USDC or cfg.weth!=WETH:raise Blocked('wrong chain or configured contract')
    check_chain(w,97477);b=w.eth.get_block('latest');n=b.number
    if not -30<=time.time()-b.timestamp<=min(cfg.max_age,120):raise Blocked('RPC block stale or from future')
    for a in (MANAGER_ADDRESS,FACTORY_ADDRESS,POOL_ADDRESS,USDC,WETH,ORACLE):
        if not w.eth.get_code(a,block_identifier=n):raise Blocked('missing contract code')
    call=lambda f:f.call(block_identifier=n)
    mgr=w.eth.contract(address=MANAGER_ADDRESS,abi=MANAGER);fac=w.eth.contract(address=FACTORY_ADDRESS,abi=FACTORY)
    if call(mgr.functions.factory())!=FACTORY_ADDRESS or call(fac.functions.getPool(USDC,WETH,100))!=POOL_ADDRESS:raise Blocked('factory or pool mismatch')
    pool=w.eth.contract(address=POOL_ADDRESS,abi=POOL)
    if (call(pool.functions.token0()),call(pool.functions.token1()),call(pool.functions.fee()),call(pool.functions.tickSpacing()))!=(USDC,WETH,100,1):raise Blocked('pool parameters mismatch')
    slot=call(pool.functions.slot0());sqrt,tick=slot[:2]
    if not slot[6] or not sqrt_ratio_at_tick(tick)<=sqrt<=sqrt_ratio_at_tick(tick+1):raise Blocked('invalid pool price')
    ts=[w.eth.contract(address=a,abi=TOKEN) for a in (USDC,WETH)]
    if [call(t.functions.decimals()) for t in ts]!=[6,18]:raise Blocked('token decimals mismatch')
    balances=[call(t.functions.balanceOf(cfg.wallet)) for t in ts];allowances=[call(t.functions.allowance(cfg.wallet,MANAGER_ADDRESS)) for t in ts]
    if balances[0]<CAP0 or balances[1]<CAP1:raise Blocked('insufficient token balance')
    native=w.eth.get_balance(cfg.wallet,block_identifier=n);nonce=w.eth.get_transaction_count(cfg.wallet,block_identifier=n)
    if nonce!=w.eth.get_transaction_count(cfg.wallet,'pending'):raise Blocked('wallet has pending or unknown transaction')
    if b.hash!=w.eth.get_block(n).hash or time.time()-b.timestamp>min(cfg.max_age,120):raise Blocked('snapshot stale or reorganized')
    raw=raw_price_from_sqrt(sqrt);price=quote_price(raw,6,18,False)
    return {'block':n,'hash':b.hash.hex(),'timestamp':b.timestamp,'base_fee':b.baseFeePerGas,'sqrt':sqrt,'tick':tick,'price':price,'raw':raw,'balances':balances,'allowances':allowances,'native':native,'nonce':nonce}

def encode_approval(amount):
    if amount not in (CAP0,CAP1):raise Blocked('approval amount outside cap')
    c=Web3().eth.contract(abi=APPROVE_ABI);data=c.encode_abi('approve',args=[MANAGER_ADDRESS,amount]);_,x=c.decode_function_input(data)
    if x!={'spender':MANAGER_ADDRESS,'value':amount}:raise Blocked('approval encoding mismatch')
    return data

def mint_minima(sqrt,lower,upper,slippage_bps=10,amount0=CAP0,amount1=CAP1):
    """Mint minima at adverse price endpoints; 10 bps = 0.1% price tolerance."""
    if not isinstance(slippage_bps,int) or not 0<slippage_bps<=100:raise Blocked('slippage exceeds 1%')
    lo,hi=ticks_for_pool(lower,upper,1);square=sqrt*sqrt
    low=math.isqrt(square*(10000-slippage_bps)//10000)
    high=math.isqrt(square*(10000+slippage_bps)//10000)
    l_high=liquidity_for_amounts(high,lo,hi,amount0,amount1)
    l_low=liquidity_for_amounts(low,lo,hi,amount0,amount1)
    min0=amounts_for_liquidity(high,lo,hi,l_high,True)[0]
    min1=amounts_for_liquidity(low,lo,hi,l_low,True)[1]
    if not min0 or not min1:raise Blocked('slippage range would remove a token minimum')
    return min0,min1,low,high

def encode_mint(cfg,s):
    lo,hi=range_for_quote(s['raw'],D('0.7'),1,False);a,b=ticks_for_pool(lo,hi,1)
    liquidity=liquidity_for_amounts(s['sqrt'],a,b,CAP0,CAP1);used=amounts_for_liquidity(s['sqrt'],a,b,liquidity,True)
    min0,min1,sqrt_low,sqrt_high=mint_minima(s['sqrt'],lo,hi);mins=[min0,min1];deadline=int(time.time())+180
    if not liquidity or not all(mins) or any(used[i]>[CAP0,CAP1][i] for i in range(2)):raise Blocked('invalid liquidity, token minima or cap')
    params=(USDC,WETH,100,lo,hi,CAP0,CAP1,*mins,cfg.wallet,deadline)
    c=Web3().eth.contract(abi=MINT_ABI);data=c.encode_abi('mint',args=[params]);_,x=c.decode_function_input(data)
    names=[v['name'] for v in MINT_ABI[0]['inputs'][0]['components']]
    if tuple(x['params'][name] for name in names)!=params:raise Blocked('mint encoding mismatch')
    return data,{'tickLower':lo,'tickUpper':hi,'amount0Desired':CAP0,'amount1Desired':CAP1,'amount0Min':mins[0],'amount1Min':mins[1],'recipient':cfg.wallet,'deadline':deadline,'expected_raw':list(used),'liquidity':liquidity,'slippage_price_bps':10,'sqrtPriceMinX96':str(sqrt_low),'sqrtPriceMaxX96':str(sqrt_high)}

def oracle(w,method,value,block):
    data=Web3.keccak(text=method)[:4]+encode(['uint256'],[value])
    return decode(['uint256'],w.eth.call({'to':ORACLE,'data':data},block_identifier=block))[0]

def _prepare_once(w,cfg):
    s=read_snapshot(w,cfg)
    if s['allowances'][0]!=CAP0:step='approve_usdc';target=USDC;data=encode_approval(CAP0);details={'spender':MANAGER_ADDRESS,'amount_raw':CAP0}
    elif s['allowances'][1]!=CAP1:step='approve_weth';target=WETH;data=encode_approval(CAP1);details={'spender':MANAGER_ADDRESS,'amount_raw':CAP1}
    else:step='mint';target=MANAGER_ADDRESS;data,details=encode_mint(cfg,s)
    tx={'from':cfg.wallet,'to':target,'data':data,'value':0}
    result=w.eth.call(tx,block_identifier=s['block'])
    if step.startswith('approve'):
        if not decode(['bool'],result)[0]:raise Blocked('approval simulation returned false')
    else:
        token,liq,a0,a1=decode(['uint256','uint128','uint256','uint256'],result)
        if token in PROTECTED_NFTS or liq!=details['liquidity'] or [a0,a1]!=details['expected_raw']:raise Blocked('mint simulation mismatch')
    estimate=w.eth.estimate_gas(tx,block_identifier=s['block']);limit=(estimate*13+9)//10
    if not 21000<=limit<=800000:raise Blocked('gas estimate outside allowed limit')
    price=w.eth.gas_price;priority=1_000_000;maxfee=max(price*2,s['base_fee']*2+priority)
    unsigned=b'\x02'+rlp.encode([97477,s['nonce'],priority,maxfee,limit,bytes.fromhex(target[2:]),0,bytes.fromhex(data[2:]),[]])
    l1=oracle(w,'getL1FeeUpperBound(uint256)',len(unsigned),s['block']);operator=oracle(w,'getOperatorFee(uint256)',limit,s['block'])
    if s['native']<limit*maxfee+2*(l1+operator):raise Blocked('insufficient ETH for estimated fees and buffer')
    # Verify exact bytes again at a fresh pinned block. A changed price/minimum reverts.
    fresh=read_snapshot(w,cfg)
    if fresh['nonce']!=s['nonce'] or fresh['balances']!=s['balances'] or fresh['allowances']!=s['allowances'] or fresh['base_fee']>maxfee:raise Blocked('state or gas changed; rerun')
    final=w.eth.call(tx,block_identifier=fresh['block'])
    if step.startswith('approve'):
        if not decode(['bool'],final)[0]:raise Blocked('fresh approval simulation returned false')
    else:
        token,liq,a0,a1=decode(['uint256','uint128','uint256','uint256'],final)
        if token in PROTECTED_NFTS or not liq or not details['amount0Min']<=a0<=CAP0 or not details['amount1Min']<=a1<=CAP1:raise Blocked('fresh mint simulation outside limits')
        details['fresh_simulated_amounts_raw']=[a0,a1]
    if w.eth.estimate_gas(tx,block_identifier=fresh['block'])>limit:raise Blocked('fresh gas exceeds limit')
    l1=oracle(w,'getL1FeeUpperBound(uint256)',len(unsigned),fresh['block']);operator=oracle(w,'getOperatorFee(uint256)',limit,fresh['block'])
    if limit*maxfee+2*(l1+operator)>MAX_STEP_FEE_WEI:raise Blocked('estimated fee with buffer exceeds 0.0001 ETH per step')
    if fresh['native']<limit*maxfee+2*(l1+operator):raise Blocked('insufficient ETH after refresh')
    if step=='mint' and (time.time()>=details['deadline']-60 or not int(details['sqrtPriceMinX96'])<=fresh['sqrt']<=int(details['sqrtPriceMaxX96'])):raise Blocked('deadline or price changed; recalculate mint')
    return {'step':step,'chainId':97477,'from':cfg.wallet,'to':target,'data':data,'value':0,'nonce':s['nonce'],'gasLimit':limit,'maxFeePerGas':maxfee,'maxPriorityFeePerGas':priority,'snapshotBlock':fresh['block'],'snapshotHash':fresh['hash'],'tick':fresh['tick'],'sqrtPriceX96':str(fresh['sqrt']),'details':details,'simulation':'eth_call at snapshot and fresh block succeeded','gasEstimate':estimate,'executionMaxWei':limit*maxfee,'l1EstimateWei':l1,'operatorEstimateWei':operator,'estimatedTotalAtSnapshotWei':limit*maxfee+l1+operator,'priceUSDCperWETH':str(fresh['price']),'balances_raw':fresh['balances'],'allowances_raw':fresh['allowances'],'native_wei':fresh['native']}

def prepare(w,cfg):
    for attempt in range(2):
        try:return _prepare_once(w,cfg)
        except (ContractLogicError,Blocked) as exc:
            transient=('Price slippage check' in str(exc) or 'price changed; recalculate mint' in str(exc))
            if not transient:raise
            if attempt==0:print('Цена изменилась; пересчитываю mint на свежем блоке…',flush=True)
    raise Blocked('Цена изменилась во время симуляции mint; нажмите «Пересчитать», транзакция не подготовлена')

def verify_before_wallet(w,cfg,record):
    """Recheck the exact journalled bytes immediately before a wallet prompt."""
    s=read_snapshot(w,cfg);step=record['step']
    if record['chainId']!=97477 or record['from'].lower()!=cfg.wallet.lower() or record['value']!=0 or record['nonce']!=s['nonce']:
        raise Blocked('network, sender, value or nonce changed')
    if s['balances']!=record['balances_raw'] or s['allowances']!=record['allowances_raw']:
        raise Blocked('balances or allowances changed; regenerate')
    target={'approve_usdc':USDC,'approve_weth':WETH,'mint':MANAGER_ADDRESS}.get(step)
    if target is None or record['to']!=target:raise Blocked('transaction target mismatch')
    if step.startswith('approve'):
        expected=encode_approval(CAP0 if step=='approve_usdc' else CAP1)
        if record['data'].lower()!=expected.lower():raise Blocked('approval calldata mismatch')
    else:
        if s['allowances']!=[CAP0,CAP1]:raise Blocked('mint allowances changed')
        contract=Web3().eth.contract(abi=MINT_ABI)
        try:fn,args=contract.decode_function_input(record['data']);p=args['params']
        except Exception as exc:raise Blocked('invalid mint calldata') from exc
        if fn.fn_name!='mint' or (p['token0'],p['token1'],p['fee'],p['recipient'])!=(USDC,WETH,100,cfg.wallet) or (p['amount0Desired'],p['amount1Desired'])!=(CAP0,CAP1):
            raise Blocked('mint parameters mismatch')
        ticks_for_pool(p['tickLower'],p['tickUpper'],1)
        if not (0<p['amount0Min']<=CAP0 and 0<p['amount1Min']<=CAP1) or time.time()>=p['deadline']-30:
            raise Blocked('mint minima invalid or deadline too close')
        details=record.get('details',{})
        if details.get('slippage_price_bps')!=10 or not int(details.get('sqrtPriceMinX96','0'))<=s['sqrt']<=int(details.get('sqrtPriceMaxX96','0')):
            raise Blocked('Цена изменилась; нажмите «Пересчитать»')
    if not 21000<=record['gasLimit']<=800000 or not 0<record['maxPriorityFeePerGas']<=record['maxFeePerGas'] or s['base_fee']>record['maxFeePerGas']:
        raise Blocked('gas parameters invalid or stale')
    tx={'from':cfg.wallet,'to':target,'data':record['data'],'value':0}
    result=w.eth.call(tx,block_identifier=s['block'])
    if step.startswith('approve'):
        if not decode(['bool'],result)[0]:raise Blocked('approval simulation failed')
    else:
        token,liquidity,a0,a1=decode(['uint256','uint128','uint256','uint256'],result)
        if token in PROTECTED_NFTS or not liquidity or not p['amount0Min']<=a0<=CAP0 or not p['amount1Min']<=a1<=CAP1:raise Blocked('mint simulation failed limits')
    gas_estimate=w.eth.estimate_gas(tx,block_identifier=s['block'])
    if gas_estimate>record['gasLimit']:raise Blocked('gas estimate exceeds limit')
    unsigned=b'\x02'+rlp.encode([97477,s['nonce'],record['maxPriorityFeePerGas'],record['maxFeePerGas'],record['gasLimit'],bytes.fromhex(target[2:]),0,bytes.fromhex(record['data'][2:]),[]])
    l1=oracle(w,'getL1FeeUpperBound(uint256)',len(unsigned),s['block']);operator=oracle(w,'getOperatorFee(uint256)',record['gasLimit'],s['block'])
    estimate=record['gasLimit']*record['maxFeePerGas']+2*(l1+operator)
    if estimate>MAX_STEP_FEE_WEI or s['native']<estimate:raise Blocked('fee estimate or ETH balance outside limit')
    if time.time()-s['timestamp']>min(cfg.max_age,120) or w.eth.get_block(s['block']).hash.hex()!=s['hash']:
        raise Blocked('revalidation snapshot became stale')
    return {'block':s['block'],'tick':s['tick'],'sqrtPriceX96':str(s['sqrt']),'gasEstimate':gas_estimate,'l1EstimateWei':l1,'operatorEstimateWei':operator,'priceUSDCperWETH':str(s['price']),'balances_raw':s['balances'],'allowances_raw':s['allowances'],'native_wei':s['native']}

def minted_token_id(receipt,wallet):
    topic=Web3.keccak(text='Transfer(address,address,uint256)');recipient=bytes.fromhex(wallet[2:].lower()).rjust(32,b'\0')
    hits=[l for l in receipt['logs'] if l['address'].lower()==MANAGER_ADDRESS.lower() and len(l['topics'])==4 and l['topics'][0]==topic and int.from_bytes(l['topics'][1],'big')==0 and bytes(l['topics'][2])==recipient]
    if len(hits)!=1 or int.from_bytes(hits[0]['topics'][3],'big') in PROTECTED_NFTS:raise Blocked('mint NFT event missing or invalid')
    return int.from_bytes(hits[0]['topics'][3],'big')

def reconcile(w,cfg,record,tx_hash,*,check_approval_state=False):
    h=Web3.to_hex(Web3.to_bytes(hexstr=tx_hash))
    if len(h)!=66:raise Blocked('invalid transaction hash')
    tx=w.eth.get_transaction(h);r=w.eth.get_transaction_receipt(h)
    if record['chainId']!=97477 or record['from'].lower()!=cfg.wallet.lower() or record['value']!=0:raise Blocked('journal chain, sender or value mismatch')
    if tx['from'].lower()!=cfg.wallet.lower() or not tx['to'] or tx['to'].lower()!=record['to'].lower() or Web3.to_hex(tx['input']).lower()!=record['data'].lower() or tx['value']!=0 or tx['nonce']!=record['nonce'] or tx.get('chainId')!=97477:raise Blocked('transaction does not match displayed call')
    step=record['step']
    if step in ('approve_usdc','approve_weth'):
        target,amount=(USDC,CAP0) if step=='approve_usdc' else (WETH,CAP1)
        if record['to']!=target or record['data'].lower()!=encode_approval(amount).lower():raise Blocked('approval token, spender or amount mismatch')
    elif step=='mint':
        try:fn,args=Web3().eth.contract(abi=MINT_ABI).decode_function_input(record['data']);p=args['params']
        except Exception as exc:raise Blocked('invalid mint calldata') from exc
        if record['to']!=MANAGER_ADDRESS or fn.fn_name!='mint' or (p['token0'],p['token1'],p['fee'],p['recipient'])!=(USDC,WETH,100,cfg.wallet) or (p['amount0Desired'],p['amount1Desired'])!=(CAP0,CAP1):raise Blocked('mint parameters mismatch')
    else:raise Blocked('unknown step')
    if r['status']!=1:raise Blocked('transaction failed; inspect manually')
    if w.eth.get_block(r['blockNumber']).hash!=r['blockHash'] or w.eth.block_number<r['blockNumber']+1:raise Blocked('receipt not canonical or lacks 2 confirmations')
    # Rabby can adjust gas and use legacy transactions. Restrict the maximum
    # execution exposure and add the actual separately charged L1 fee.
    gas=tx['gas'];unit_price=tx.get('maxFeePerGas') if tx.get('type')==2 else tx.get('gasPrice')
    if not isinstance(gas,int) or not 21000<=gas<=min(800000,2*record['gasLimit']) or not isinstance(unit_price,int) or unit_price<=0:
        raise Blocked('gas parameters outside safe limits')
    l1=r.get('l1Fee')
    if isinstance(l1,str):
        try:l1=int(l1,16) if l1.startswith('0x') else int(l1)
        except ValueError:raise Blocked('invalid L1 fee')
    if not isinstance(l1,int) or l1<0:raise Blocked('receipt has no valid L1 fee')
    operator=oracle(w,'getOperatorFee(uint256)',gas,r['blockNumber'])
    if gas*unit_price+l1+operator>MAX_STEP_FEE_WEI:raise Blocked('gas plus L1/operator fees exceed 0.0001 ETH')
    if step in ('approve_usdc','approve_weth') and check_approval_state:
        token=w.eth.contract(address=target,abi=TOKEN)
        if token.functions.allowance(cfg.wallet,MANAGER_ADDRESS).call(block_identifier=r['blockNumber'])!=amount or token.functions.allowance(cfg.wallet,MANAGER_ADDRESS).call()!=amount:
            raise Blocked('approval allowance not confirmed at receipt and latest state')
    if record['step']=='mint':
        minted_token_id(r,cfg.wallet)
    return h

def run(cfg):
    base=str(cfg.log)+'.manual-open';Path(base).parent.mkdir(parents=True,exist_ok=True);fd=os.open(base+'.lock',os.O_CREAT|os.O_RDWR,0o600)
    try:
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise Blocked('manual operation already running')
        s=load_state(base);scope=state_scope(cfg)
        if s is None:s={'version':1,'scope':scope,'signals':[],'latch':None,'watch':None,'latest':None,'pending':None,'completed':[],'done':False}
        elif s['scope']!=scope:raise Blocked('journal scope mismatch')
        w=connect(cfg)
        while True:
            check_chain(w,97477)
            for item in s['completed']:reconcile(w,cfg,item['call'],item['hash'])
            if s['done']:print(f"Новая NFT #{s.get('new_token_id','UNKNOWN')} подтверждена; повторный mint заблокирован.",flush=True);return 0
            if s['pending']:
                p=s['pending'];print('Ожидается подтверждение ранее показанного вызова:',p['call']['step'],flush=True)
                h=p.get('hash') or input('Вставьте hash транзакции; q — выход; cancel — если точно не подписывали: ').strip()
                if h.lower()=='q':return 0
                if h.lower()=='cancel' and not p.get('hash'):
                    nonce=p['call']['nonce']
                    if w.eth.get_transaction_count(cfg.wallet,'latest')!=nonce or w.eth.get_transaction_count(cfg.wallet,'pending')!=nonce:
                        raise Blocked('nonce changed or pending; manual inspection required')
                    s['pending']=None;save_state(base,s);print('Неподписанный план отменён; выполняется новая проверка.');continue
                if not p.get('hash'):
                    try:valid=len(Web3.to_bytes(hexstr=h))==32
                    except (ValueError,TypeError):valid=False
                    if not valid:raise Blocked('invalid transaction hash')
                    p['hash']=h;save_state(base,s)
                verified=reconcile(w,cfg,p['call'],h,check_approval_state=True)
                s['completed'].append({'call':p['call'],'hash':verified});s['pending']=None
                if p['call']['step']=='mint':
                    s['new_token_id']=minted_token_id(w.eth.get_transaction_receipt(verified),cfg.wallet);s['done']=True
                save_state(base,s);continue
            call=prepare(w,cfg)
            if any(x['call']['step']==call['step'] for x in s['completed']):raise Blocked('confirmed step would repeat; inspect state')
            s['pending']={'call':call,'hash':None};save_state(base,s)
            print('\nВНИМАНИЕ: USD-цена USDC.e не подтверждена; будущая L1 fee не ограничена. Minima рассчитаны для допуска цены 0,1%; цена может измениться.\nПроверьте все поля в кошельке. Программа не подписывает и не отправляет транзакции.\n'+json.dumps(call,ensure_ascii=False,indent=2),flush=True)
    finally:os.close(fd)
