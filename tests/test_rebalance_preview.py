from decimal import Decimal
from types import SimpleNamespace
import pytest

from doma_lp_bot import rebalance_preview as p
from doma_lp_bot.rabby_local import PreviewSession

def test_only_authorized_nft():
    p.require_position(900003)
    for token in (900001, 71187, '900003', True):
        with pytest.raises(ValueError): p.require_position(token)

def test_indicative_swap_never_exceeds_position_amounts():
    for a,b in ((100,0),(0,100),(100,200),(100,100)):
        token,_,amount=p.estimate_swap(a,b,50,50,Decimal(1))
        assert 0<=amount<=(a if token==p.USDC else b)
    with pytest.raises(ValueError):p.estimate_swap(-1,1,1,1,Decimal(1))

def test_preview_never_enables_transaction_actions():
    s=PreviewSession.__new__(PreviewSession)
    for action,args in ((s.arm,()),(s.submit,('0x00',)),(s.cancel,())):
        with pytest.raises(ValueError,match='отключена'):action(*args)

def test_wrong_nft_fails_before_rpc():
    with pytest.raises(ValueError,match='MANUAL_INITIAL_NFT'):
        p.preview(None,SimpleNamespace(),900001)
