from decimal import Decimal as D, localcontext
from .v3_math import amounts, range_for_quote, raw_price_at_tick, quote_price


def simulate(snapshot, percent, gas_units_a=None, gas_units_b=None, gas_price=None, router_bps=None, slippage_bps=None, impact_bps=None):
    """Compare indicative spot-only deposits; fees are excluded from available inventory."""
    with localcontext() as ctx:
        ctx.prec = 70
        raw=snapshot['raw_price']; sqrt=snapshot['sqrt']; spacing=snapshot['spacing']
        w0=snapshot['weth_is_token0']; d0=snapshot['d0']; d1=snapshot['d1']
        lo,hi=range_for_quote(raw,percent,spacing,w0)
        a0,a1=amounts(snapshot['liquidity'],sqrt,snapshot['lower'],snapshot['upper'])
        unit0,unit1=amounts(10**18,sqrt,lo,hi)
        price=quote_price(raw,d0,d1,w0)
        usdc = a1/D(10)**d1 if w0 else a0/D(10)**d0
        weth = a0/D(10)**d0 if w0 else a1/D(10)**d1
        unit_u = unit1/D(10)**d1 if w0 else unit0/D(10)**d0
        unit_w = unit0/D(10)**d0 if w0 else unit1/D(10)**d1
        if unit_u <= 0 or unit_w <= 0: raise ValueError('new range does not contain price strictly')
        total=usdc+weth*price
        if total <= 0: raise ValueError('zero withdrawal value')
        fraction_w=unit_w*price/(unit_u+unit_w*price)
        delta=total*fraction_w-weth*price
        swap_value=abs(delta)
        b_scale=min(usdc/unit_u,weth/unit_w)
        b_usdc=unit_u*b_scale; b_weth=unit_w*b_scale
        b_left_u=max(D(0),usdc-b_usdc); b_left_w=max(D(0),weth-b_weth)
        def gas(units):
            return None if units is None or gas_price is None else D(units)*D(gas_price)/D(10)**18
        def bps(value): return None if value is None else swap_value*D(value)/10000
        agas,bgas=gas(gas_units_a),gas(gas_units_b)
        prices=sorted(quote_price(raw_price_at_tick(t),d0,d1,w0) for t in (lo,hi))
        return {'new_lower_tick':lo,'new_upper_tick':hi,'new_lower_price':prices[0],'new_upper_price':prices[1],
            'withdraw_weth':weth,'withdraw_usdc':usdc,'withdraw_value_usdc':total,
            'strategy_a':{'label':'swap; ideal spot before costs', 'target_weth_value_fraction':fraction_w,
                'swap_direction':'USDC.e -> WETH' if delta>0 else 'WETH -> USDC.e' if delta<0 else 'none',
                'swap_input_weth':swap_value/price if delta<0 else D(0), 'swap_input_usdc':swap_value if delta>0 else D(0),
                'deposit_weth_ideal':total*fraction_w/price,'deposit_usdc_ideal':total*(1-fraction_w),
                'capital_utilization_before_costs_percent':D(100),'leftover_weth_ideal':D(0),'leftover_usdc_ideal':D(0),
                'actual_utilization_after_costs':'UNKNOWN','pool_fee_usdc_assuming_position_pool':swap_value*D(snapshot['fee'])/D(10**6),
                'router_fee_usdc_assumption':bps(router_bps),'slippage_usdc_assumption':bps(slippage_bps),
                'price_impact_usdc_assumption':bps(impact_bps),'gas_eth_assumption':agas,
                'gas_usdc_assumption':None if agas is None else agas*price,'total_cost':'UNKNOWN'},
            'strategy_b':{'label':'no swap', 'deposit_weth':b_weth,'deposit_usdc':b_usdc,
                'capital_utilization_before_costs_percent':(b_usdc+b_weth*price)*100/total,
                'leftover_weth':b_left_w,'leftover_usdc':b_left_u,'swap_pool_fee_usdc':D(0),
                'swap_router_fee_usdc':D(0),'gas_eth_assumption':bgas,
                'gas_usdc_assumption':None if bgas is None else bgas*price,'total_cost':'UNKNOWN'},
            'note':'Spot-only, before costs; excludes accrued fees, wallet balances and price movement. No quote or executable plan. Slippage and impact assumptions may overlap.'}
