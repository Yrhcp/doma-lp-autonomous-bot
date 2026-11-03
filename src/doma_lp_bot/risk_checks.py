"""Conservative, pure risk checks. No source is enabled for live trading."""
from dataclasses import dataclass
from decimal import Decimal as D, localcontext, ROUND_CEILING

@dataclass(frozen=True)
class PriceObservation:
    source: str
    asset: str
    chain_id: int
    token: str
    price_usd: D
    observed_at: int

class RiskBlocked(ValueError): pass

def independent_prices(observations, *, expected_asset, expected_token, chain_id, now, max_age, max_deviation, allowed_sources, max_depeg=None):
    if max_age<=0 or not D(0)<D(max_deviation)<D(1): raise RiskBlocked('invalid oracle policy')
    if len(set(allowed_sources))<2: raise RiskBlocked('two independently verified sources are required')
    accepted={}
    for q in observations:
        if q.source not in allowed_sources or q.source in accepted or q.asset!=expected_asset or q.chain_id!=chain_id or q.token.lower()!=expected_token.lower(): continue
        price=D(q.price_usd)
        if not price.is_finite() or price<=0 or not 0<=now-q.observed_at<=max_age: continue
        accepted[q.source]=price
    if len(accepted)<2: raise RiskBlocked('independent fresh observations unavailable for this exact token and chain')
    lo,hi=min(accepted.values()),max(accepted.values())
    if (hi-lo)/lo > D(max_deviation): raise RiskBlocked('independent prices disagree')
    if max_depeg is not None and (not D(0)<D(max_depeg)<D(1) or lo < D(1)-D(max_depeg) or hi > D(1)+D(max_depeg)):
        raise RiskBlocked('stablecoin depeg threshold exceeded')
    return lo,hi

def budget_upper_usd(*,usdc_raw,weth_raw,usdc_high,weth_high,gas_limits,max_fee_per_gas,extra_fee_wei,extra_cost_usd,swap_input_usd):
    """Conservative bound: counts the entire swap input in addition to deposit.

    May double count and reject valid plans. Gas limits cover all transactions;
    L1/data and additional costs MUST be supplied as independent upper bounds.
    """
    vals=[usdc_high,weth_high,extra_cost_usd,swap_input_usd]
    if any(x is None for x in vals) or extra_fee_wei is None or max_fee_per_gas is None or not gas_limits: raise RiskBlocked('cost bound missing')
    vals=[D(x) for x in vals]
    if any(not x.is_finite() or x<0 for x in vals) or vals[0]<=0 or vals[1]<=0: raise RiskBlocked('invalid price/cost')
    integers=[usdc_raw,weth_raw,max_fee_per_gas,extra_fee_wei,*gas_limits]
    if any(not isinstance(x,int) or isinstance(x,bool) or x<0 for x in integers) or not all(x>0 for x in gas_limits) or max_fee_per_gas<=0:
        raise RiskBlocked('invalid raw quantities')
    gas_wei=sum(gas_limits)*max_fee_per_gas+extra_fee_wei
    with localcontext() as ctx:
        ctx.prec=100;ctx.rounding=ROUND_CEILING
        total=D(usdc_raw)*vals[0]/D(10**6)+D(weth_raw)*vals[1]/D(10**18)+D(gas_wei)*vals[1]/D(10**18)+vals[2]+vals[3]
    if total>D(50): raise RiskBlocked('worst-case bound exceeds $50')
    return total

def minimum_out(amount_out, slippage_bps):
    if not isinstance(amount_out,int) or amount_out<=0 or not isinstance(slippage_bps,int) or not 0<slippage_bps<10000:
        raise RiskBlocked('invalid quote or slippage')
    result=amount_out*(10000-slippage_bps)//10000
    if result<=0: raise RiskBlocked('minimum output rounds to zero')
    return result

def validate_swap_quote(quote,*,chain_id,token_in,token_out,pool,amount_in,current_block,current_time,max_block_lag,max_age,slippage_bps):
    """Validate provided evidence; caller must authenticate RPC and pool separately."""
    try:
        if quote['chain_id']!=chain_id or quote['token_in'].lower()!=token_in.lower() or quote['token_out'].lower()!=token_out.lower() or quote['pool'].lower()!=pool.lower():
            raise RiskBlocked('quote pair, chain or pool mismatch')
        if quote['amount_in_raw']!=amount_in or not isinstance(amount_in,int) or amount_in<=0:
            raise RiskBlocked('quote amount mismatch')
        if not isinstance(quote['block'],int) or not isinstance(quote['timestamp'],int) or max_age<=0 or max_block_lag<0:
            raise RiskBlocked('invalid quote timing')
        if not 0<=current_time-quote['timestamp']<=max_age or not 0<=current_block-quote['block']<=max_block_lag:
            raise RiskBlocked('stale or future quote')
        return minimum_out(quote['amount_out_raw'],slippage_bps)
    except (KeyError,TypeError,AttributeError) as exc:
        raise RiskBlocked('incomplete quote evidence') from exc
