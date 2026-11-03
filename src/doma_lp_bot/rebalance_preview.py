"""Read-only preflight for the one authorised NFT; never creates wallet calls."""
import os
import math
import time
from dataclasses import replace
from decimal import Decimal as D

from eth_abi import decode
from web3 import Web3

from .manual_open import FACTORY_ADDRESS, MANAGER_ADDRESS, POOL_ADDRESS, USDC, WETH, minted_token_id
from .pool import snapshot
from .preflight import QUOTER, QUOTER_ABI
from .v3_integer import amounts_for_liquidity, ticks_for_pool
from .v3_math import range_for_quote
from .protected import PROTECTED_NFTS

TOKEN_ID = int(os.getenv('MANUAL_INITIAL_NFT') or '0')
MINT_HASH = os.getenv('MANUAL_INITIAL_MINT_TX', '')
DECREASE_ABI = [{'type':'function','name':'decreaseLiquidity','stateMutability':'payable',
 'inputs':[{'name':'params','type':'tuple','components':[{'name':'tokenId','type':'uint256'},
 {'name':'liquidity','type':'uint128'},{'name':'amount0Min','type':'uint256'},
 {'name':'amount1Min','type':'uint256'},{'name':'deadline','type':'uint256'}]}],
 'outputs':[{'name':'amount0','type':'uint256'},{'name':'amount1','type':'uint256'}]}]

def require_position(token_id):
    if type(token_id) is not int or token_id != TOKEN_ID or token_id in PROTECTED_NFTS:
        raise ValueError('configure MANUAL_INITIAL_NFT')

def estimate_swap(amount0, amount1, target0, target1, raw_price):
    """Indicative spot-only sizing; actual quote and post-collect balances required."""
    if min(amount0, amount1, target0, target1) < 0 or raw_price <= 0 or not target0 or not target1:
        raise ValueError('invalid token amounts or price')
    a, b, x, y, p = map(D, (amount0, amount1, target0, target1, raw_price))
    delta = a*y-b*x
    if delta > 0:
        return USDC, WETH, min(amount0, int(delta/(y+p*x)))
    if delta < 0:
        return WETH, USDC, min(amount1, int(-delta/(x+y/p)))
    return None, None, 0

def preview(w, cfg, token_id=TOKEN_ID):
    require_position(token_id)
    if cfg.chain != 97477 or cfg.manager != MANAGER_ADDRESS or cfg.usdc != USDC or cfg.weth != WETH:
        raise ValueError('Неверная сеть или адреса в конфигурации')
    r = w.eth.get_transaction_receipt(MINT_HASH)
    tx = w.eth.get_transaction(MINT_HASH)
    if (r['status'] != 1 or tx['to'] != MANAGER_ADDRESS or tx['from'].lower() != cfg.wallet.lower()
        or tx.get('chainId') != 97477 or minted_token_id(r,cfg.wallet) != token_id
        or w.eth.get_block(r['blockNumber']).hash != r['blockHash']
        or w.eth.block_number < r['blockNumber'] + 1):
        raise ValueError('Mint или владелец NFT не подтверждены')
    s = snapshot(w,replace(cfg,token_id=token_id))
    if (s['factory'],s['pool'],s['token0'],s['token1'],s['fee'],s['spacing'],s['d0'],s['d1']) != (FACTORY_ADDRESS,POOL_ADDRESS,USDC,WETH,100,1,6,18):
        raise ValueError('NFT или пул не соответствуют ожидаемым адресам')
    n=s['block']; lower,upper=ticks_for_pool(s['lower'],s['upper'],1)
    amounts=amounts_for_liquidity(s['sqrt'],lower,upper,s['liquidity'])
    square=s['sqrt']*s['sqrt']
    low=math.isqrt(square*9990//10000); high=math.isqrt(square*10010//10000)
    mins=(amounts_for_liquidity(high,lower,upper,s['liquidity'])[0],amounts_for_liquidity(low,lower,upper,s['liquidity'])[1])
    c=w.eth.contract(address=MANAGER_ADDRESS,abi=DECREASE_ABI)
    data=c.encode_abi('decreaseLiquidity',args=[(token_id,s['liquidity'],*mins,int(time.time())+180)])
    result=decode(['uint256','uint256'],w.eth.call({'from':cfg.wallet,'to':MANAGER_ADDRESS,'data':data,'value':0},block_identifier=n))
    if tuple(result)!=tuple(amounts):raise ValueError('Симуляция decreaseLiquidity не совпала с расчётом')
    lo,hi=range_for_quote(s['raw_price'],D('0.7'),1,False)
    a,b=ticks_for_pool(lo,hi,1)
    target=amounts_for_liquidity(s['sqrt'],a,b,10**18)
    tin,tout,amount=estimate_swap(*amounts,*target,s['raw_price'])
    quote=None
    if amount:
        q=w.eth.contract(address=QUOTER,abi=QUOTER_ABI)
        if q.functions.factory().call(block_identifier=n)!=FACTORY_ADDRESS:
            raise ValueError('Quoter не соответствует factory')
        result=q.functions.quoteExactInputSingle((tin,tout,amount,100,0)).call(block_identifier=n)
        quote={'token_in':tin,'token_out':tout,'amount_in_raw':amount,'amount_out_raw':result[0],
               'note':'Предварительная котировка только для основного капитала NFT; после collect необходим новый расчёт.'}
    if w.eth.get_block(n).hash.hex()!=s['block_hash'] or time.time()-s['block_timestamp']>cfg.max_age:
        raise ValueError('Блок устарел или изменился; повторите')
    return {'block':n,'nft':token_id,'pool':s['pool'],'liquidity':s['liquidity'],
      'current_tick':s['tick'],'tick_lower':s['lower'],'tick_upper':s['upper'],
      'price_usdc_per_weth':str(s['price_usdc_per_weth']),
      'range_usdc_per_weth':[str(s['lower_price']),str(s['upper_price'])],
      'principal_raw':list(amounts),'fee_estimate':s['fees'],
      'decrease_simulation':'eth_call успешен; это не транзакция для подписи',
      'temporary_range':{'percent':0.7,'tick_lower':lo,'tick_upper':hi,'note':'Временный диапазон: волатильность не оценивалась.'},
      'indicative_swap':quote,
      'blocked':'Полный цикл не реализован: сбор, swap с ограниченными разрешениями, mint и восстановление после частичного исполнения не проверены. Подпись и закрытие отключены.'}
