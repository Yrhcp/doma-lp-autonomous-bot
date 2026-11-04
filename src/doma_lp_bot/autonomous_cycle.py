"""Journal-backed preparation/reconciliation; no broadcasting or monitor loop."""
import fcntl
import os
import random
import time
from contextlib import contextmanager
from dataclasses import replace
from decimal import Decimal
from email.utils import parsedate_to_datetime
from pathlib import Path
from web3.exceptions import ContractLogicError

from .autonomous_dry_run import validate_selection, PROTECTED_NFTS
from .manual_open import Blocked, MAX_STEP_FEE_WEI, MANAGER_ADDRESS, USDC, WETH, oracle
from .rebalance_manual import (active_base, active_scope, cycle_base, prepare, read,
                               reconcile, scope, with_token)
from .storage import load_state, save_state
from . import rebalance_mint as mint, rebalance_swap as swap
from .strategy import SignalGate, buffered_exit

ORDER = ('decrease', 'collect', 'approve_swap_token', 'approve_swap_permit2',
         'swap', 'approve_mint_usdc', 'approve_mint_weth', 'mint_new')
INITIAL_NFT = int(os.getenv('AUTONOMOUS_INITIAL_NFT') or '0')

def cycle_gas_budget(w, wallet):
    """Conservative current-price estimate; future network fees can change."""
    block = w.eth.get_block('latest')
    if not -30 <= time.time() - block.timestamp <= 120:
        raise Blocked('невозможно оценить газ по устаревшему блоку')
    native = w.eth.get_balance(wallet, block_identifier=block.number)
    maxfee = max(2 * w.eth.gas_price, 2 * block.baseFeePerGas + 1_000_000)
    l1 = oracle(w, 'getL1FeeUpperBound(uint256)', 4096, block.number)
    op = oracle(w, 'getOperatorFee(uint256)', 800000, block.number)
    per_step = min(MAX_STEP_FEE_WEI, 800000 * maxfee + 2 * (l1 + op))
    # Eight possible steps, plus 25% at current fee levels. Each actual step
    # is independently checked again; this is not a future-fee guarantee.
    required = (len(ORDER) * per_step * 125 + 99) // 100
    return native, required, block.number

def eth_usd_spot():
    """Coinbase ETH/USD spot; None if unavailable, malformed or stale."""
    import requests
    try:
        r = requests.get('https://api.coinbase.com/v2/prices/ETH-USD/spot', timeout=5)
        r.raise_for_status()
        age = time.time() - parsedate_to_datetime(r.headers['Date']).timestamp()
        data = r.json()['data']
        value = Decimal(data['amount'])
        if data['base'] != 'ETH' or data['currency'] != 'USD' or not value.is_finite() or value <= 0 or not -30 <= age <= 60:
            return None
        return value
    except (requests.RequestException, KeyError, ValueError, TypeError, ArithmeticError):
        return None

def range_key(cfg):
    offset = getattr(cfg, 'range_quote_offset', None)
    return f'quote_offset:{offset}' if offset is not None else str(cfg.range_percent)


class SignalMonitor:
    """Durable 60-second / 0.1% trigger; never starts transactions itself."""
    def __init__(self, cfg):
        validate_selection(cfg)
        if (cfg.confirm != 60 or cfg.exit_buffer_percent != Decimal('0.1') or
                cfg.range_quote_offset != Decimal('15') or cfg.poll != 15):
            raise Blocked('неверная автономная стратегия')
        self.cfg = cfg
        self.base = str(cfg.log) + '.autonomous-monitor'
        self.scope = {'purpose': 'autonomous_monitor', 'chain': 97477, 'wallet': cfg.wallet.lower()}
        self.state = load_state(self.base) or {'version': 1, 'scope': self.scope, 'signals': [],
                      'latch': None, 'watch': None, 'latest': None, 'cycle_token': None}
        if self.state.get('scope') != self.scope:
            raise Blocked('журнал мониторинга относится к другому кошельку')
        latch = self.state['latch']
        if latch is not None:
            latch = (tuple(latch[0]) if latch[0] is not None else None, latch[1])
        # The autonomous mode is not limited by a fixed cooldown or daily quota.
        # Keep the latch and durable pending journal to prevent duplicate cycles.
        self.gate = SignalGate(60, 0, None, self.state['signals'],
                               cfg.max_observation_gap, latch)

    def observe(self, snapshot, now):
        if snapshot.get('token_id') != self.cfg.token_id or self.cfg.token_id in PROTECTED_NFTS:
            raise Blocked('снимок относится к другой или защищённой NFT')
        cycle = self.state.get('cycle_token')
        if cycle is not None:
            if cycle != self.cfg.token_id:
                raise Blocked('предыдущий цикл ещё не завершён')
            return True
        side = buffered_exit(snapshot, Decimal('0.1'))
        identity = (snapshot['token_id'], snapshot['pool'], snapshot['lower'],
                    snapshot['upper'], snapshot['liquidity'])
        ready = self.gate.observe(not side, now, snapshot['block'], direction=side,
                                  identity=identity, actual_in_range=snapshot['status'] == 'IN_RANGE')
        if ready:
            self.gate.record(now)
            self.state['cycle_token'] = self.cfg.token_id
        self.state['signals'] = list(self.gate.signals)
        self.state['latch'] = self.gate.latched
        self.state['latest'] = {'token_id': self.cfg.token_id, 'block': snapshot['block'], 'time': now}
        save_state(self.base, self.state)
        return ready

    def completed(self, w, initial_cfg):
        """Clear the latch only after the mint receipt establishes a new NFT."""
        previous = self.state.get('cycle_token')
        if previous != self.cfg.token_id:
            raise Blocked('нет активного цикла')
        current = resolve_active(w, initial_cfg)
        if current.token_id == previous:
            raise Blocked('новая NFT ещё не подтверждена')
        self.state['cycle_token'] = None
        self.cfg = current
        save_state(self.base, self.state)
        return current


@contextmanager
def cycle_lock(cfg):
    fd = os.open(str(cfg.log) + '.rebalance.lock', os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Blocked('другой процесс выполняет ребалансировку') from None
        yield
    finally:
        os.close(fd)


def _empty(cfg):
    return {'version': 1, 'scope': scope(cfg), 'signals': [], 'latch': None,
            'watch': None, 'latest': None, 'pending': None, 'completed': []}


def _state(cfg):
    s = load_state(cycle_base(cfg)) or _empty(cfg)
    if s.get('scope') != scope(cfg) or not isinstance(s.get('completed'), list):
        raise Blocked('журнал относится к другой позиции')
    steps = [x['call']['step'] for x in s['completed']]
    if (steps[:min(2, len(steps))] != ['decrease', 'collect'][:min(2, len(steps))]
            or any(x not in ORDER for x in steps) or len(steps) != len(set(steps))
            or [ORDER.index(x) for x in steps] != sorted(ORDER.index(x) for x in steps)):
        raise Blocked('нарушен порядок журнала')
    return s


def _confirm(w, cfg, state, call, tx_hash):
    step = call['step']; done = state['completed']
    if step in ('decrease', 'collect'):
        return reconcile(w, cfg, call, tx_hash)
    if step in swap.SWAP_STEPS:
        return swap.reconcile(w, cfg, call, tx_hash, state.get('swap_plan'), mint.available(done[:2], state.get('wallet_budget_raw')))
    return mint.reconcile(w, cfg, call, tx_hash, done, state.get('wallet_budget_raw'))


def _pending_step(state):
    pending = state.get('pending')
    if not isinstance(pending, dict):
        raise Blocked('нет ожидающей операции')
    step = pending.get('call', {}).get('step')
    steps = [x['call']['step'] for x in state['completed']]
    if (step not in ORDER or step in steps or
        (len(steps) < 2 and step != ('decrease', 'collect')[len(steps)]) or
        (steps and ORDER.index(step) <= ORDER.index(steps[-1]))):
        raise Blocked('pending не соответствует этапу')
    return step


def resolve_active(w, cfg):
    """Recover the active NFT from verified per-cycle mint records."""
    if INITIAL_NFT <= 0 or cfg.token_id != INITIAL_NFT:
        raise Blocked('configure AUTONOMOUS_INITIAL_NFT with the initial owned position')
    seen = set()
    while True:
        validate_selection(cfg)
        if cfg.token_id in seen:
            raise Blocked('цикл в цепочке NFT')
        seen.add(cfg.token_id)
        s = _state(cfg); done = s['completed']
        if not done or done[-1]['call']['step'] != 'mint_new':
            return cfg
        if s.get('pending'):
            raise Blocked('завершённый mint и pending одновременно')
        item = done[-1]
        new_id = _confirm(w, cfg, {**s, 'completed': done[:-1]}, item['call'], item['hash'])
        if (type(new_id) is not int or new_id <= 0 or new_id in PROTECTED_NFTS or
                new_id in seen or item.get('new_token_id') != new_id or s.get('active_token_id') != new_id):
            raise Blocked('новая NFT не совпадает с журналом')
        cfg = with_token(cfg, new_id)


def prepare_next(w, cfg):
    """Return existing pending or prepare exactly one next step; never sign/send."""
    validate_selection(cfg)
    with cycle_lock(cfg):
        s = _state(cfg); done = s['completed']; steps = [x['call']['step'] for x in done]
        if s.get('swap_plan') and s['swap_plan'].get('range_percent') != range_key(cfg):
            raise Blocked('сохранённый план swap рассчитан для другого диапазона')
        if s.get('pending'):
            _pending_step(s)
            return s['pending']
        for item in done:
            result = _confirm(w, cfg, {**s, 'completed': done[:done.index(item)]}, item['call'], item['hash'])
            step = item['call']['step']
            if ((step == 'collect' and result != item.get('received_raw')) or
                (step == 'swap' and result != item.get('swap_received_raw')) or
                (step == 'mint_new' and result != item.get('new_token_id'))):
                raise Blocked('подтверждённый шаг не совпадает с журналом')
        if 'mint_new' in steps:
            raise Blocked('цикл уже завершён; повтор запрещён')
        if len(done) < 2:
            call = prepare(w, cfg, ('decrease', 'collect')[len(done)])
        else:
            fresh = read(w, cfg)
            if 'wallet_budget_raw' not in s:
                if len(done)>2 or s.get('swap_plan'):
                    raise Blocked('цикл начат по прежним правилам; требуется безопасное восстановление')
                balances = fresh.get('balances')
                if not isinstance(balances, list) or len(balances)!=2:
                    raise Blocked('не удалось получить балансы токенов')
                mint.available(done[:2], balances)
                s['wallet_budget_raw'] = list(balances)
                save_state(cycle_base(cfg), s)
            budget = mint.available(done[:2], s['wallet_budget_raw'])
            if 'swap' not in steps and not any(x.startswith('approve_mint_') for x in steps):
                if not s.get('swap_plan'):
                    plan = swap.plan_for(fresh, budget, cfg.range_percent, cfg.range_quote_offset)
                    if plan:
                        plan['range_percent'] = range_key(cfg)
                        s['swap_plan'] = plan
                        save_state(cycle_base(cfg), s)
                if s.get('swap_plan') and s['swap_plan'].get('range_percent') != range_key(cfg):
                    raise Blocked('сохранённый план swap рассчитан для другого диапазона')
                call = (swap.prepare(w, cfg, fresh, s['swap_plan'], budget, steps)
                        if s.get('swap_plan') else mint.prepare(w, cfg, fresh, done, s['wallet_budget_raw']))
            else:
                call = mint.prepare(w, cfg, fresh, done, s['wallet_budget_raw'])
        if call['step'] in steps:
            raise Blocked('повтор подтверждённой операции')
        s['pending'] = {'call': call, 'hash': None}
        save_state(cycle_base(cfg), s)
        return s['pending']


def reconcile_pending(w, cfg):
    """Confirm a saved hash before advancing; retain pending on any error."""
    validate_selection(cfg)
    with cycle_lock(cfg):
        s = _state(cfg); p = s.get('pending')
        if not p or not p.get('hash'):
            raise Blocked('нет сохранённого hash; повтор заблокирован')
        step = _pending_step(s); done = s['completed']
        if step in [x['call']['step'] for x in done]:
            raise Blocked('операция уже подтверждена')
        value = _confirm(w, cfg, s, p['call'], p['hash'])
        item = {'call': p['call'], 'hash': p['hash'], 'received_raw': value if step == 'collect' else None}
        if step == 'swap': item['swap_received_raw'] = value
        if step == 'mint_new':
            if type(value) is not int or value in PROTECTED_NFTS or value == cfg.token_id or value <= 0:
                raise Blocked('защищённая или неверная новая NFT')
            item['new_token_id'] = value
            s['active_token_id'] = value
        s['completed'].append(item); s['pending'] = None
        save_state(cycle_base(cfg), s)
        return item


def refresh_unsigned_mint(w, cfg):
    """Atomically replace a stale, never-staged mint after verifying the chain.

    A saved hash is never discarded: its broadcast result can be unknown even
    when one RPC reports matching latest and pending nonces.
    """
    from web3 import Web3
    from .preflight import MINT_ABI
    validate_selection(cfg)
    if cfg.range_quote_offset != Decimal('15'):
        raise Blocked('неверный диапазон для обновления mint')
    with cycle_lock(cfg):
        state = _state(cfg)
        pending = state.get('pending')
        if not isinstance(pending, dict) or _pending_step(state) != 'mint_new':
            raise Blocked('нет ожидающего mint')
        if 'hash' not in pending or pending['hash'] is not None:
            raise Blocked('у mint есть hash или неизвестный статус; автоматическая замена запрещена')
        old = pending['call']
        if (old.get('chainId') != 97477 or old.get('from', '').lower() != cfg.wallet.lower() or
                old.get('to', '').lower() != MANAGER_ADDRESS.lower() or old.get('value') != 0 or
                type(old.get('nonce')) is not int):
            raise Blocked('неверные параметры ожидающего mint')
        try:
            fn, decoded = Web3().eth.contract(abi=MINT_ABI).decode_function_input(old['data'])
            params = decoded['params']; details = old['details']
            low, high = int(details['sqrtMin']), int(details['sqrtMax'])
            deadline = params['deadline']
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise Blocked('повреждены параметры ожидающего mint') from exc
        done = state['completed']
        if (fn.fn_name != 'mint' or (params['token0'], params['token1'], params['fee'],
                params['recipient'].lower()) != (USDC, WETH, 100, cfg.wallet.lower()) or
                deadline != details.get('deadline') or not 0 < low <= high or
                (params['amount0Desired'], params['amount1Desired']) != tuple(
                    mint.available(done, state.get('wallet_budget_raw')))):
            raise Blocked('ожидающий mint не соответствует журналу')
        if 'wallet_budget_raw' not in state:
            raise Blocked('цикл начат по прежним правилам; требуется безопасное восстановление')

        def check_nonce():
            latest = w.eth.get_transaction_count(cfg.wallet, 'latest')
            queued = w.eth.get_transaction_count(cfg.wallet, 'pending')
            if latest != old['nonce'] or queued != old['nonce']:
                raise Blocked('nonce изменился или есть транзакция в очереди; проверьте журнал и сеть')

        check_nonce()
        fresh = read(w, cfg)  # verifies chain, NFT, pool, balances and pending nonce
        if fresh['nonce'] != old['nonce']:
            raise Blocked('nonce снимка не совпадает с ожидающим mint')
        stale = time.time() >= deadline - 30 or not low <= fresh['sqrt'] <= high
        if not stale:
            return pending
        for index, item in enumerate(done):
            result = _confirm(w, cfg, {**state, 'completed': done[:index]}, item['call'], item['hash'])
            step = item['call']['step']
            if ((step == 'collect' and result != item.get('received_raw')) or
                    (step == 'swap' and result != item.get('swap_received_raw'))):
                raise Blocked('подтверждённый шаг не совпадает с журналом')
        budget = mint.available(done, state['wallet_budget_raw'])
        remainder = swap.plan_for(fresh, budget, cfg.range_percent, cfg.range_quote_offset)
        if remainder:
            if not any(item['call']['step'] == 'swap' for item in done):
                raise Blocked('новая цена требует swap; автоматическая замена mint остановлена')
            print('Изменились пропорции: повторный swap запрещён, возможен остаток токенов.', flush=True)
        # Recalculate the desired amounts from the saved balances and the
        # verified swap result. Never issue a second swap in this cycle.
        new = mint.prepare(w, cfg, fresh, done, state['wallet_budget_raw'])
        if new.get('step') != 'mint_new' or new.get('nonce') != old['nonce']:
            raise Blocked('повторная подготовка изменила этап или nonce')
        check_nonce()
        replacement = {'call': new, 'hash': None}
        state['pending'] = replacement
        save_state(cycle_base(cfg), state)
        print('Устаревший mint пересчитан; подтверждённые операции сохранены.', flush=True)
        return replacement


def broadcast_authorized(w, cfg, call, key_path, *, authorized=False):
    """Broadcast one verified pending step only after separate authorization.

    This function is deliberately not exposed through the dry-run CLI. If the
    send result is unknown, the durable hash stays pending and no retry occurs.
    """
    if authorized is not True:
        raise Blocked('отправка требует отдельного разрешения пользователя')
    if len(call.get('data', '')) > 4098:
        raise Blocked('calldata больше допустимого размера для оценки газа')
    from web3 import Web3
    from .autonomous_dry_run import stage_signed_pending
    raw = stage_signed_pending(w, cfg, call, key_path)
    expected = Web3.to_hex(Web3.keccak(raw))
    sent = Web3.to_hex(w.eth.send_raw_transaction(raw))
    if sent.lower() != expected.lower():
        raise Blocked('RPC вернул неверный hash; повтор запрещён')
    return expected


def run_dry_monitor(cfg):
    """Monitor and rehearse the first required call; never broadcast."""
    _run_monitor(cfg, authorized=False)


def run_authorized_monitor(cfg, *, authorized=False):
    """Not exposed in CLI; invoke only after separate user authorization."""
    if authorized is not True:
        raise Blocked('отправка требует отдельного разрешения пользователя')
    _run_monitor(cfg, authorized=True)


def _run_monitor(cfg, *, authorized):
    from requests.exceptions import RequestException
    from web3.exceptions import Web3Exception
    from .autonomous_dry_run import TEST_WALLET, _account_from_file, sign_rehearsal
    from .pool import snapshot
    from .rpc import connect, check_chain

    directory = Path.home()/'.local/share/doma-lp'
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    initial = replace(cfg, wallet=TEST_WALLET, token_id=INITIAL_NFT, range_percent=Decimal('0.3'), range_quote_offset=Decimal('15'),
                      exit_buffer_percent=Decimal('0.1'), confirm=60, poll=15,
                      log=str(directory/'autonomous-monitor.jsonl'))
    validate_selection(initial)
    key_path = str(directory/'test-wallet.key')
    if _account_from_file(key_path).address != TEST_WALLET:
        raise Blocked('файл не соответствует тестовому кошельку')
    fd = os.open(str(initial.log) + '.autonomous.lock', os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Blocked('автономный монитор уже запущен') from None
        _run_dry_loop(initial, key_path, snapshot, connect, check_chain, sign_rehearsal,
                      (RequestException, TimeoutError, ConnectionError, Web3Exception),
                      broadcast=broadcast_authorized if authorized else None)
    finally:
        os.close(fd)


def _run_dry_loop(initial, key_path, snapshot, connect, check_chain, sign_rehearsal, rpc_errors, broadcast=None):
    w = connect(initial)
    monitor = SignalMonitor(initial)
    backoff = 5
    reserve_check_at = 0
    while True:
        try:
            check_chain(w, 97477)
            active = resolve_active(w, initial)
            previous = monitor.state.get('cycle_token')
            if previous is not None and active.token_id != previous:
                monitor.cfg = with_token(initial, previous)
                active = monitor.completed(w, initial)
                previous = None
            monitor.cfg = active
            state = _state(active)
            if state.get('swap_plan') and state['swap_plan'].get('range_percent') != range_key(active):
                raise Blocked('сохранённый план swap рассчитан для другого диапазона')
            if state.get('pending') and previous is None:
                raise Blocked('pending без подтверждённого сигнала; требуется проверка журнала')
            if previous is not None:
                pending_before = state.get('pending')
                if not state.get('completed') and not (pending_before and pending_before.get('hash')):
                    native, required, block = cycle_gas_budget(w, active.wallet)
                    print('Резерв на цикл:', required, 'wei; ETH:', native, 'wei; блок', block, flush=True)
                    if native < required:
                        raise Blocked('недостаточно ETH для оценки полного цикла с запасом')
                pending = state.get('pending') or prepare_next(w, active)
                if pending.get('hash'):
                    receipt = w.eth.get_transaction_receipt(pending['hash'])
                    if w.eth.block_number < receipt['blockNumber'] + 1:
                        time.sleep(2)
                        continue
                    reconcile_pending(w, active)
                    backoff = 5
                    continue
                if pending.get('call', {}).get('step') == 'mint_new':
                    pending = refresh_unsigned_mint(w, active)
                if broadcast is not None:
                    broadcast(w, active, pending['call'], key_path, authorized=True)
                    backoff = 5
                    continue
                sign_rehearsal(w, active, pending['call'], key_path)
                print('DRY-RUN: шаг', pending['call']['step'],
                      'проверен и подписан локально; отправка отключена.', flush=True)
                return
            s = snapshot(w, active)
            print('NFT', active.token_id, s['status'], 'блок', s['block'], flush=True)
            if time.time() >= reserve_check_at:
                native = w.eth.get_balance(active.wallet, block_identifier=s['block'])
                price = eth_usd_spot()
                if price is None:
                    print('Предупреждение: резерв ETH в USD неизвестен; цена ETH/USD недоступна.', flush=True)
                elif Decimal(native) * price / Decimal(10**18) < Decimal('2'):
                    print('Предупреждение: резерв газа ниже цели $2;', native, 'wei; ETH/USD', str(price), flush=True)
                reserve_check_at = time.time() + 300
            if monitor.observe(s, time.time()):
                continue
            backoff = 5
            time.sleep(active.poll)
        except ContractLogicError as exc:
            raise Blocked('контракт отклонил проверку; состояние сохранено для разбора') from exc
        except rpc_errors as exc:
            monitor.gate.reset_observation()
            print('RPC временно недоступен:', type(exc).__name__, flush=True)
            time.sleep(min(300, backoff * random.uniform(1.0, 1.2)))
            backoff = min(300, backoff * 2)
