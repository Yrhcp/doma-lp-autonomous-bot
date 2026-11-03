import pytest
from doma_lp_bot.calldata_lab import *
from doma_lp_bot.preflight import prepare_mint_unsigned,PreparationBlocked
W='0x0000000000000000000000000000000000000001'
A='0x0000000000000000000000000000000000000002'
B='0x0000000000000000000000000000000000000003'

def test_encode_and_decode_bounded_approval():
    x=approval_calldata(chain=31337,spender=W,amount=50,ceiling=50)
    assert x[:10]=='0x095ea7b3'
    with pytest.raises(ValueError):approval_calldata(chain=31337,spender=W,amount=MAX_UINT256,ceiling=MAX_UINT256)
    with pytest.raises(ValueError):approval_calldata(chain=97477,spender=W,amount=50,ceiling=50)

def test_encode_decode_mint_and_reject_wrong_recipient_or_deadline():
    kw=dict(chain=31337,wallet=W,token0=A,token1=B,fee=100,lower=-10,upper=10,spacing=1,amount0=100,amount1=100,min0=90,min1=90,deadline=200,now=100)
    assert mint_calldata(**kw)[:10]=='0x88316456'
    with pytest.raises(ValueError):mint_calldata(**{**kw,'chain':97477})
    with pytest.raises(ValueError):mint_calldata(**{**kw,'deadline':100})
    with pytest.raises(ValueError):mint_calldata(**{**kw,'min0':0})
    with pytest.raises(ValueError):mint_calldata(**{**kw,'token0':B,'token1':A})

def test_encode_decode_swap_with_guarded_minimum():
    kw=dict(chain=31337,wallet=W,token_in=A,token_out=B,fee=100,amount_in=100,min_out=90,deadline=200,now=100)
    assert swap_calldata(**kw)[:10]=='0x3593564c'
    with pytest.raises(ValueError):swap_calldata(**{**kw,'min_out':0})
    with pytest.raises(ValueError):swap_calldata(**{**kw,'chain':97477})
    with pytest.raises(PreparationBlocked):prepare_mint_unsigned({'status':'READY_VERIFIED','blockers':[]})

def test_simulation_only_allows_lab_chain_and_eth_call():
    class Eth:
        chain_id=31337
        def call(self,tx):
            assert tx['from']==W and tx['to']==A and tx['value']==0
            return b'checked'
    obj=type('Provider',(),{'eth':Eth()})()
    assert simulate_call(obj,sender=W,target=A,data='0x1234')==b'checked'
    obj.eth.chain_id=97477
    with pytest.raises(ValueError):simulate_call(obj,sender=W,target=A,data='0x1234')
