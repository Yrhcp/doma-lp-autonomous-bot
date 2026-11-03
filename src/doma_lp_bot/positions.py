from .protected import PROTECTED_NFTS
from .contracts import MANAGER
from requests.exceptions import RequestException
from web3.exceptions import Web3RPCError

class PositionError(ValueError): pass

def get_position(w3, cfg, block, check_deadline=lambda: None):
    manager = w3.eth.contract(address=cfg.manager, abi=MANAGER)
    def call(f):
        check_deadline()
        return f.call(block_identifier=block)
    hint = None
    if getattr(cfg, 'log', None):
        from .rebalance_manual import journal_active_hint
        hint = journal_active_hint(cfg)
    if hint is not None and cfg.token_id is not None and cfg.token_id != hint:
        raise PositionError('configured NFT differs from active rebalance journal')
    if cfg.token_id is not None:
        ids = [cfg.token_id]
    else:
        # Prefer the last confirmed rebalance journal, when available. The
        # position is still checked on-chain below; never auto-select protected positions.
        if hint is not None:
            ids = [hint]
        else:
            n = call(manager.functions.balanceOf(cfg.wallet))
            if n > 1000:
                raise PositionError('too many NFTs to enumerate; set POSITION_TOKEN_ID')
            try:
                ids = [call(manager.functions.tokenOfOwnerByIndex(cfg.wallet, i)) for i in range(n)]
            except (TimeoutError, RequestException, Web3RPCError):
                raise
            except Exception as e:
                raise PositionError('NFT enumeration failed; set POSITION_TOKEN_ID') from e
    matches = []
    for token_id in ids:
        if cfg.token_id is None and token_id in PROTECTED_NFTS: continue
        p = call(manager.functions.positions(token_id))
        if {p[2].lower(),p[3].lower()} != {cfg.weth.lower(),cfg.usdc.lower()}:
            if cfg.token_id is not None: raise PositionError('NFT does not match configured WETH/USDC addresses')
            continue
        if p[7] == 0:
            if cfg.token_id is not None: raise PositionError(f'NFT {token_id} has zero liquidity')
            continue
        if call(manager.functions.ownerOf(token_id)).lower() != cfg.wallet.lower():
            if cfg.token_id is not None: raise PositionError(f'NFT {token_id} is not owned by wallet')
            continue
        matches.append((token_id,p))
    if len(matches) != 1:
        raise PositionError(f'found {len(matches)} active matching NFTs; set POSITION_TOKEN_ID if ambiguous')
    return manager, *matches[0]
