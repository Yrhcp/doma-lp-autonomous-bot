import os
import math
from dataclasses import dataclass
from decimal import Decimal
from dotenv import load_dotenv
from web3 import Web3

@dataclass(frozen=True)
class Config:
    rpc: str; chain: int; wallet: str; manager: str; token_id: int | None; weth: str; usdc: str
    range_percent: Decimal; exit_buffer_percent: Decimal; poll: float; confirm: float; cooldown: float; limit: int
    timeout: float; snapshot_timeout: float; max_age: int; max_observation_gap: float; log: str; gas_units_a: int | None; gas_units_b: int | None
    router_bps: Decimal | None; slippage_bps: Decimal | None; impact_bps: Decimal | None
    range_quote_offset: Decimal | None = None

    @classmethod
    def load(cls):
        load_dotenv()
        def get(k, default): return os.getenv(k, default).strip()
        def optional(k, typ):
            val = get(k, '')
            return typ(val) if val else None
        def address(k, default): return Web3.to_checksum_address(get(k, default))
        c = cls(get('RPC_URL','https://rpc.doma.xyz'), int(get('EXPECTED_CHAIN_ID','97477')),
            address('WALLET_ADDRESS',''),
            address('POSITION_MANAGER','0xce126ca6aceBBDCe95D7b8A3Ce637951640811E0'), optional('POSITION_TOKEN_ID', int),
            address('WETH_ADDRESS','0x4200000000000000000000000000000000000006'),
            address('USDC_ADDRESS','0x31EEf89D5215C305304a2fA5376a1f1b6C5dc477'),
            Decimal(get('RANGE_PERCENT','0.7')), Decimal(get('EXIT_BUFFER_PERCENT','0.1')), float(get('POLL_SECONDS','15')), float(get('CONFIRM_SECONDS','60')),
            float(get('COOLDOWN_SECONDS','3600')), int(get('MAX_SIGNALS_24H','3')), float(get('RPC_TIMEOUT_SECONDS','10')), float(get('SNAPSHOT_TIMEOUT_SECONDS','90')),
            int(get('MAX_BLOCK_AGE_SECONDS','120')), float(get('MAX_OBSERVATION_GAP_SECONDS','90')), get('LOG_PATH','logs/monitor.jsonl'), optional('ESTIMATED_GAS_UNITS_A',int), optional('ESTIMATED_GAS_UNITS_B',int),
            optional('ROUTER_FEE_BPS',Decimal), optional('SLIPPAGE_BPS',Decimal), optional('PRICE_IMPACT_BPS',Decimal))
        if not all(math.isfinite(x) for x in (c.poll, c.confirm, c.cooldown, c.timeout, c.snapshot_timeout, c.max_observation_gap)) or not c.rpc.startswith(('https://','http://')) or c.chain <= 0 or c.weth == c.usdc or not c.range_percent.is_finite() or not 0 < c.range_percent < 100 or not c.exit_buffer_percent.is_finite() or not 0 <= c.exit_buffer_percent < 100 or c.poll <= 0 or c.confirm < 0 or c.cooldown < 0 or c.limit <= 0 or c.timeout <= 0 or c.snapshot_timeout <= 0 or c.max_age <= 0 or c.max_observation_gap <= 0 or (c.token_id is not None and c.token_id < 0) or any(x is not None and x <= 0 for x in (c.gas_units_a,c.gas_units_b)):
            raise ValueError('invalid configuration')
        if any(x is not None and (not x.is_finite() or not 0 <= x <= 10000) for x in (c.router_bps,c.slippage_bps,c.impact_bps)):
            raise ValueError('invalid bps estimate')
        return c
