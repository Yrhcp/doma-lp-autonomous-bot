"""Offline calldata laboratory: chain 31337 ONLY; never a mainnet plan.

The runtime gate in preflight always blocks a Doma transaction. These helpers
produce real ABI encoding solely for isolated simulations on a local fork or
mock chain. They do not submit, sign, estimate or read balances.
"""
from eth_abi import encode,decode
from web3 import Web3
from .preflight import MINT_ABI
from .v3_integer import MAX_UINT256,ticks_for_pool

LAB_CHAIN=31337
APPROVE_ABI=[{'type':'function','name':'approve','stateMutability':'nonpayable','inputs':[{'name':'spender','type':'address'},{'name':'value','type':'uint256'}],'outputs':[{'name':'','type':'bool'}]}]
ROUTER_ABI=[{'type':'function','name':'execute','stateMutability':'payable','inputs':[{'name':'commands','type':'bytes'},{'name':'inputs','type':'bytes[]'},{'name':'deadline','type':'uint256'}],'outputs':[]}]

def _chain(chain):
    if chain!=LAB_CHAIN: raise ValueError('offline simulation chain 31337 only; Doma blocked')

def _address(value):
    a=Web3.to_checksum_address(value)
    if int(a,16)==0: raise ValueError('zero address')
    return a

def _deadline(deadline,now):
    if not isinstance(deadline,int) or not isinstance(now,int) or not now<deadline<=now+300: raise ValueError('deadline invalid')

def approval_calldata(*,chain,spender,amount,ceiling):
    _chain(chain);spender=_address(spender)
    if not all(isinstance(x,int) and not isinstance(x,bool) for x in (amount,ceiling)) or not 0<amount<=ceiling<MAX_UINT256:
        raise ValueError('approval must be strictly bounded')
    c=Web3().eth.contract(abi=APPROVE_ABI)
    data=c.encode_abi('approve',args=[spender,amount])
    _,decoded=c.decode_function_input(data)
    if decoded!={'spender':spender,'value':amount}: raise ValueError('approval decode mismatch')
    return data

def mint_calldata(*,chain,wallet,token0,token1,fee,lower,upper,spacing,amount0,amount1,min0,min1,deadline,now):
    _chain(chain);wallet=_address(wallet);token0=_address(token0);token1=_address(token1)
    if int(token0,16)>=int(token1,16) or fee not in (100,500,3000,10000):raise ValueError('invalid pair or fee')
    ticks_for_pool(lower,upper,spacing);_deadline(deadline,now)
    if not all(isinstance(v,int) and not isinstance(v,bool) and 0<=v<MAX_UINT256 for v in (amount0,amount1,min0,min1)):
        raise ValueError('invalid token amounts')
    if not (0<min0<=amount0 and 0<min1<=amount1): raise ValueError('minimums required for laboratory case')
    params=(token0,token1,fee,lower,upper,amount0,amount1,min0,min1,wallet,deadline)
    c=Web3().eth.contract(abi=MINT_ABI);data=c.encode_abi('mint',args=[params])
    _,decoded=c.decode_function_input(data)
    names=[x['name'] for x in MINT_ABI[0]['inputs'][0]['components']]
    if tuple(decoded['params'][name] for name in names)!=params:raise ValueError('mint decode mismatch')
    return data

def swap_calldata(*,chain,wallet,token_in,token_out,fee,amount_in,min_out,deadline,now):
    _chain(chain);wallet=_address(wallet);token_in=_address(token_in);token_out=_address(token_out)
    _deadline(deadline,now)
    if token_in==token_out or fee not in (100,500,3000,10000):raise ValueError('invalid swap pair')
    if not all(isinstance(x,int) and not isinstance(x,bool) and 0<x<MAX_UINT256 for x in (amount_in,min_out)):
        raise ValueError('swap input and minimum must be positive')
    path=bytes.fromhex(token_in[2:])+fee.to_bytes(3,'big')+bytes.fromhex(token_out[2:])
    swap_input=encode(['address','uint256','uint256','bytes','bool'],[wallet,amount_in,min_out,path,True])
    c=Web3().eth.contract(abi=ROUTER_ABI);data=c.encode_abi('execute',args=[b'\x00',[swap_input],deadline])
    _,decoded=c.decode_function_input(data)
    if decoded['commands']!=b'\x00' or decoded['deadline']!=deadline or len(decoded['inputs'])!=1:raise ValueError('router decode mismatch')
    recipient,inp,out,decoded_path,payer=decode(['address','uint256','uint256','bytes','bool'],decoded['inputs'][0])
    if (recipient.lower(),inp,out,decoded_path,payer)!=(wallet.lower(),amount_in,min_out,path,True): raise ValueError('swap decode mismatch')
    return data

def simulate_call(w3,*,sender,target,data):
    """One nonpersistent EVM call on the isolated chain only; no state overrides."""
    if w3.eth.chain_id != LAB_CHAIN: raise ValueError('simulation requires local chain 31337')
    sender=_address(sender);target=_address(target)
    if not isinstance(data,str) or not data.startswith('0x'): raise ValueError('invalid calldata')
    return w3.eth.call({'from':sender,'to':target,'data':data,'value':0})
