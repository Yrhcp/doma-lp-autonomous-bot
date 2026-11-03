from decimal import Decimal as D
import pytest
from doma_lp_bot.v3_math import *

def test_sqrt_price_and_orientation():
    assert raw_price_from_sqrt(2**96) == 1
    assert quote_price(D(4), 6, 18, True) == D('4e-12')
    assert quote_price(D(4), 6, 18, False) == D('2.5e11')
    with pytest.raises(ValueError): raw_price_from_sqrt(0)

@pytest.mark.parametrize('tick', [-200000,-1,0,1,198855,199332])
def test_price_tick(tick):
    assert price_to_tick(raw_price_at_tick(tick)) == tick
    assert price_to_tick(raw_price_at_tick(tick) * D('1.00001')) == tick

def test_spacing_and_quote_direction():
    raw = D('443000000')
    for w0 in (True,False):
        lo,hi = range_for_quote(raw,D(1),10,w0)
        assert lo % 10 == hi % 10 == 0
        assert raw_price_at_tick(lo) <= (raw*D('.99') if w0 else raw/D('1.01'))
        assert raw_price_at_tick(hi) >= (raw*D('1.01') if w0 else raw/D('.99'))
    lo,hi=aligned_bounds(D('1.0001')**-21,D('1.0001')**-9,10)
    assert (lo,hi)==(-30,0)

def test_amounts_boundaries_and_zero():
    a,b=amounts(10**10,2**96,-100,100)
    assert abs(a-b) < D('1e-17')
    assert amounts(0,2**96,-100,100)==(0,0)
    assert amounts(10**10,2**96,100,200)[1]==0
    assert amounts(10**10,2**96,-200,-100)[0]==0


def test_historical_mint_amounts_from_chain():
    # Block 17686398, pool slot0 and mint event; mint rounds up raw amounts.
    a,b=amounts(211424728778838,1672612593672580183218149581881043,198855,199332)
    assert 0 <= D(85123426)-a < 1
    assert 0 <= D(67826849351677594)-b < 1

def test_tickmath_exact_boundaries_and_midpoint():
    assert sqrt_ratio_at_tick(0)==2**96
    assert sqrt_ratio_at_tick(-887272)==4295128739
    assert sqrt_ratio_at_tick(887272)==1461446703485210103287273052203988822378723970342
    for t in [-887272,-198855,-1,0,1,198855,887271]:
        assert sqrt_ratio_at_tick(t)<sqrt_ratio_at_tick(t+1)

def test_position_status_exact_tick_boundaries():
    from doma_lp_bot.pool import position_status
    assert position_status(-1,0,100)=='OUT_OF_RANGE'
    assert position_status(0,0,100)=='IN_RANGE'
    assert position_status(99,0,100)=='IN_RANGE'
    assert position_status(100,0,100)=='OUT_OF_RANGE'

def test_historical_distance_denominators():
    from doma_lp_bot.monitor import distances
    result=distances(D('2225.632750'),D('2205.738626'),D('2313.496567'))
    assert round(result['from_lower_boundary_percent'],4)==D('0.9019')
    assert round(result['to_upper_boundary_percent'],4)==D('3.7979')
    assert round(result['rise_to_upper_from_current_percent'],4)==D('3.9478')
    assert round(result['fall_to_lower_from_current_percent'],4)==D('0.8939')

def test_range_ticks_reverse_price_and_outside_amounts():
    raw=D('443000000'); percent=D('.7')
    for orientation in (False,True):
        lo,hi=range_for_quote(raw,percent,1,orientation)
        mid=quote_price(raw,6,18,orientation)
        bounds=sorted(quote_price(raw_price_at_tick(t),6,18,orientation) for t in (lo,hi))
        assert bounds[0] <= mid*D('.993') and bounds[1] >= mid*D('1.007')
    assert amounts(0,2**96,-10,10)==(0,0)
    assert amounts(123456,2**96,10,20)[1]==0
    assert amounts(123456,2**96,-20,-10)[0]==0

def test_historical_user_snapshot_price_amounts_and_liquidity():
    from decimal import localcontext
    sqrt=1679394907977389280927680335459309
    liquidity=211424728778838
    assert abs(quote_price(raw_price_from_sqrt(sqrt),6,18,False)-D('2225.632750')) < D('0.000001')
    a,b=amounts(liquidity,sqrt,198855,199332)
    assert abs(a/D(10**6)-D('44.678403')) < D('0.000001')
    assert abs(b/D(10**18)-D('0.085925829776')) < D('0.000000000001')
    with localcontext() as ctx:
        ctx.prec=70
        current=D(sqrt)/D(2**96); lo=raw_price_at_tick(198855).sqrt(); hi=raw_price_at_tick(199332).sqrt()
        # Independently invert the two token-amount formulas for liquidity.
        assert abs(a*current*hi/(hi-current)-liquidity) < D('0.000000000001')
        assert abs(b/(current-lo)-liquidity) < D('0.000000000001')
