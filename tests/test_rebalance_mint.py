from types import SimpleNamespace
from decimal import Decimal
import pytest
from web3 import Web3
from doma_lp_bot import rebalance_mint as m
from doma_lp_bot.manual_open import Blocked
from doma_lp_bot.v3_math import sqrt_ratio_at_tick, raw_price_from_sqrt, raw_price_at_tick, quote_price

WALLET=Web3.to_checksum_address('0x2222222222222222222222222222222222222222')

def test_mint_uses_only_collected_tokens_and_binds_recipient():
    cfg=SimpleNamespace(wallet=WALLET);amounts=[2300000,1000000000000000]
    s={'sqrt':sqrt_ratio_at_tick(199065),'balances':[50000000,10**18],'nonce':7}
    data,details=m.mint_data(cfg,s,amounts)
    record={'step':'mint_new','chainId':97477,'from':WALLET,'to':m.MANAGER_ADDRESS,'value':0,'nonce':7,'data':data,'details':details}
    p=m.check_record(cfg,s,record,amounts)
    assert (p['amount0Desired'],p['amount1Desired'])==tuple(amounts)
    assert p['recipient']==WALLET and p['amount0Min']>0 and p['amount1Min']>0
    with pytest.raises(Blocked):m.check_record(cfg,s,{**record,'from':m.USDC},amounts)
    with pytest.raises(Blocked):m.check_record(cfg,{**s,'balances':[1,1]},record,amounts)
    with pytest.raises(Blocked):m.check_record(cfg,s,record,[amounts[0]-1,amounts[1]])


def test_swap_required_blocks_mint_and_position_900001_not_addressed():
    s={'sqrt':sqrt_ratio_at_tick(199065),'balances':[10000000,10**18]}
    cfg=SimpleNamespace(wallet=WALLET)
    with pytest.raises(Blocked,match='требуется swap'):m.mint_data(cfg,s,[2300000,1020000000000000])
    with pytest.raises(Blocked):m.available([{'call':{'step':'decrease'}},{'call':{'step':'collect'},'received_raw':[0,-1]}])


def test_mint_approval_is_exact():
    cfg=SimpleNamespace(wallet=WALLET);budget=[2300000,1000000000000000]
    s={'sqrt':sqrt_ratio_at_tick(199065),'balances':budget,'nonce':7}
    c=Web3().eth.contract(abi=m.APPROVE_ABI)
    def rec(n):return {'step':'approve_mint_usdc','chainId':97477,'from':WALLET,'to':m.USDC,'value':0,'nonce':7,'data':c.encode_abi('approve',args=[m.MANAGER_ADDRESS,n])}
    m.check_record(cfg,s,rec(budget[0]),budget)
    with pytest.raises(Blocked):m.check_record(cfg,s,rec(2**256-1),budget)


def test_mint_autonomous_range_is_point_three_percent_and_bound_to_check():
    cfg=SimpleNamespace(wallet=WALLET,range_percent=Decimal('0.3'))
    budget=[2300000,1000000000000000]
    s={'sqrt':sqrt_ratio_at_tick(199065),'balances':budget,'nonce':7}
    data,details=m.mint_data(cfg,s,budget,after_swap=True)
    record={'step':'mint_new','chainId':97477,'from':WALLET,'to':m.MANAGER_ADDRESS,
            'value':0,'nonce':7,'data':data,'details':details}
    p=m.check_record(cfg,s,record,budget,after_swap=True)
    spot=quote_price(raw_price_from_sqrt(s['sqrt']),6,18,False)
    lo,hi=sorted(quote_price(raw_price_at_tick(t),6,18,False) for t in (p['tickLower'],p['tickUpper']))
    assert Decimal('0.003') <= 1-lo/spot < Decimal('0.00311')
    assert Decimal('0.003') <= hi/spot-1 < Decimal('0.00311')
    with pytest.raises(Blocked):
        m.check_record(SimpleNamespace(wallet=WALLET,range_percent=Decimal('0.7')),s,record,budget,after_swap=True)
