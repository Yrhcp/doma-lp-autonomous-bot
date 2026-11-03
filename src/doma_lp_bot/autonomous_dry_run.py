"""Offline signing rehearsal for an already prepared manual-cycle step.

This module cannot broadcast. A signed byte string is never returned or stored.
It is not an autonomous executor and is not evidence of a complete cycle.
"""
import os
import stat
import fcntl
from eth_account import Account
from web3 import Web3
from .manual_open import Blocked
from .rebalance_manual import cycle_base, scope, verify_before_wallet
from .storage import load_state, save_state

from .protected import PROTECTED_NFTS
from dotenv import load_dotenv
load_dotenv()
_wallet = os.getenv('AUTONOMOUS_WALLET_ADDRESS', '').strip()
TEST_WALLET = Web3.to_checksum_address(_wallet) if _wallet else None


def validate_selection(cfg):
    if TEST_WALLET is None or cfg.chain != 97477 or cfg.wallet != TEST_WALLET:
        raise Blocked('configure the dedicated autonomous wallet for Doma chain 97477')
    if type(cfg.token_id) is not int or cfg.token_id <= 0:
        raise Blocked('укажите NFT тестового кошелька; позиция пока не открыта')
    if cfg.token_id in PROTECTED_NFTS:
        raise Blocked('NFT защищена от автономного режима')


def _account_from_file(path):
    if not os.path.isabs(path):
        raise Blocked('путь к локальному секрету должен быть абсолютным')
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or
                info.st_mode & 0o077):
            raise Blocked('файл секрета должен принадлежать пользователю и иметь права 0600')
        with os.fdopen(fd, 'rb', closefd=False) as f:
            key = f.read(130).strip()
        if len(key) not in (32, 64, 66):
            raise Blocked('неверный формат файла секрета')
        try:
            return Account.from_key(key if len(key) == 32 else key.decode('ascii'))
        except (ValueError, UnicodeError):
            raise Blocked('неверный формат файла секрета') from None
    finally:
        os.close(fd)


def _checked_signature(w, cfg, record, key_path):
    validate_selection(cfg)
    if (record.get('chainId') != 97477 or record.get('from', '').lower() != cfg.wallet.lower()
            or record.get('value') != 0):
        raise Blocked('транзакция относится к другой сети или отправителю')
    state = load_state(cycle_base(cfg))
    if (not state or state.get('scope') != scope(cfg) or not state.get('pending')
            or state['pending'].get('hash') or state['pending'].get('call') != record):
        raise Blocked('нет соответствующего неподписанного pending в журнале')
    account = _account_from_file(key_path)
    if account.address != TEST_WALLET:
        raise Blocked('секрет не соответствует тестовому кошельку')
    verify_before_wallet(w, cfg, record)
    tx = {'chainId': 97477, 'nonce': record['nonce'], 'to': record['to'],
          'value': 0, 'data': record['data'], 'gas': record['gasLimit'],
          'maxFeePerGas': record['maxFeePerGas'],
          'maxPriorityFeePerGas': record['maxPriorityFeePerGas'], 'type': 2}
    signed = account.sign_transaction(tx)
    return state, signed


def sign_rehearsal(w, cfg, record, key_path):
    """Revalidate a durable pending record and sign locally, discarding bytes."""
    _, signed = _checked_signature(w, cfg, record, key_path)
    # A digest is sufficient for the rehearsal: never return or persist raw bytes.
    return signed.hash.hex()


def stage_signed_pending(w, cfg, record, key_path):
    """Persist the signed hash before returning bytes; does not broadcast.

    Pending remains blocked if broadcast status is unknown. This helper does not
    authorize a transaction and must only be used by a future approved executor.
    """
    fd = os.open(str(cfg.log)+'.rebalance.lock', os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Blocked('другой процесс выполняет ребалансировку') from None
        state, signed = _checked_signature(w, cfg, record, key_path)
        state['pending']['hash'] = Web3.to_hex(signed.hash)
        save_state(cycle_base(cfg), state)
        return signed.raw_transaction
    finally:
        os.close(fd)
