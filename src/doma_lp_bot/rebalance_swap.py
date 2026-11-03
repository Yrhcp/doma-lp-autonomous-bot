"""Manual, bounded single-pool swap for position rebalancing."""
import time
from decimal import Decimal
from eth_abi import encode,decode
from web3 import Web3

from .manual_open import Blocked, USDC, WETH, MANAGER_ADDRESS, MAX_STEP_FEE_WEI, APPROVE_ABI, oracle
from .preflight import FACTORY_ADDRESS, ROUTER, TOKEN, QUOTER, QUOTER_ABI
from .rebalance_preview import estimate_swap
from .v3_integer import ticks_for_pool, amounts_for_liquidity
from .v3_math import raw_price_from_sqrt, range_for_quote, range_for_offset

PERMIT2=Web3.to_checksum_address('0x000000000022D473030F116dDEE9F6B43aC78BA3')
ROUTER_HASH='e16c4e73f116df542880bac897fef57f4162c2c916de66e4f97f7a938e63170f'
PERMIT_HASH='ef1ed184f18f79781d7abbb97561b742b149c59c424a2a9d4d7f8a4190db6830'
SWAP_STEPS=('approve_swap_token','approve_swap_permit2','swap')
PERMIT_ABI=[{'type':'function','name':'allowance','stateMutability':'view','inputs':[{'name':'owner','type':'address'},{'name':'token','type':'address'},{'name':'spender','type':'address'}],'outputs':[{'type':'uint160'},{'type':'uint48'},{'type':'uint48'}]},
 {'type':'function','name':'approve','stateMutability':'nonpayable','inputs':[{'name':'token','type':'address'},{'name':'spender','type':'address'},{'name':'amount','type':'uint160'},{'name':'expiration','type':'uint48'}],'outputs':[]}]
ROUTER_ABI=[{'type':'function','name':'V3_POSITION_MANAGER','stateMutability':'view','inputs':[],'outputs':[{'type':'address'}]},
 {'type':'function','name':'execute','stateMutability':'payable','inputs':[{'name':'commands','type':'bytes'},{'name':'inputs','type':'bytes[]'},{'name':'deadline','type':'uint256'}],'outputs':[]}]


def contracts(w,block):
    if Web3.keccak(w.eth.get_code(ROUTER,block_identifier=block)).hex()!=ROUTER_HASH or Web3.keccak(w.eth.get_code(PERMIT2,block_identifier=block)).hex()!=PERMIT_HASH:
        raise Blocked('код router или Permit2 изменился')
    r=w.eth.contract(address=ROUTER,abi=ROUTER_ABI)
    q=w.eth.contract(address=QUOTER,abi=QUOTER_ABI)
    if r.functions.V3_POSITION_MANAGER().call(block_identifier=block)!=MANAGER_ADDRESS or q.functions.factory().call(block_identifier=block)!=FACTORY_ADDRESS:
        raise Blocked('router или quoter не соответствует фабрике/менеджеру')
    return q


def plan_for(s,budget,range_percent=Decimal('0.7'),offset=None):
    raw=raw_price_from_sqrt(s['sqrt']);lo,hi=(range_for_offset(raw,offset,1) if offset is not None else range_for_quote(raw,range_percent,1,False))
    a,b=ticks_for_pool(lo,hi,1);target=amounts_for_liquidity(s['sqrt'],a,b,10**18)
    tin,tout,amount=estimate_swap(*budget,*target,raw)
    value=Decimal(amount) if tin==USDC else Decimal(amount)/raw if tin==WETH else Decimal(0)
    if value<=10000:return None
    if not 0<amount<2**160 or amount>budget[0 if tin==USDC else 1]:raise Blocked('неверный объём swap')
    return {'token_in':tin,'token_out':tout,'amount_in':amount,'budget_raw':list(budget),'permit_expiry':int(time.time())+1800}


def validate_plan(plan,budget):
    if (not isinstance(plan,dict) or plan.get('budget_raw')!=budget or
        (plan.get('token_in'),plan.get('token_out')) not in ((USDC,WETH),(WETH,USDC)) or
        type(plan.get('amount_in')) is not int or not 0<plan['amount_in']<2**160 or
        plan['amount_in']>budget[0 if plan['token_in']==USDC else 1] or type(plan.get('permit_expiry')) is not int):
        raise Blocked('неверный план swap')


def encode_swap(cfg,plan,min_out,deadline):
    path=bytes.fromhex(plan['token_in'][2:])+int(100).to_bytes(3,'big')+bytes.fromhex(plan['token_out'][2:])
    inp=encode(['address','uint256','uint256','bytes','bool'],[cfg.wallet,plan['amount_in'],min_out,path,True])
    return Web3().eth.contract(abi=ROUTER_ABI).encode_abi('execute',args=[b'\x00',[inp],deadline])


def validate_data(cfg,record,plan):
    step=record['step'];data=record['data'];to=record['to'];amount=plan['amount_in'];token=plan['token_in']
    if step=='approve_swap_token':
        fn,p=Web3().eth.contract(abi=APPROVE_ABI).decode_function_input(data)
        if to!=token or fn.fn_name!='approve' or p!={'spender':PERMIT2,'value':amount}:raise Blocked('неверный ERC20 approve')
    elif step=='approve_swap_permit2':
        fn,p=Web3().eth.contract(abi=PERMIT_ABI).decode_function_input(data)
        if to!=PERMIT2 or fn.fn_name!='approve' or (p['token'],p['spender'],p['amount'],p['expiration'])!=(token,ROUTER,amount,plan['permit_expiry']):raise Blocked('неверный Permit2 approve')
    elif step=='swap':
        fn,p=Web3().eth.contract(abi=ROUTER_ABI).decode_function_input(data)
        if to!=ROUTER or fn.fn_name!='execute' or p['commands']!=b'\x00' or len(p['inputs'])!=1:raise Blocked('неверная команда router')
        recipient,amt,minimum,path,payer=decode(['address','uint256','uint256','bytes','bool'],p['inputs'][0])
        expected=bytes.fromhex(token[2:])+int(100).to_bytes(3,'big')+bytes.fromhex(plan['token_out'][2:])
        if recipient.lower()!=cfg.wallet.lower() or amt!=amount or not minimum or path!=expected or not payer or p['deadline']!=record['details']['deadline'] or minimum!=record['details']['amountOutMinimum']:
            raise Blocked('неверные параметры swap')
        return minimum,p['deadline']
    else:raise Blocked('неизвестный этап swap')
    return None


def next_call(w,cfg,s,plan,steps):
    token=plan['token_in'];amount=plan['amount_in'];block=s['block']
    if plan['permit_expiry']<time.time()+180:raise Blocked('срок ограниченного Permit2 истёк; требуется ручная проверка')
    if any(s['balances'][i]<plan['budget_raw'][i] for i in (0,1)):raise Blocked('полученные токены перемещены')
    q=contracts(w,block)
    erc=w.eth.contract(address=token,abi=TOKEN).functions.allowance(cfg.wallet,PERMIT2).call(block_identifier=block)
    if erc!=amount:
        if 'approve_swap_token' in steps:raise Blocked('разрешение токена изменилось')
        c=Web3().eth.contract(abi=APPROVE_ABI)
        return 'approve_swap_token',token,c.encode_abi('approve',args=[PERMIT2,amount]),{'spender':PERMIT2,'amount_raw':amount}
    permit=w.eth.contract(address=PERMIT2,abi=PERMIT_ABI)
    allowed,expiry,_=permit.functions.allowance(cfg.wallet,token,ROUTER).call(block_identifier=block)
    if allowed!=amount or expiry!=plan['permit_expiry']:
        if 'approve_swap_permit2' in steps:raise Blocked('разрешение Permit2 изменилось')
        c=Web3().eth.contract(abi=PERMIT_ABI)
        return 'approve_swap_permit2',PERMIT2,c.encode_abi('approve',args=[token,ROUTER,amount,plan['permit_expiry']]),{'token':token,'spender':ROUTER,'amount_raw':amount,'expiration':plan['permit_expiry']}
    out=q.functions.quoteExactInputSingle((token,plan['token_out'],amount,100,0)).call(block_identifier=block)[0]
    minimum=out*995//1000
    if not minimum:raise Blocked('котировка swap равна нулю')
    deadline=min(int(time.time())+180,plan['permit_expiry'])
    return 'swap',ROUTER,encode_swap(cfg,plan,minimum,deadline),{'amountIn':amount,'amountOutMinimum':minimum,'quoteOut':out,'slippageBps':50,'deadline':deadline,'tokenIn':token,'tokenOut':plan['token_out']}


def verify(w,cfg,record,plan,budget):
    from .rebalance_manual import read,fee_check
    validate_plan(plan,budget);s=read(w,cfg);step=record['step']
    if record['chainId']!=97477 or record['from'].lower()!=cfg.wallet.lower() or record['value']!=0 or record['nonce']!=s['nonce']:
        raise Blocked('неверный sender, сеть или nonce')
    if any(s['balances'][i]<budget[i] for i in (0,1)):raise Blocked('полученные токены перемещены')
    q=contracts(w,s['block']);parsed=validate_data(cfg,record,plan)
    if not time.time()+30<plan['permit_expiry']<=time.time()+1800:raise Blocked('срок Permit2 истёк или превышен')
    tx={'from':cfg.wallet,'to':record['to'],'value':0,'data':record['data']}
    if step=='swap':
        minimum,deadline=parsed
        if not time.time()+30<deadline<=plan['permit_expiry']:raise Blocked('deadline истёк')
        erc=w.eth.contract(address=plan['token_in'],abi=TOKEN).functions.allowance(cfg.wallet,PERMIT2).call(block_identifier=s['block'])
        amount,expiry,_=w.eth.contract(address=PERMIT2,abi=PERMIT_ABI).functions.allowance(cfg.wallet,plan['token_in'],ROUTER).call(block_identifier=s['block'])
        if erc!=plan['amount_in'] or amount!=plan['amount_in'] or expiry!=plan['permit_expiry']:raise Blocked('разрешения swap изменились')
        out=q.functions.quoteExactInputSingle((plan['token_in'],plan['token_out'],plan['amount_in'],100,0)).call(block_identifier=s['block'])[0]
        if not minimum or minimum<out*995//1000:raise Blocked('minOut хуже свежей котировки более чем на 0,5%')
    result=w.eth.call(tx,block_identifier=s['block'])
    if step=='approve_swap_token' and not decode(['bool'],result)[0]:raise Blocked('approve отклонён')
    gas=w.eth.estimate_gas(tx,block_identifier=s['block'])
    if gas>record['gasLimit']:raise Blocked('gas вырос; пересчитайте')
    l1,op=fee_check(w,s,record)
    if w.eth.get_block(s['block']).hash.hex()!=s['hash'] or time.time()-s['timestamp']>min(cfg.max_age,120):raise Blocked('устаревший блок')
    return {'block':s['block'],'tick':s['tick'],'gasEstimate':gas,'l1EstimateWei':l1,'operatorEstimateWei':op}


def _prepare_once(w,cfg,s,plan,budget,steps):
    validate_plan(plan,budget);step,target,data,details=next_call(w,cfg,s,plan,steps)
    tx={'from':cfg.wallet,'to':target,'value':0,'data':data}
    gas=w.eth.estimate_gas(tx,block_identifier=s['block']);limit=(gas*13+9)//10
    priority=1_000_000;maxfee=max(w.eth.gas_price*2,s['base_fee']*2+priority)
    record={'step':step,'chainId':97477,'from':cfg.wallet,'to':target,'data':data,'value':0,'nonce':s['nonce'],'gasLimit':limit,'maxFeePerGas':maxfee,'maxPriorityFeePerGas':priority,'details':details}
    record['revalidation']=verify(w,cfg,record,plan,budget)
    return record


def prepare(w,cfg,s,plan,budget,steps):
    """Requote an unsigned swap if its 0.5% bound became stale during preparation."""
    from .rebalance_manual import read
    for attempt in range(4):
        try:return _prepare_once(w,cfg,s,plan,budget,steps)
        except Blocked as exc:
            if 'minOut хуже свежей котировки более чем на 0,5%' not in str(exc):raise
            if attempt==3:raise Blocked('Котировка меняется во время подготовки swap; нажмите «Пересчитать». Вызов не подготовлен.') from exc
            print('Котировка изменилась; безопасно пересчитываю swap…',flush=True)
            s=read(w,cfg)


def transfers(r,wallet,plan):
    from .manual_open import POOL_ADDRESS
    event=Web3.keccak(text='Transfer(address,address,uint256)');address=lambda t:bytes(t)[-20:]
    wallet_b=bytes.fromhex(wallet[2:]);pool_b=bytes.fromhex(POOL_ADDRESS[2:]);sent=received=0
    for x in r['logs']:
        if len(x['topics'])!=3 or x['topics'][0]!=event:continue
        a,b=address(x['topics'][1]),address(x['topics'][2]);value=int.from_bytes(x['data'],'big')
        if x['address'].lower()==plan['token_in'].lower() and a==wallet_b and b==pool_b:sent+=value
        if x['address'].lower()==plan['token_out'].lower() and a==pool_b and b==wallet_b:received+=value
    if sent!=plan['amount_in'] or received<=0:raise Blocked('Transfer swap не соответствует ожидаемому маршруту')
    return received


def reconcile(w,cfg,record,h,plan,budget):
    from .rebalance_manual import FailedTransaction
    validate_plan(plan,budget);parsed=validate_data(cfg,record,plan)
    if record['chainId']!=97477 or record['from'].lower()!=cfg.wallet.lower() or record['value']!=0:raise Blocked('неверные параметры журнала')
    tx=w.eth.get_transaction(h);r=w.eth.get_transaction_receipt(h)
    if (tx['from'].lower()!=cfg.wallet.lower() or tx.get('chainId')!=97477 or tx['to']!=record['to'] or tx['value']!=0 or tx['nonce']!=record['nonce'] or Web3.to_hex(tx['input']).lower()!=record['data'].lower()):raise Blocked('транзакция не соответствует журналу')
    if w.eth.get_block(r['blockNumber']).hash!=r['blockHash'] or w.eth.block_number<r['blockNumber']+1:raise Blocked('receipt не имеет двух подтверждений')
    if r['status']!=1:raise FailedTransaction('транзакция подтверждена с ошибкой')
    gas=tx['gas'];unit=tx.get('maxFeePerGas') if tx.get('type')==2 else tx.get('gasPrice');l1=r.get('l1Fee')
    if isinstance(l1,str):l1=int(l1,16) if l1.startswith('0x') else int(l1)
    if (not isinstance(gas,int) or not 21000<=gas<=min(800000,2*record['gasLimit']) or not isinstance(unit,int) or unit<=0 or not isinstance(l1,int) or l1<0 or gas*unit+l1+oracle(w,'getOperatorFee(uint256)',gas,r['blockNumber'])>MAX_STEP_FEE_WEI):raise Blocked('gas-параметры вне лимита')
    if record['step']=='approve_swap_token':
        value=w.eth.contract(address=plan['token_in'],abi=TOKEN).functions.allowance(cfg.wallet,PERMIT2).call(block_identifier=r['blockNumber'])
        if value!=plan['amount_in']:raise Blocked('ERC20 allowance не подтверждён')
    elif record['step']=='approve_swap_permit2':
        amount,expiry,_=w.eth.contract(address=PERMIT2,abi=PERMIT_ABI).functions.allowance(cfg.wallet,plan['token_in'],ROUTER).call(block_identifier=r['blockNumber'])
        if (amount,expiry)!=(plan['amount_in'],plan['permit_expiry']):raise Blocked('Permit2 allowance не подтверждён')
    else:
        received=transfers(r,cfg.wallet,plan)
        if received<parsed[0]:raise Blocked('выход swap ниже minOut')
        return received
    return None
