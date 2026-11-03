# SPDX-License-Identifier: GPL-2.0-or-later
# TickMath constants and getSqrtRatioAtTick derive from Uniswap v3-core.
"""High precision, indicative V3 math; not contract-level integer rounding."""
from decimal import Decimal, localcontext, ROUND_FLOOR, ROUND_CEILING

D = Decimal
Q96 = D(2**96)
MIN_TICK, MAX_TICK = -887272, 887272


def raw_price_from_sqrt(sqrt_x96):
    if not 0 < sqrt_x96 < 2**160:
        raise ValueError('invalid sqrtPriceX96')
    with localcontext() as c:
        c.prec = 80
        return (D(sqrt_x96) / Q96) ** 2


def raw_price_at_tick(tick):
    if not MIN_TICK <= tick <= MAX_TICK:
        raise ValueError('tick outside V3 limits')
    with localcontext() as c:
        c.prec = 80
        return D('1.0001') ** tick


def price_to_tick(raw_price):
    if raw_price <= 0:
        raise ValueError('price must be positive')
    with localcontext() as c:
        c.prec = 80
        t = int((raw_price.ln() / D('1.0001').ln()).to_integral_value(rounding=ROUND_FLOOR))
        # Decimal logarithms can land a few ulps below an exact boundary.
        while t < MAX_TICK and raw_price_at_tick(t + 1) <= raw_price:
            t += 1
        while t >= MIN_TICK and raw_price_at_tick(t) > raw_price:
            t -= 1
        if not MIN_TICK <= t <= MAX_TICK:
            raise ValueError('price outside V3 limits')
        return t


def quote_price(raw, d0, d1, weth_is_token0):
    human1_per_0 = raw * (D(10) ** (d0 - d1))
    return human1_per_0 if weth_is_token0 else D(1) / human1_per_0


def range_ticks(raw, percent, spacing):
    if spacing <= 0 or not 0 < percent < 100:
        raise ValueError('invalid range/spacing')
    # ± percent in either quote orientation: reverse endpoints when token0 is USDC.
    # Caller supplies raw lower/upper directly through range_for_quote below.
    return aligned_bounds(raw * (1 - percent / 100), raw * (1 + percent / 100), spacing)


def aligned_bounds(lo, hi, spacing):
    if spacing <= 0 or lo <= 0 or hi <= lo:
        raise ValueError('invalid bounds')
    with localcontext() as c:
        c.prec = 80
        base = D('1.0001').ln()
        l = int((lo.ln() / base / spacing).to_integral_value(rounding=ROUND_FLOOR)) * spacing
        u = int((hi.ln() / base / spacing).to_integral_value(rounding=ROUND_CEILING)) * spacing
    if l < MIN_TICK or u > MAX_TICK or l >= u:
        raise ValueError('range outside usable V3 tick limits')
    return l, u


def range_for_quote(raw, percent, spacing, weth_is_token0):
    f = percent / D(100)
    if not 0 < f < 1:
        raise ValueError('range percent must be between 0 and 100')
    if weth_is_token0:
        lo, hi = raw * (1-f), raw * (1+f)
    else:
        lo, hi = raw / (1+f), raw / (1-f)
    return aligned_bounds(lo, hi, spacing)


def range_for_offset(raw, offset, spacing):
    # USDC.e token0 (6 decimals), WETH token1 (18 decimals).
    with localcontext() as c:
        c.prec = 80
        price = D(10) ** 12 / raw
        if not offset.is_finite() or offset <= 0 or price <= offset:
            raise ValueError('invalid fixed price range')
        return aligned_bounds(D(10) ** 12 / (price + offset),
                              D(10) ** 12 / (price - offset), spacing)


def amounts(liquidity, sqrt_x96, lower, upper):
    if liquidity < 0 or lower >= upper or not MIN_TICK <= lower < upper <= MAX_TICK:
        raise ValueError('invalid liquidity or ticks')
    with localcontext() as c:
        c.prec = 80
        s = raw_price_from_sqrt(sqrt_x96).sqrt()
        a = raw_price_at_tick(lower).sqrt()
        b = raw_price_at_tick(upper).sqrt()
        x = min(max(s, a), b)
        L = D(liquidity)
        return L * (b-x) / (x*b), L * (x-a)

# Exact Uniswap V3 TickMath.getSqrtRatioAtTick, including Q96 round-up.
_TICK_FACTORS = (
    0xfffcb933bd6fad37aa2d162d1a594001, 0xfff97272373d413259a46990580e213a,
    0xfff2e50f5f656932ef12357cf3c7fdcc, 0xffe5caca7e10e4e61c3624eaa0941cd0,
    0xffcb9843d60f6159c9db58835c926644, 0xff973b41fa98c081472e6896dfb254c0,
    0xff2ea16466c96a3843ec78b326b52861, 0xfe5dee046a99a2a811c461f1969c3053,
    0xfcbe86c7900a88aedcffc83b479aa3a4, 0xf987a7253ac413176f2b074cf7815e54,
    0xf3392b0822b70005940c7a398e4b70f3, 0xe7159475a2c29b7443b29c7fa6e889d9,
    0xd097f3bdfd2022b8845ad8f792aa5825, 0xa9f746462d870fdf8a65dc1f90e061e5,
    0x70d869a156d2a1b890bb3df62baf32f7, 0x31be135f97d08fd981231505542fcfa6,
    0x9aa508b5b7a84e1c677de54f3e99bc9, 0x5d6af8dedb81196699c329225ee604,
    0x2216e584f5fa1ea926041bedfe98, 0x48a170391f7dc42444e8fa2)

def sqrt_ratio_at_tick(tick):
    if not MIN_TICK <= tick <= MAX_TICK:
        raise ValueError('tick outside V3 limits')
    abs_tick = abs(tick)
    ratio = 1 << 128
    for bit, factor in enumerate(_TICK_FACTORS):
        if abs_tick & (1 << bit):
            ratio = (ratio * factor) >> 128
    if tick > 0:
        ratio = ((1 << 256) - 1) // ratio
    return (ratio >> 32) + (1 if ratio & ((1 << 32) - 1) else 0)
