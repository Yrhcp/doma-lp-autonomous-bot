from doma_lp_bot.fees import inside_growth, accrued_fee, UINT256, Q128, fee_snapshot


def test_inside_growth_all_regions_and_wrap():
    assert inside_growth(100,20,30,5,0,10)==50
    assert inside_growth(100,20,30,-1,0,10)==UINT256-10
    assert inside_growth(100,20,30,10,0,10)==10
    assert accrued_fee(20,3,UINT256-2)==0
    assert accrued_fee(9, Q128*2, 0)==18

class Call:
    def __init__(self,value): self.value=value
class Functions:
    def ticks(self,t): return Call([0,0,20 if t==1 else 30,40 if t==1 else 50,0,0,0,True])
    def feeGrowthGlobal0X128(self): return Call(Q128*100)
    def feeGrowthGlobal1X128(self): return Call(Q128*200)

def test_fee_snapshot_separates_owed():
    # growth values per tick are small raw units; use same zero previous to test exact floors.
    pool=type('P',(),{'functions':Functions()})()
    p=[0,0,0,0,0,1,10,10,0,0,300,400]
    r=fee_snapshot(lambda x:x.value,pool,p,5,(0,0),False)
    assert r['stored_owed0_raw']==300 and r['stored_owed1_raw']==400
    assert r['accrued_since_update_usdc']==999
    assert r['accrued_since_update_weth']==1999
    assert r['collectable_usdc_estimate']==1299

def test_historical_user_fee_growth_snapshot():
    L=211424728778838
    g0=731791314649436750349885518256035
    g1=380642482487463563509904051843978950498792
    outside0=730278360882683749958048992651004
    outside1=379964198395550436907646777069292663855963
    inside0=inside_growth(g0,outside0,0,199242,198855,199332)
    inside1=inside_growth(g1,outside1,0,199242,198855,199332)
    assert accrued_fee(L,inside0,0)==940030
    assert accrued_fee(L,inside1,0)==421432445840052
