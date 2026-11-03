"""Uniswap V3 integer liquidity arithmetic, research and read-only only.

LiquidityAmounts uses floor division; pool Mint deltas use round-up. These
functions do not constitute a slippage-safe transaction or an on-chain quote.
"""
from .v3_math import sqrt_ratio_at_tick
Q96=1<<96
MAX_UINT128=(1<<128)-1
MAX_UINT160=(1<<160)-1
MAX_UINT256=(1<<256)-1

def _validate(a,b):
    if not (isinstance(a,int) and isinstance(b,int) and 0<a<b<=MAX_UINT160):
        raise ValueError('invalid sqrt price bounds')

def _ceil(n,d):
    if d<=0: raise ValueError('invalid divisor')
    return (n+d-1)//d

def liquidity_for_amount0(a,b,amount):
    _validate(a,b)
    if not isinstance(amount,int) or not 0<=amount<=MAX_UINT256: raise ValueError('invalid amount0')
    intermediate=a*b//Q96
    result=amount*intermediate//(b-a)
    if result>MAX_UINT128: raise OverflowError('liquidity exceeds uint128')
    return result

def liquidity_for_amount1(a,b,amount):
    _validate(a,b)
    if not isinstance(amount,int) or not 0<=amount<=MAX_UINT256: raise ValueError('invalid amount1')
    result=amount*Q96//(b-a)
    if result>MAX_UINT128: raise OverflowError('liquidity exceeds uint128')
    return result

def liquidity_for_amounts(current,lower,upper,amount0,amount1):
    _validate(lower,upper)
    if not isinstance(current,int) or not 0<current<=MAX_UINT160: raise ValueError('invalid current price')
    if current<=lower: return liquidity_for_amount0(lower,upper,amount0)
    if current<upper: return min(liquidity_for_amount0(current,upper,amount0),liquidity_for_amount1(lower,current,amount1))
    return liquidity_for_amount1(lower,upper,amount1)

def amount0_delta(a,b,liquidity,round_up):
    _validate(a,b)
    if not isinstance(liquidity,int) or not 0<=liquidity<=MAX_UINT128: raise ValueError('invalid liquidity')
    n=liquidity<<96
    # SqrtPriceMath.getAmount0Delta: mulDivRoundingUp then divRoundingUp.
    return _ceil(_ceil(n*(b-a),b),a) if round_up else (n*(b-a)//b)//a

def amount1_delta(a,b,liquidity,round_up):
    _validate(a,b)
    if not isinstance(liquidity,int) or not 0<=liquidity<=MAX_UINT128: raise ValueError('invalid liquidity')
    n=liquidity*(b-a)
    return _ceil(n,Q96) if round_up else n//Q96

def amounts_for_liquidity(current,lower,upper,liquidity,round_up=False):
    _validate(lower,upper)
    if not isinstance(current,int) or not 0<current<=MAX_UINT160: raise ValueError('invalid current price')
    if current<=lower: return amount0_delta(lower,upper,liquidity,round_up),0
    if current<upper: return amount0_delta(current,upper,liquidity,round_up),amount1_delta(lower,current,liquidity,round_up)
    return 0,amount1_delta(lower,upper,liquidity,round_up)

def ticks_for_pool(lower,upper,spacing):
    if not isinstance(spacing,int) or spacing<=0 or lower>=upper or lower%spacing or upper%spacing:
        raise ValueError('invalid ticks or tick spacing')
    return sqrt_ratio_at_tick(lower),sqrt_ratio_at_tick(upper)
