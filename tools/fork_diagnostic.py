#!/usr/bin/env python3
"""Historical research only: eth_call, debug_traceCall and eth_simulateV1.

Usage: PYTHONPATH=src python tools/fork_diagnostic.py EXAMPLE_V3_RESEARCH.json
Requires a local Anvil fork at 127.0.0.1:18545 pinned to snapshot block,
with chain ID 31337. Simulated transactions are never signed or submitted.
This is not a plan for real use and does not verify USD or total costs.
"""
import json
import sys
from eth_abi import decode
from web3 import Web3
from doma_lp_bot.preflight import QUOTER_ABI, MINT_ABI
from doma_lp_bot.contracts import MANAGER
from doma_lp_bot.calldata_lab import approval_calldata, mint_calldata, swap_calldata
from doma_lp_bot.v3_integer import ticks_for_pool, liquidity_for_amounts, amounts_for_liquidity

RPC = 'http://127.0.0.1:18545'
PERMIT2 = Web3.to_checksum_address('0x000000000022D473030F116dDEE9F6B43aC78BA3')
ROUTER = Web3.to_checksum_address('0x5089863E97196773038f98459262D866f2281f58')
TOKEN_ABI = [{'type':'function','name':n,'stateMutability':'view','inputs':[{'name':'owner','type':'address'}]+([{'name':'spender','type':'address'}] if n=='allowance' else []),'outputs':[{'type':'uint256'}]} for n in ('balanceOf','allowance')]
PERMIT_ABI = [
    {'type':'function','name':'allowance','stateMutability':'view','inputs':[{'type':'address'},{'type':'address'},{'type':'address'}],'outputs':[{'type':'uint160'},{'type':'uint48'},{'type':'uint48'}]},
    {'type':'function','name':'approve','stateMutability':'nonpayable','inputs':[{'name':'token','type':'address'},{'name':'spender','type':'address'},{'name':'amount','type':'uint160'},{'name':'expiration','type':'uint48'}],'outputs':[]}]

def rpc(w, method, params):
    result = w.provider.make_request(method, params)
    if 'error' in result:
        raise RuntimeError(f'{method}: {result["error"]}')
    return result['result']

def check_results(results, count):
    if len(results) != count or any(row.get('status') != '0x1' for row in results):
        raise RuntimeError('simulation did not succeed at every step')
    return [int(row['gasUsed'], 16) for row in results]

def main(snapshot):
    p=json.load(open(snapshot, encoding='utf8'))
    w=Web3(Web3.HTTPProvider(RPC,request_kwargs={'timeout':90}))
    block=w.eth.get_block('latest')
    if w.eth.chain_id!=31337 or block.number!=p['block'] or block.hash.hex().removeprefix('0x').lower()!=p['block_hash'].removeprefix('0x').lower():
        raise RuntimeError('requires chain 31337 pinned to the exact snapshot block hash')
    addr=Web3.to_checksum_address
    wallet=addr(p['wallet']); t0=addr(p['token0']); t1=addr(p['token1']); manager=addr(p['manager'])
    tokens=[w.eth.contract(address=a,abi=TOKEN_ABI) for a in (t0,t1)]
    permit=w.eth.contract(address=PERMIT2,abi=PERMIT_ABI)
    nft=w.eth.contract(address=manager,abi=MANAGER)
    def state():
        return {'balances':[t.functions.balanceOf(wallet).call() for t in tokens],
                'manager_allowances':[t.functions.allowance(wallet,manager).call() for t in tokens],
                'permit2_erc20_allowances':[t.functions.allowance(wallet,PERMIT2).call() for t in tokens],
                'permit2_router_allowances':[permit.functions.allowance(wallet,t.address,ROUTER).call() for t in tokens],
                'nft_owner':nft.functions.ownerOf(900001).call(),
                'nft_position':nft.functions.positions(900001).call()}
    before=state(); now=block.timestamp; deadline=now+240
    original_mint=mint_calldata(chain=31337,wallet=wallet,token0=t0,token1=t1,fee=p['fee'],lower=p['lower_tick'],upper=p['upper_tick'],spacing=p['tick_spacing'],amount0=p['illustrative_desired_usdc_raw'],amount1=p['illustrative_desired_weth_raw'],min0=1,min1=1,deadline=deadline,now=now)
    original_swap=swap_calldata(chain=31337,wallet=wallet,token_in=t0,token_out=t1,fee=p['fee'],amount_in=1000000,min_out=1,deadline=deadline,now=now)
    failures={}
    for name,target,data in [('mint',manager,original_mint),('swap',ROUTER,original_swap)]:
        tx={'from':wallet,'to':target,'data':data}
        result=w.provider.make_request('eth_call',[tx,'latest'])
        if 'error' not in result: raise RuntimeError(f'{name} unexpectedly succeeded')
        trace=rpc(w,'debug_traceCall',[tx,'latest',{'tracer':'callTracer'}])
        failures[name]={'error':result['error'],'trace':trace}
    swap_in=2_800_000_000_000_000
    quoter=w.eth.contract(address=addr(p['quoter']),abi=QUOTER_ABI)
    out,sqrt_after,crossed,quote_gas=quoter.functions.quoteExactInputSingle((t1,t0,swap_in,p['fee'],0)).call()
    min_out=out*995//1000
    want0=before['balances'][0]+min_out; want1=before['balances'][1]-swap_in
    lo,hi=ticks_for_pool(p['lower_tick'],p['upper_tick'],p['tick_spacing'])
    liquidity=liquidity_for_amounts(sqrt_after,lo,hi,want0,want1)
    used0,used1=amounts_for_liquidity(sqrt_after,lo,hi,liquidity,True)
    steps=[('approve_weth_permit2',t1,approval_calldata(chain=31337,spender=PERMIT2,amount=swap_in,ceiling=swap_in)),
           ('permit2_approve_router',PERMIT2,permit.encode_abi('approve',args=[t1,ROUTER,swap_in,deadline])),
           ('swap',ROUTER,swap_calldata(chain=31337,wallet=wallet,token_in=t1,token_out=t0,fee=p['fee'],amount_in=swap_in,min_out=min_out,deadline=deadline,now=now)),
           ('approve_usdc_manager',t0,approval_calldata(chain=31337,spender=manager,amount=want0,ceiling=want0)),
           ('approve_weth_manager',t1,approval_calldata(chain=31337,spender=manager,amount=want1,ceiling=want1)),
           ('mint',manager,mint_calldata(chain=31337,wallet=wallet,token0=t0,token1=t1,fee=p['fee'],lower=p['lower_tick'],upper=p['upper_tick'],spacing=p['tick_spacing'],amount0=want0,amount1=want1,min0=used0*99//100,min1=used1*99//100,deadline=deadline,now=now))]
    calls=[{'from':wallet,'to':to,'data':data,'gas':'0x1e8480'} for _,to,data in steps]
    sim=rpc(w,'eth_simulateV1',[{'blockStateCalls':[{'calls':calls}],'validation':False,'returnFullTransactions':False},'latest'])
    result=sim[0]['calls']; gas=check_results(result,len(steps))
    minted=decode(['uint256','uint128','uint256','uint256'],bytes.fromhex(result[-1]['returnData'][2:]))
    if tuple(minted[1:])!=(liquidity,used0,used1): raise RuntimeError('mint output differs from integer calculation')
    # Check contract-level failure paths, without using the stricter lab encoder.
    codec=w.eth.contract(abi=MINT_ABI)
    base=[t0,t1,p['fee'],p['lower_tick'],p['upper_tick'],want0,want1,1,1,wallet,deadline]
    negatives={}
    for label,index,value in [('expired_deadline',10,now-1),('inverted_ticks',3,p['upper_tick']+1)]:
        params=base.copy(); params[index]=value
        bad=codec.encode_abi('mint',args=[tuple(params)])
        response=w.provider.make_request('eth_call',[{'from':wallet,'to':manager,'data':bad},'latest'])
        if 'error' not in response: raise RuntimeError(f'negative case {label} unexpectedly succeeded')
        negatives[label]=response['error']
    bad_swap=swap_calldata(chain=31337,wallet=wallet,token_in=t1,token_out=t0,fee=p['fee'],amount_in=swap_in,min_out=out+1,deadline=deadline,now=now)
    bad_calls=calls[:2]+[{'from':wallet,'to':ROUTER,'data':bad_swap,'gas':'0x1e8480'}]
    bad_result=rpc(w,'eth_simulateV1',[{'blockStateCalls':[{'calls':bad_calls}],'validation':False},'latest'])[0]['calls']
    check_results(bad_result[:2],2)
    if bad_result[2].get('status')!='0x0': raise RuntimeError('unattainable minimum unexpectedly succeeded')
    negatives['swap_minimum_above_quote']={'error':bad_result[2].get('error'), 'returnData':bad_result[2].get('returnData')}
    if state()!=before: raise RuntimeError('unexpected fork state change')
    output={'warning':'Historical simulation only. No verified USD, costs, finality, or authorization. Not for signing.',
            'block':p['block'],'block_hash':p['block_hash'],'wallet':wallet,'deadline':deadline,'before':before,
            'ticks':[p['lower_tick'],p['upper_tick']],'spacing':p['tick_spacing'],
            'reproduced_failures':failures,'quote':{'input_weth_raw':swap_in,'output_usdc_raw':out,'minimum_usdc_raw':min_out,'sqrt_after':sqrt_after,'ticks_crossed':crossed,'quote_gas':quote_gas},
            'simulation':{'steps':[n for n,_,_ in steps],'gas_used':gas,'total_gas':sum(gas),'synthetic_mint_result':minted,'nft_900001_unchanged':True,'checked_state_unchanged':True,'validation':False,'negative_cases':negatives}}
    print(json.dumps(output,indent=2))

if __name__=='__main__':
    if len(sys.argv)!=2: raise SystemExit(__doc__)
    main(sys.argv[1])
