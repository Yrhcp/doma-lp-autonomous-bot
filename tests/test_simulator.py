from decimal import Decimal as D
from doma_lp_bot.v3_math import raw_price_from_sqrt
from doma_lp_bot.simulator import simulate

def test_simulation_range_and_unknown_costs():
    s={'raw_price': raw_price_from_sqrt(2**96), 'sqrt':2**96, 'spacing':10,'weth_is_token0':True,'d0':18,'d1':18,'liquidity':10**20,'lower':-100,'upper':100,'fee':500}
    r=simulate(s,D(1))
    assert r['new_lower_tick'] % 10 == r['new_upper_tick'] % 10 == 0
    assert D('.4762') < r['strategy_a']['target_weth_value_fraction'] < D('.4763')
    assert r['strategy_a']['gas_eth_assumption'] is None and r['strategy_a']['router_fee_usdc_assumption'] is None
    assert r['strategy_a']['pool_fee_usdc_assuming_position_pool'] >= 0

def test_simulation_asymmetric_range_not_forced_half():
    s={'raw_price':raw_price_from_sqrt(2**96),'sqrt':2**96,'spacing':200,'weth_is_token0':True,'d0':18,'d1':18,'liquidity':10**20,'lower':-300,'upper':300,'fee':100}
    r=simulate(s,D('0.1'))
    assert r['strategy_a']['target_weth_value_fraction'] != D('.5')

def test_no_swap_conserves_assets_and_limiting_asset():
    s={'raw_price': raw_price_from_sqrt(2**96), 'sqrt':2**96,'spacing':10,'weth_is_token0':True,'d0':18,'d1':18,'liquidity':10**20,'lower':100,'upper':200,'fee':500}
    r=simulate(s,D('0.7')); b=r['strategy_b']; a=r['strategy_a']
    assert b['capital_utilization_before_costs_percent']==0
    assert b['deposit_usdc']==b['deposit_weth']==0
    assert b['leftover_weth']==r['withdraw_weth'] and b['leftover_usdc']==0
    assert a['capital_utilization_before_costs_percent']==100 and a['actual_utilization_after_costs']=='UNKNOWN'
    assert a['swap_input_weth']>0

def test_no_swap_balanced_conservation_both_orientations():
    for w0 in (True,False):
        s={'raw_price':raw_price_from_sqrt(2**96),'sqrt':2**96,'spacing':10,'weth_is_token0':w0,'d0':18,'d1':18,'liquidity':10**20,'lower':-100,'upper':100,'fee':100}
        r=simulate(s,D('0.7')); b=r['strategy_b']
        assert abs((b['deposit_weth']+b['leftover_weth'])-r['withdraw_weth']) < D('1e-27')
        assert abs((b['deposit_usdc']+b['leftover_usdc'])-r['withdraw_usdc']) < D('1e-27')
        assert 0 <= b['capital_utilization_before_costs_percent'] <= 100
