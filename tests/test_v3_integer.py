import pytest
from doma_lp_bot.v3_integer import *


def test_mint_rounding_historical_event():
    # Verified Doma mint in block 17686398; raw event amounts round UP.
    sqrt=1672612593672580183218149581881043
    a,b=ticks_for_pool(198855,199332,1)
    liq=211424728778838
    assert amounts_for_liquidity(sqrt,a,b,liq,True)==(85123426,67826849351677594)
    floor=amounts_for_liquidity(sqrt,a,b,liq,False)
    assert floor==(85123425,67826849351677593)
    obtained=liquidity_for_amounts(sqrt,a,b,85123426,67826849351677594)
    assert obtained>=liq and obtained-liq<3000000

def test_boundaries_rounding_and_zero():
    a,b=ticks_for_pool(-100,100,10)
    l=10**12
    assert amounts_for_liquidity(a,a,b,l,True)[1]==0
    assert amounts_for_liquidity(b,a,b,l,True)[0]==0
    for price in (a,Q96,b):
        down=amounts_for_liquidity(price,a,b,l,False)
        up=amounts_for_liquidity(price,a,b,l,True)
        assert all(0<=x-y<=1 for x,y in zip(up,down))
    assert amounts_for_liquidity(Q96,a,b,0,True)==(0,0)
    assert liquidity_for_amounts(Q96,a,b,0,1)==0

def test_invalid_spacing_and_overflow():
    with pytest.raises(ValueError): ticks_for_pool(-11,20,10)
    with pytest.raises(ValueError): liquidity_for_amounts(0,1,2,1,1)
    with pytest.raises(OverflowError): liquidity_for_amount1(1,2,MAX_UINT256)
