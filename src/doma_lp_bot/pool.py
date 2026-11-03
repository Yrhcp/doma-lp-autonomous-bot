import time
from decimal import Decimal as D
from .contracts import FACTORY, POOL, TOKEN
from .positions import get_position
from .fees import fee_snapshot
from .rpc import check_chain
from .v3_math import amounts, raw_price_from_sqrt, quote_price, raw_price_at_tick, sqrt_ratio_at_tick

def position_status(tick, lower, upper):
    if lower >= upper: raise ValueError('invalid position bounds')
    return 'IN_RANGE' if lower <= tick < upper else 'OUT_OF_RANGE'

def snapshot(w3, cfg):
    deadline = time.monotonic() + cfg.snapshot_timeout
    def check_deadline():
        if time.monotonic() >= deadline: raise TimeoutError("snapshot time budget exceeded")
    check_deadline()
    check_chain(w3, cfg.chain)
    check_deadline()
    block = w3.eth.get_block('latest')
    n = block['number']
    age = time.time() - block['timestamp']
    if age > cfg.max_age or age < -30:
        raise ValueError(f'block timestamp is stale or in future: age={age:.0f}s')
    manager, token_id, p = get_position(w3, cfg, n, check_deadline)
    def call(f):
        check_deadline()
        return f.call(block_identifier=n)
    factory_address = call(manager.functions.factory())
    factory = w3.eth.contract(address=factory_address, abi=FACTORY)
    pool_address = call(factory.functions.getPool(p[2],p[3],p[4]))
    if int(pool_address, 16) == 0: raise ValueError('factory returned zero pool')
    pool = w3.eth.contract(address=pool_address, abi=POOL)
    t0, t1 = call(pool.functions.token0()), call(pool.functions.token1())
    fee, spacing, slot = call(pool.functions.fee()), call(pool.functions.tickSpacing()), call(pool.functions.slot0())
    if t0.lower() != p[2].lower() or t1.lower() != p[3].lower() or fee != p[4]:
        raise ValueError('pool data do not match NFT')
    d0 = call(w3.eth.contract(address=t0, abi=TOKEN).functions.decimals())
    d1 = call(w3.eth.contract(address=t1, abi=TOKEN).functions.decimals())
    if d0 > 36 or d1 > 36 or spacing <= 0 or p[5] >= p[6] or p[5] % spacing or p[6] % spacing:
        raise ValueError('invalid pool decimals, tick spacing or position bounds')
    sqrt, tick = slot[:2]
    raw = raw_price_from_sqrt(sqrt)
    # slot0 tick can equal the preceding tick at a boundary, but should be near sqrt price.
    if not sqrt_ratio_at_tick(tick) <= sqrt <= sqrt_ratio_at_tick(min(tick + 1, 887272)):
        raise ValueError('slot0 tick and sqrtPriceX96 disagree')
    w0 = t0.lower() == cfg.weth.lower()
    price = quote_price(raw,d0,d1,w0)
    prices = sorted(quote_price(raw_price_at_tick(t), d0, d1, w0) for t in (p[5],p[6]))
    a0,a1 = amounts(p[7],sqrt,p[5],p[6])
    try:
        fees = fee_snapshot(call, pool, p, tick, (d0,d1), w0)
    except Exception as exc:
        fees = {'status':'UNKNOWN', 'error_type':type(exc).__name__, 'note':'fee-growth reads unavailable; not zero'}
    # Detect a reorganization while reading historical block state.
    if time.time() - block['timestamp'] > cfg.max_age:
        raise ValueError('snapshot became stale while reading RPC')
    check_deadline()
    if w3.eth.get_block(n)['hash'] != block['hash']:
        raise ValueError('block changed during snapshot')
    return {'block': n, 'block_hash': block['hash'].hex(), 'block_timestamp': block['timestamp'], 'token_id': token_id,
        'factory': factory_address, 'pool': pool_address, 'token0': t0, 'token1': t1, 'fee': fee,
        'd0':d0, 'd1':d1, 'spacing':spacing, 'sqrt':sqrt, 'tick':tick, 'lower':p[5], 'upper':p[6],
        'liquidity':p[7], 'fee_growth_last0_x128':p[8], 'fee_growth_last1_x128':p[9], 'tokens_owed0_raw':p[10], 'tokens_owed1_raw':p[11], 'raw_price':raw,
        'weth_is_token0':w0, 'fees':fees, 'price_usdc_per_weth':price, 'lower_price':prices[0], 'upper_price':prices[1],
        'weth': (a0 / D(10)**d0 if w0 else a1 / D(10)**d1),
        'usdc': (a1 / D(10)**d1 if w0 else a0 / D(10)**d0),
        'status':position_status(tick,p[5],p[6])}
