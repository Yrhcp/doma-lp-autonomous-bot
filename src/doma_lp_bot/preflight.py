"""Read-only candidate for a NEW position; never signs or sends anything.

A real USD oracle, network cost bound and reconciliation are not implemented.
Consequently the public preparation path is intentionally fail-closed.
"""
import time
import uuid
from decimal import Decimal as D, localcontext, ROUND_DOWN
from web3 import Web3
from .rpc import connect,check_chain
from .contracts import fn,MANAGER,FACTORY,POOL
from .v3_integer import ticks_for_pool,liquidity_for_amounts,amounts_for_liquidity
from .v3_math import raw_price_from_sqrt,quote_price,range_for_quote,raw_price_at_tick,amounts,sqrt_ratio_at_tick

QUOTER=Web3.to_checksum_address('0x2023ADF9fF50219C34989baABD76A0f5cfe432f4')
MANAGER_ADDRESS=Web3.to_checksum_address('0xce126ca6aceBBDCe95D7b8A3Ce637951640811E0')
FACTORY_ADDRESS=Web3.to_checksum_address('0x2e50b586d5bcD04cb6125E028A6a669f7f3cF1C2')
ROUTER=Web3.to_checksum_address('0x5089863E97196773038f98459262D866f2281f58')
WETH=Web3.to_checksum_address('0x4200000000000000000000000000000000000006')
USDC=Web3.to_checksum_address('0x31EEf89D5215C305304a2fA5376a1f1b6C5dc477')
TOKEN=[fn('balanceOf',['address'],['uint256']),fn('allowance',['address','address'],['uint256']),fn('decimals',[],['uint8'])]
QUOTER_ABI=[fn('factory',[],['address']),{'type':'function','name':'quoteExactInputSingle','stateMutability':'nonpayable',
    'inputs':[{'name':'params','type':'tuple','components':[{'name':'tokenIn','type':'address'},{'name':'tokenOut','type':'address'},{'name':'amountIn','type':'uint256'},{'name':'fee','type':'uint24'},{'name':'sqrtPriceLimitX96','type':'uint160'}]}],
    'outputs':[{'name':'amountOut','type':'uint256'},{'name':'sqrtPriceX96After','type':'uint160'},{'name':'initializedTicksCrossed','type':'uint32'},{'name':'gasEstimate','type':'uint256'}]}]

# Exact minimal ABI for the verified manager. Encoding only; no execution path.
MINT_ABI=[{'type':'function','name':'mint','stateMutability':'payable','inputs':[{'name':'params','type':'tuple','components':[
    {'name':'token0','type':'address'},{'name':'token1','type':'address'},{'name':'fee','type':'uint24'},
    {'name':'tickLower','type':'int24'},{'name':'tickUpper','type':'int24'}, {'name':'amount0Desired','type':'uint256'},
    {'name':'amount1Desired','type':'uint256'},{'name':'amount0Min','type':'uint256'}, {'name':'amount1Min','type':'uint256'},
    {'name':'recipient','type':'address'},{'name':'deadline','type':'uint256'}]}],
    'outputs':[{'name':'tokenId','type':'uint256'},{'name':'liquidity','type':'uint128'},{'name':'amount0','type':'uint256'},{'name':'amount1','type':'uint256'}]}]

class PreparationBlocked(ValueError): pass

def validate_budget(budget,reserve):
    budget=D(budget);reserve=D(reserve)
    if not budget.is_finite() or not reserve.is_finite() or not 0 < budget <= 50 or not 0 < reserve < budget:
        raise ValueError('budget must be <= 50 USD and reserve must be positive and less than budget')
    return budget,reserve

def prepare_mint_unsigned(plan):
    """Future gate; no executable transaction until ALL unresolved checks are implemented."""
    if plan.get('blockers') or plan.get('status') != 'READY_VERIFIED':
        raise PreparationBlocked('unsafe/incomplete plan; no transaction prepared')
    # No current live preflight sets READY_VERIFIED. This explicit stop prevents
    # fabricated inputs bypassing budget, uniqueness, oracle and gas checks.
    raise PreparationBlocked('unsigned mint builder disabled pending independent oracle, complete fee cap and reconciliation')

def illustrative_swap_requirement(balances, wants, price):
    """Research-only spot sizing; can under/overestimate impact and is not executable."""
    b0,b1=balances;w0,w1=wants
    if b0<w0 and b1<w1: return None,'both target assets are deficient; funding is required'
    if b0<w0 and b1>w1:
        needed=int((D(w0-b0)/D(10**6)/price*D(10**18)).to_integral_value(rounding=ROUND_DOWN))
        amount=min(b1-w1,needed)
        return ((WETH,USDC,amount) if amount>0 else None), 'illustrative spot-sized exact-input WETH to USDC.e'
    if b1<w1 and b0>w0:
        needed=int((D(w1-b1)/D(10**18)*price*D(10**6)).to_integral_value(rounding=ROUND_DOWN))
        amount=min(b0-w0,needed)
        return ((USDC,WETH,amount) if amount>0 else None), 'illustrative spot-sized exact-input USDC.e to WETH'
    return None,'target amounts already covered or no convertible surplus'

def live_preflight(cfg,budget=D('50'),reserve=D('5'),width=D('0.7')):
    budget,reserve=validate_budget(budget,reserve)
    if not width.is_finite() or not 0<width<100: raise ValueError('invalid range percent')
    if cfg.chain!=97477 or cfg.manager!=MANAGER_ADDRESS or cfg.usdc!=USDC or cfg.weth!=WETH:
        raise ValueError('Doma mainnet or verified contract addresses do not match configured values')
    w=connect(cfg);check_chain(w,97477);block=w.eth.get_block('latest');n=block.number
    age=time.time()-block.timestamp
    if age>cfg.max_age or age < -30: raise ValueError('stale or future block')
    def call(f): return f.call(block_identifier=n)
    for address in (MANAGER_ADDRESS,FACTORY_ADDRESS,QUOTER,WETH,USDC):
        if not w.eth.get_code(address,block_identifier=n): raise ValueError(f'no code at {address}')
    manager=w.eth.contract(address=MANAGER_ADDRESS,abi=MANAGER)
    quoter=w.eth.contract(address=QUOTER,abi=QUOTER_ABI)
    if call(manager.functions.factory()) != FACTORY_ADDRESS or call(quoter.functions.factory()) != FACTORY_ADDRESS:
        raise ValueError('manager/quoter factory mismatch')
    factory=w.eth.contract(address=FACTORY_ADDRESS,abi=FACTORY)
    pool_addr=call(factory.functions.getPool(USDC,WETH,100))
    if int(pool_addr,16)==0 or not w.eth.get_code(pool_addr,block_identifier=n): raise ValueError('pool missing')
    pool=w.eth.contract(address=pool_addr,abi=POOL)
    t0,t1=call(pool.functions.token0()),call(pool.functions.token1())
    fee,spacing,slot=call(pool.functions.fee()),call(pool.functions.tickSpacing()),call(pool.functions.slot0())
    if (t0,t1,fee)!=(USDC,WETH,100) or spacing<=0: raise ValueError('pool identity mismatch')
    if not slot[6] or not sqrt_ratio_at_tick(slot[1]) <= slot[0] <= sqrt_ratio_at_tick(min(slot[1]+1,887272)):
        raise ValueError('pool slot0 invalid or inconsistent')
    tokens=[w.eth.contract(address=x,abi=TOKEN) for x in (t0,t1)]
    decimals=[call(t.functions.decimals()) for t in tokens]
    if decimals != [6,18]: raise ValueError('token decimals mismatch')
    balances=[call(t.functions.balanceOf(cfg.wallet)) for t in tokens]
    allowances={str(addr):[call(t.functions.allowance(cfg.wallet,addr)) for t in tokens] for addr in (MANAGER_ADDRESS,ROUTER)}
    raw=raw_price_from_sqrt(slot[0]);price=quote_price(raw,6,18,False)
    lo,hi=range_for_quote(raw,width,spacing,False)
    bounds=sorted(quote_price(raw_price_at_tick(t),6,18,False) for t in (lo,hi))
    # USD valuation is NOT verified. These numbers merely illustrate the shape
    # of a new position at 1 USDC.e = 1 USD, and are never used for authorization.
    with localcontext() as ctx:
        ctx.prec=70
        a0,a1=amounts(10**18,slot[0],lo,hi)
        u=a0/D(10**6); e=a1/D(10**18)
        total=u+e*price
        hypothetical=budget-reserve
        desired_usdc=hypothetical*u/total
        desired_weth=hypothetical*e/total
        want0=int((desired_usdc*D(10**6)).to_integral_value(rounding=ROUND_DOWN))
        want1=int((desired_weth*D(10**18)).to_integral_value(rounding=ROUND_DOWN))
    sqrt_lo,sqrt_hi=ticks_for_pool(lo,hi,spacing)
    liquidity=liquidity_for_amounts(slot[0],sqrt_lo,sqrt_hi,want0,want1)
    used0,used1=amounts_for_liquidity(slot[0],sqrt_lo,sqrt_hi,liquidity,True)
    if used0>want0 or used1>want1: raise ValueError('integer mint requirement exceeds desired amounts')
    # A real single-pool Quoter eth_call. This is a probe, not an actionable
    # quote for any proposed swap; no assumption about best route or routers.
    quote=None;quote_error=None
    try:
        out=call(quoter.functions.quoteExactInputSingle((USDC,WETH,1_000_000,100,0)))
        quote={'token_in':USDC,'token_out':WETH,'amount_in_raw':1_000_000,'amount_out_raw':out[0],
               'sqrt_price_after_x96':out[1],'initialized_ticks_crossed':out[2],'quoter_gas_estimate_not_total':out[3],
               'purpose':'1 USDC.e route probe only; not a quote for the eventual swap','pool':pool_addr,'block':n}
    except Exception as exc:
        quote_error=type(exc).__name__
    need,need_note=illustrative_swap_requirement(balances,(want0,want1),price)
    suggested_quote=None;suggested_quote_error=None
    if need is not None:
        try:
            result=call(quoter.functions.quoteExactInputSingle((need[0],need[1],need[2],100,0)))
            suggested_quote={'token_in':need[0],'token_out':need[1],'amount_in_raw':need[2],
                             'amount_out_raw':result[0],'sqrt_price_after_x96':result[1],
                             'initialized_ticks_crossed':result[2],'quoter_gas_estimate_not_total':result[3],
                             'pool':pool_addr,'block':n,'block_timestamp':block.timestamp,
                             'note':'indicative spot-sized candidate; no protection or valid route guarantee'}
        except Exception as exc:
            suggested_quote_error=type(exc).__name__
    eth=w.eth.get_balance(cfg.wallet,block_identifier=n)
    gas_price=w.eth.gas_price
    if time.time()-block.timestamp > cfg.max_age: raise ValueError('snapshot became stale during preflight')
    if w.eth.get_block(n).hash != block.hash: raise ValueError('block changed while preparing')
    blockers=['USD oracle and depeg protection unavailable: 1 USDC.e = 1 USD is NOT verified',
              'complete worst-case network costs and L1/data fees are not bounded',
              'independent price validation, slippage/minimum amounts and stale-quote protection not implemented',
              'verified transaction simulation, duplicate protection and partial-execution reconciliation not implemented',
              'real swap route, router fees and execution price for required amount not verified']
    if balances[0]<want0 or balances[1]<want1:
        blockers.append('current wallet balances do not cover hypothetical target without funding or a separately verified swap')
    return {'plan_id':str(uuid.uuid4()),'status':'BLOCKED_READ_ONLY','created_at_utc':__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(),
        'chain_id':97477,'block':n,'block_hash':block.hash.hex(),'block_timestamp':block.timestamp,'wallet':cfg.wallet,
        'position':'NEW_POSITION_ONLY; protected NFTs are not modified', 'manager':MANAGER_ADDRESS,'factory':FACTORY_ADDRESS,
        'pool':pool_addr,'quoter':QUOTER,'token0':t0,'token1':t1,'decimals':decimals,'fee':fee,'tick_spacing':spacing,
        'sqrt_price_x96':slot[0],'tick':slot[1],'spot_usdc_per_weth':price,'lower_tick':lo,'upper_tick':hi,
        'lower_price_usdc_per_weth':bounds[0],'upper_price_usdc_per_weth':bounds[1],
        'budget_usd_cap':budget,'hypothetical_fee_reserve_usd':reserve,
        'illustrative_assumption':'1 USDC.e = 1 USD ONLY for illustration; USD limit is unverified and no operation may be signed',
        'illustrative_desired_usdc_raw':want0,'illustrative_desired_weth_raw':want1,
        'illustrative_liquidity_integer':liquidity,'illustrative_mint_required_raw':[used0,used1],
        'wallet_balances_raw_token0_token1':balances,'allowances_raw_by_spender':allowances,'native_eth_wei':eth,
        'gas_price_wei_current_not_cost_cap':gas_price,'pool_quote_probe':quote,'quote_error':quote_error,
        'illustrative_swap_sizing_note':need_note,'illustrative_swap_quote':suggested_quote,'illustrative_swap_quote_error':suggested_quote_error,
        'unsigned_transaction_template':{'chainId':97477,'from':cfg.wallet,'to':MANAGER_ADDRESS,'value':0,'data':None,'gas':None,'status':'NOT_SIGNABLE_BLOCKED'},
        'transaction_data':None,'blockers':blockers,
        'next_steps':['independently verify USD oracle and all network fees','verify route and quote for exact amount',
                      'verify actual funding and approvals; never unlimited approvals',
                      'implement transaction simulation, stale quote and partial execution recovery','independent review before any signature']}
