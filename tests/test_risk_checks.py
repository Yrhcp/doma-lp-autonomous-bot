import pytest
from decimal import Decimal as D
from doma_lp_bot.risk_checks import *

def test_exact_asset_fresh_independent_sources_and_depeg():
    q=[PriceObservation('A','USDC.e',97477,'0x123',D('.98'),100),PriceObservation('B','USDC.e',97477,'0x123',D('.981'),100)]
    assert independent_prices(q,expected_asset='USDC.e',expected_token='0x123',chain_id=97477,now=105,max_age=10,max_deviation=D('.01'),allowed_sources=('A','B'))==(D('.98'),D('.981'))
    with pytest.raises(RiskBlocked): independent_prices(q,expected_asset='USDC.e',expected_token='0x123',chain_id=97477,now=111,max_age=10,max_deviation=D('.01'),allowed_sources=('A','B'))
    with pytest.raises(RiskBlocked): independent_prices(q,expected_asset='USDC.e',expected_token='0xother',chain_id=97477,now=105,max_age=10,max_deviation=D('.01'),allowed_sources=('A','B'))
    q[1]=PriceObservation('B','USDC.e',97477,'0x123',D('1.03'),100)
    with pytest.raises(RiskBlocked,match='disagree'): independent_prices(q,expected_asset='USDC.e',expected_token='0x123',chain_id=97477,now=105,max_age=10,max_deviation=D('.01'),allowed_sources=('A','B'))

def test_worst_case_budget_includes_gas_and_external_costs():
    kw=dict(usdc_raw=20_000_000,weth_raw=10**16,usdc_high=D('1.01'),weth_high=D('2000'),gas_limits=[100000,250000],max_fee_per_gas=10**9,extra_fee_wei=10**12,extra_cost_usd=D('.01'),swap_input_usd=D(0))
    assert budget_upper_usd(**kw)==D('40.912')
    with pytest.raises(RiskBlocked,match='exceeds'):budget_upper_usd(**{**kw,'usdc_raw':40_000_000})
    with pytest.raises(RiskBlocked,match='missing'):budget_upper_usd(**{**kw,'extra_fee_wei':None})

def test_min_output_integer_floor():
    assert minimum_out(101,50)==100
    with pytest.raises(RiskBlocked):minimum_out(1,5000)

def test_depeg_even_if_sources_agree():
    q=[PriceObservation('A','USDC.e',97477,'0x123',D('.80'),100),PriceObservation('B','USDC.e',97477,'0x123',D('.801'),100)]
    with pytest.raises(RiskBlocked,match='depeg'):
        independent_prices(q,expected_asset='USDC.e',expected_token='0x123',chain_id=97477,now=105,max_age=10,max_deviation=D('.01'),allowed_sources=('A','B'),max_depeg=D('.05'))

def test_quote_freshness_chain_pool_amount_and_floor():
    q={'chain_id':97477,'token_in':'0x01','token_out':'0x02','pool':'0x03','amount_in_raw':100,'amount_out_raw':201,'block':100,'timestamp':1000}
    args=dict(chain_id=97477,token_in='0x01',token_out='0x02',pool='0x03',amount_in=100,current_block=102,current_time=1010,max_block_lag=3,max_age=20,slippage_bps=50)
    assert validate_swap_quote(q,**args)==199
    with pytest.raises(RiskBlocked,match='stale'):validate_swap_quote(q,**{**args,'current_time':1021})
    with pytest.raises(RiskBlocked,match='stale'):validate_swap_quote(q,**{**args,'current_block':104})
    with pytest.raises(RiskBlocked,match='mismatch'):validate_swap_quote(q,**{**args,'pool':'0x04'})
    with pytest.raises(RiskBlocked,match='mismatch'):validate_swap_quote(q,**{**args,'amount_in':101})
