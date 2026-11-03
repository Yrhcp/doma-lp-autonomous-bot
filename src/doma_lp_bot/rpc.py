from web3 import Web3

def connect(config):
    # Only HTTP RPC requests are made. No signing middleware or transaction functions.
    return Web3(Web3.HTTPProvider(config.rpc, request_kwargs={'timeout': config.timeout}, exception_retry_configuration=None))

def check_chain(w3, expected):
    actual = w3.eth.chain_id
    if actual != expected:
        raise ValueError(f'wrong chain ID: expected {expected}, got {actual}')
