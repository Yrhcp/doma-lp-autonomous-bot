from types import SimpleNamespace
import pytest
from doma_lp_bot.positions import get_position, PositionError
from doma_lp_bot.rpc import check_chain

class Call:
    def __init__(self, result): self.result=result
    def call(self, **kw):
        if isinstance(self.result, Exception): raise self.result
        return self.result
class Functions:
    def __init__(self,p,owner='0xabc',enumeration=None): self.p=p; self.owner=owner; self.enumeration=enumeration
    def balanceOf(self,w): return Call(1)
    def tokenOfOwnerByIndex(self,w,i): return Call(self.enumeration if self.enumeration is not None else 5)
    def ownerOf(self,i): return Call(self.owner)
    def positions(self,i): return Call(self.p)

def setup(p, token_id=5, owner='0xabc', enumeration=None):
    f=Functions(p,owner,enumeration)
    w3=SimpleNamespace(eth=SimpleNamespace(contract=lambda **kw:SimpleNamespace(functions=f)))
    c=SimpleNamespace(manager='m', wallet='0xabc', token_id=token_id, weth='0x1', usdc='0x2')
    return w3,c

def test_position_zero_liquidity_and_ownership():
    p=[0,0,'0x1','0x2',100,0,10,0,0,0,0,0]
    with pytest.raises(PositionError,match='zero liquidity'): get_position(*setup(p),1)
    p[7]=1
    with pytest.raises(PositionError,match='not owned'): get_position(*setup(p,owner='0xdef'),1)
    assert get_position(*setup(p),1)[1] == 5

def test_enumeration_failure():
    p=[0,0,'0x1','0x2',100,0,10,1,0,0,0,0]
    with pytest.raises(TimeoutError): get_position(*setup(p,token_id=None,enumeration=TimeoutError()),1)

def test_rpc_error_and_wrong_chain():
    w3=SimpleNamespace(eth=SimpleNamespace(chain_id=1))
    with pytest.raises(ValueError,match='wrong chain'): check_chain(w3,97477)
    class Broken:
        @property
        def chain_id(self): raise TimeoutError('offline')
    with pytest.raises(TimeoutError): check_chain(SimpleNamespace(eth=Broken()),97477)

def test_snapshot_call_budget_interrupts_enumeration():
    p=[0,0,'0x1','0x2',100,0,10,1,0,0,0,0]
    w3,c=setup(p,token_id=None)
    def expired(): raise TimeoutError('budget exceeded')
    with pytest.raises(TimeoutError,match='budget exceeded'):
        get_position(w3,c,1,expired)

def test_http_provider_has_no_hidden_internal_retry():
    from doma_lp_bot.rpc import connect
    c=SimpleNamespace(rpc='http://127.0.0.1:1',timeout=1)
    assert connect(c).provider.exception_retry_configuration is None


def test_protected_nft_not_selected_automatically():
    p=[0,0,'0x1','0x2',100,0,10,1,0,0,0,0]
    w,c=setup(p,token_id=None,enumeration=900001)
    with pytest.raises(PositionError,match='found 0'):
        get_position(w,c,1)
    # Explicit monitoring is a separate user choice, not automatic selection.
    w,c=setup(p,token_id=900001)
    assert get_position(w,c,1)[1]==900001
