from types import SimpleNamespace
import json
import pytest
from doma_lp_bot import compare_positions as cp

def snap(token=1,fee=100,block=1,t=100,status='IN_RANGE'):
    return {'token_id':token,'pool':cp.EXPECTED_POOLS[fee],'factory':cp.FACTORY_ADDRESS,'fee':fee,'lower':10,'upper':20,'liquidity':1000,
            'fee_growth_last0_x128':4,'fee_growth_last1_x128':5,'tokens_owed0_raw':0,'tokens_owed1_raw':0,
            'block':block,'block_timestamp':t,'status':status,'usdc':'2','weth':'0.001','price_usdc_per_weth':'2000',
            'fees':{'status':'estimated_from_fee_growth','accrued_since_update_weth':'0.001','accrued_since_update_usdc':'0.02'}}

def test_id_restrictions():
    for ids in ([1,1],[900001,2],[0,2]):
        with pytest.raises(ValueError):cp.validate_ids(ids)

def test_observations_fees_principal_and_unknown_cost():
    a=snap();o=cp.position_observation(a,max_gap=30)
    b=snap(block=2,t=115);b['fees']['accrued_since_update_usdc']='0.03';b['price_usdc_per_weth']='2200'
    o=cp.position_observation(b,o,max_gap=30);r=cp.present(b,o)
    assert r['fees_since_first_observation_estimate']=={'weth':'0.000','usdc_e':'0.01'}
    assert r['estimated_in_range_seconds_between_matching_samples']==15
    assert r['principal_change_since_first_observation_usdc_quote']=='0.200'
    assert r['net_roi_percent']=='UNKNOWN' and r['realized_fees_since_mint']=='UNKNOWN'

def test_changes_gaps_and_restart_stop_false_fee_accounting():
    a=snap();o=cp.position_observation(a,max_gap=30)
    b=snap(block=2,t=115);b['tokens_owed0_raw']=2
    o=cp.position_observation(b,o,max_gap=30)
    assert cp.present(b,o)['fees_since_first_observation_estimate']=='UNKNOWN'
    assert o['estimated_in_range_seconds']==0
    c=snap(block=3,t=200);o=cp.position_observation(c,o,max_gap=30,broken=True)
    assert o['gaps']==2 and not o['fees_complete']

def test_cli_collection_separate_durable_state_and_restart(tmp_path,monkeypatch,capsys):
    cfg=SimpleNamespace(log=str(tmp_path/'monitor.jsonl'),chain=97477,wallet='0x123',manager=cp.MANAGER_ADDRESS,weth=cp.WETH,usdc=cp.USDC,max_observation_gap=30,poll=15,rpc='https://private')
    # dataclasses.replace requires a real dataclass
    from dataclasses import make_dataclass
    C=make_dataclass('C',[(k,type(v)) for k,v in vars(cfg).items()]+[('token_id',int)])
    cfg=C(**vars(cfg),token_id=0);counter={'b':1}
    monkeypatch.setattr(cp,'connect',lambda cfg:object())
    monkeypatch.setattr(cp,'snapshot',lambda w,cfg:snap(cfg.token_id,100 if cfg.token_id==1 else 500,counter['b'],100+15*(counter['b']-1)))
    assert cp.run(cfg,[1,2])==0
    assert not (tmp_path/'monitor.jsonl.state.json').exists()
    counter['b']=2
    assert cp.run(cfg,[1,2])==0
    output=[json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(output)==2 and all(p['fees_since_first_observation_estimate']=='UNKNOWN' for p in output[1]['positions'])
    assert all(p['estimated_in_range_seconds_between_matching_samples']==0 for p in output[1]['positions'])
