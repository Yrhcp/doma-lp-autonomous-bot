from doma_lp_bot.strategy import SignalGate

def test_unlimited_cycles_still_require_fresh_confirmation_and_position_change():
    g = SignalGate(60, 0, None, max_gap=90)
    block = 0
    for cycle in range(5):
        identity = (cycle, 'pool', 0, 10, 1)
        block += 1
        assert not g.observe(False, cycle*90, block, 'below', identity, False)
        block += 1
        assert g.observe(False, cycle*90+60, block, 'below', identity, False)
        g.record(cycle*90+60)

def test_exit_confirmation_cooldown_rate_and_reset():
    g = SignalGate(60,120,2)
    assert not g.observe(False,0,1,'below')
    assert not g.observe(False,59,2,'below')
    assert g.observe(False,60,3,'below'); g.record(60)
    assert not g.observe(False,80,4,'below')
    assert not g.observe(False,150,5,'below')
    assert not g.observe(False,180,6,'below')  # same episode stays latched
    assert not g.observe(True,181,7)
    assert not g.observe(False,190,8,'above')
    assert g.observe(False,250,9,'above'); g.record(250)
    assert not g.observe(True,251,10)
    assert not g.observe(False,260,11,'below')
    assert not g.observe(False,320,12,'below')  # 24h limit
    assert not g.observe(True,321,13)
    assert not g.observe(False,86500,14,'above')
    assert g.observe(False,86560,15,'above')

def test_stale_and_errors_reset_confirmation():
    g = SignalGate(60,0,3)
    assert not g.observe(False,0,1)
    assert not g.observe(False,60,1)
    g.reset_observation()
    assert not g.observe(False,120,2)
    assert g.observe(False,180,3)

def test_exit_buffer_and_boundary():
    from decimal import Decimal as D
    from doma_lp_bot.strategy import buffered_exit
    s={'price_usdc_per_weth':D('99.95'),'lower_price':D(100),'upper_price':D(110),'status':'OUT_OF_RANGE'}
    assert buffered_exit(s,D('.1')) is None
    s['price_usdc_per_weth']=D('99.9')
    assert buffered_exit(s,D('.1'))=='below'
    s['price_usdc_per_weth']=D('110.11')
    assert buffered_exit(s,D('.1'))=='above'
    s['price_usdc_per_weth']=D('110')
    assert buffered_exit(s,D(0))=='above'
    s['status']='IN_RANGE'
    assert buffered_exit(s,D(0)) is None

def test_gap_reorg_and_opposite_exit_reset():
    g=SignalGate(60,0,3,max_gap=70)
    assert not g.observe(False,0,1,'below')
    assert not g.observe(False,80,2,'below')
    assert not g.observe(False,110,3,'above')
    assert not g.observe(False,140,3,'above')
    assert not g.observe(False,170,4,'above')
    assert g.observe(False,200,5,'above')

def test_latch_survives_gap_and_buffer_return_until_in_range():
    g=SignalGate(60,0,3,max_gap=90)
    ident=(1,'pool',0,10,1)
    assert not g.observe(False,0,1,'below',ident,False)
    assert g.observe(False,60,2,'below',ident,False); g.record(60)
    g.reset_observation()
    assert not g.observe(True,100,3,None,ident,False)  # still outside, inside buffer
    assert not g.observe(False,160,4,'below',ident,False)
    assert not g.observe(False,220,5,'below',ident,False)
    assert not g.observe(True,230,6,None,ident,True)
    assert not g.observe(False,240,7,'below',ident,False)
    assert g.observe(False,300,8,'below',ident,False)

def test_latch_opposite_jump_is_new_episode():
    g=SignalGate(60,0,3)
    identity=(1,'p',0,10,1)
    assert not g.observe(False,0,1,'below',identity,False)
    assert g.observe(False,60,2,'below',identity,False); g.record(60)
    assert not g.observe(False,75,3,'above',identity,False)
    assert not g.observe(False,120,4,'above',identity,False)
    assert g.observe(False,135,5,'above',identity,False)
