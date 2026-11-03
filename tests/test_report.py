from decimal import Decimal as D
from types import SimpleNamespace
import json
from doma_lp_bot.report import analyze,render

C=SimpleNamespace(chain=97477,wallet='0xabc',manager='0xdef',max_observation_gap=90)
def snap(second, status='IN_RANGE', fee='1', token=1, block=None, baseline=0):
    return {'event':'snapshot','time':f'2026-10-30T10:00:{second:02d}+00:00','chain':97477,'wallet':'0xabc','manager':'0xdef',
            'token_id':token,'pool':'pool','lower':1,'upper':10,'liquidity':100,'status':status,'block':block if block is not None else second+1,
            'price_usdc_per_weth':'2000','weth':'0.1','usdc':'30','fee_growth_last0_x128':baseline,'fee_growth_last1_x128':baseline,
            'tokens_owed0_raw':0,'tokens_owed1_raw':0,
            'fees':{'status':'estimated_from_fee_growth','accrued_since_update_weth':fee,'accrued_since_update_usdc':fee},
            'simulation':{'strategy_a':{'capital_utilization_before_costs_percent':'100'},'strategy_b':{'capital_utilization_before_costs_percent':'50'}}}
def write(p,rows): p.write_text(''.join(json.dumps(x)+'\n' for x in rows))

def test_report_aggregates_without_summing_fee_snapshots(tmp_path):
    p=tmp_path/'x.jsonl'; rows=[snap(0,'OUT_OF_RANGE','1'),snap(15,'OUT_OF_RANGE','2'),snap(30,'IN_RANGE','3')]
    write(p,rows); r=analyze(p,C)
    assert r['snapshots']==3 and r['price_sample_mean']==2000
    assert r['out_of_range_count']==2 and r['out_of_range_periods'][0]['covered_seconds']==15
    assert r['fees_change_estimate']=={'weth':D(2),'usdc':D(2)}
    assert r['principal_change_usdc']==0
    assert r['strategy_b_utilization_before_costs']['mean']==50

def test_report_gap_fee_reset_missing_rows(tmp_path):
    p=tmp_path/'x.jsonl'; rows=[snap(0,'OUT_OF_RANGE'),{'event':'error','error_category':'rpc','time':'2026-10-30T10:00:05+00:00','chain':97477,'wallet':'0xabc','manager':'0xdef'},
       {'event':'gap','time':'2026-10-30T10:00:10+00:00','chain':97477,'wallet':'0xabc','manager':'0xdef'},snap(15,'OUT_OF_RANGE',baseline=8)]
    write(p,rows); p.write_text(p.read_text()+'{bad\n')
    r=analyze(p,C)
    assert r['fees_change_estimate'] is None and r['rpc_errors']==1 and r['recorded_gaps']==1
    assert len(r['out_of_range_periods'])==2 and all(x['covered_seconds']==0 for x in r['out_of_range_periods'])
    assert any('Повреждённых строк' in w for w in r['warnings'])
    assert 'UNKNOWN' in render(r)

def test_report_nft_switch_or_unknown_fee(tmp_path):
    p=tmp_path/'x.jsonl'; write(p,[snap(0),snap(15,token=2)])
    r=analyze(p,C)
    assert r['principal_change_usdc'] is None and r['fees_change_estimate'] is None and r['inferred_gaps']==1
    missing=analyze(tmp_path/'absent',C)
    assert missing['snapshots']==0 and missing['price_min'] is None

def test_collect_or_missing_baseline_is_unknown(tmp_path):
    p=tmp_path/'x.jsonl'; a=snap(0); b=snap(15,fee='0',baseline=7)
    write(p,[a,b]); assert analyze(p,C)['fees_change_estimate'] is None
    del b['fee_growth_last0_x128'];write(p,[a,b]); assert analyze(p,C)['fees_change_estimate'] is None

def test_unobserved_cross_range_splits_periods(tmp_path):
    a=snap(0,'OUT_OF_RANGE'); a.update(price_usdc_per_weth='90',lower_price='100',upper_price='110')
    b=snap(15,'OUT_OF_RANGE');b.update(price_usdc_per_weth='120',lower_price='100',upper_price='110')
    p=tmp_path/'x.jsonl';write(p,[a,b]);r=analyze(p,C)
    assert len(r['out_of_range_periods'])==2
    assert all(x['covered_seconds']==0 for x in r['out_of_range_periods'])

def test_latest_watch_filters_prior_and_later_history(tmp_path):
    p=tmp_path/'x.jsonl'
    def event(name,t,**extra): return {'event':name,'time':t,'chain':97477,'wallet':'0xabc','manager':'0xdef',**extra}
    start=event('watch_start','2026-10-30T09:59:59+00:00',watch_id='abc')
    end=event('watch_complete','2026-10-30T10:00:20+00:00',watch_id='abc')
    earlier=snap(0); earlier['time']='2026-10-30T09:59:50+00:00'
    later=snap(30); write(p,[earlier,start,snap(0),snap(15),end,later])
    r=analyze(p,C,latest_watch=True)
    assert r['scope']=='latest_watch' and r['snapshots']==2 and r['elapsed_seconds']==21
    assert analyze(p,C)['snapshots']==4

def test_partial_simulation_not_counted_as_valid_pair(tmp_path):
    p=tmp_path/'x.jsonl'; s=snap(0)
    del s['simulation']['strategy_b']
    write(p,[s]); r=analyze(p,C)
    assert r['strategy_a_utilization_before_costs'] is None
    assert r['strategy_b_utilization_before_costs'] is None
