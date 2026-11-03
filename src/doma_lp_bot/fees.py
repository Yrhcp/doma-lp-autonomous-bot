"""Uniswap V3 fee-growth accounting from a single-block snapshot."""
from decimal import Decimal as D
UINT256 = 1 << 256
Q128 = 1 << 128

def inside_growth(global_growth, lower_outside, upper_outside, current, lower, upper):
    below = lower_outside if current >= lower else (global_growth - lower_outside) % UINT256
    above = upper_outside if current < upper else (global_growth - upper_outside) % UINT256
    return (global_growth - below - above) % UINT256

def accrued_fee(liquidity, growth_now, growth_last):
    return liquidity * ((growth_now - growth_last) % UINT256) // Q128

def fee_snapshot(call, pool, p, tick, decimals, weth_is_token0):
    lower = call(pool.functions.ticks(p[5]))
    upper = call(pool.functions.ticks(p[6]))
    if not lower[7] or not upper[7]:
        raise ValueError('position tick not initialized')
    globals_ = [call(pool.functions.feeGrowthGlobal0X128()), call(pool.functions.feeGrowthGlobal1X128())]
    accrued = [accrued_fee(p[7], inside_growth(globals_[i], lower[2+i], upper[2+i], tick, p[5], p[6]), p[8+i]) for i in (0,1)]
    if any(x >= 1 << 128 for x in accrued):
        raise ValueError('accrued fee exceeds uint128; result is not safely estimable')
    # tokensOwed may contain burned principal; it cannot safely be labeled all fees.
    stored = [p[10],p[11]]
    if any(accrued[i] + stored[i] >= 1 << 128 for i in (0,1)):
        raise ValueError('owed total exceeds uint128; result is not safely estimable')
    values = [D(accrued[i]) / D(10)**decimals[i] for i in (0,1)]
    totals = [D(accrued[i] + stored[i]) / D(10)**decimals[i] for i in (0,1)]
    wi,ui = (0,1) if weth_is_token0 else (1,0)
    return {'status':'estimated_from_fee_growth', 'accrued_since_update_weth':values[wi], 'accrued_since_update_usdc':values[ui],
            'stored_owed0_raw':stored[0], 'stored_owed1_raw':stored[1],
            'collectable_weth_estimate':totals[wi], 'collectable_usdc_estimate':totals[ui],
            'note':'stored tokensOwed may include withdrawn principal; collectable total is not necessarily all fees; integer rounding and overflow behavior may differ at extreme values'}
