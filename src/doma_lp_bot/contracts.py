"""Minimal read-only ABI, matching verified explorer ABI for this deployment."""
def fn(name, inputs, outputs):
    return {'type': 'function', 'name': name, 'stateMutability': 'view',
            'inputs': [{'name': '', 'type': x} for x in inputs],
            'outputs': [{'name': '', 'type': x} for x in outputs]}

MANAGER = [fn('factory', [], ['address']), fn('balanceOf', ['address'], ['uint256']),
           fn('tokenOfOwnerByIndex', ['address', 'uint256'], ['uint256']), fn('ownerOf', ['uint256'], ['address']),
           fn('positions', ['uint256'], ['uint96','address','address','address','uint24','int24','int24','uint128','uint256','uint256','uint128','uint128'])]
FACTORY = [fn('getPool', ['address','address','uint24'], ['address'])]
POOL = [fn('token0', [], ['address']), fn('token1', [], ['address']), fn('fee', [], ['uint24']),
        fn('tickSpacing', [], ['int24']), fn('feeGrowthGlobal0X128', [], ['uint256']), fn('feeGrowthGlobal1X128', [], ['uint256']),
        fn('ticks', ['int24'], ['uint128','int128','uint256','uint256','int56','uint160','uint32','bool']), fn('slot0', [], ['uint160','int24','uint16','uint16','uint16','uint8','bool'])]
TOKEN = [fn('decimals', [], ['uint8'])]
