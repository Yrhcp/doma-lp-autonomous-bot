import json
import fcntl
import math
import random
import os
import signal
import threading
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from requests.exceptions import RequestException
from web3.exceptions import Web3Exception
from .pool import snapshot
from .rpc import connect
from .simulator import simulate
from .strategy import SignalGate, buffered_exit
from .storage import append_record, load_state, save_state as atomic_save_state, state_path, log_files


class DurableStateError(RuntimeError): pass

def save_state(path,state):
    mask=None
    if hasattr(signal,'pthread_sigmask') and threading.current_thread() is threading.main_thread():
        mask=signal.pthread_sigmask(signal.SIG_BLOCK,{signal.SIGINT,signal.SIGTERM})
    try:
        try: atomic_save_state(path,state)
        except Exception as exc: raise DurableStateError('cannot persist durable state; monitor stopped') from exc
    finally:
        if mask is not None: signal.pthread_sigmask(signal.SIG_SETMASK,mask)

def stamp(): return datetime.now(timezone.utc).isoformat()

def write_log(path, event, **values):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    data = {'time': stamp(), 'event': event, **values}
    payload = json.dumps(data, default=lambda o: str(o) if isinstance(o, Decimal) else repr(o), ensure_ascii=False) + '\n'
    # Defer Ctrl+C until this complete JSONL record has been flushed on Linux.
    mask = None
    if hasattr(signal, 'pthread_sigmask') and threading.current_thread() is threading.main_thread():
        mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT,signal.SIGTERM})
    try:
        append_record(path,payload)
    finally:
        if mask is not None: signal.pthread_sigmask(signal.SIG_SETMASK, mask)

def read_records(path):
    if not Path(path).exists(): return []
    result = []
    with open(path, encoding='utf8') as f:
        for line in f:
            try:
                x = json.loads(line)
                if not isinstance(x, dict): raise ValueError('invalid record')
                if x.get('event') == 'signal' and not all(k in x for k in ('chain','wallet','manager','time')):
                    raise ValueError('incomplete signal record')
                result.append(x)
            except (ValueError, KeyError, TypeError, AttributeError) as exc:
                raise ValueError('signal log malformed; cannot safely enforce rate limit') from exc
    return result

def scoped(x, cfg):
    return x.get('chain') == cfg.chain and x.get('wallet','').lower() == cfg.wallet.lower() and x.get('manager', cfg.manager).lower() == cfg.manager.lower()

def previous_signals(path, cfg):
    found = []
    for x in read_records(path):
        try:
            if x.get('event') == 'signal' and scoped(x, cfg):
                parsed = datetime.fromisoformat(x['time'])
                if parsed.tzinfo is None: raise ValueError('signal timestamp has no timezone')
                timestamp = parsed.timestamp()
                if timestamp > time.time()+5: raise ValueError('signal timestamp in future; check clock')
                found.append(timestamp)
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise ValueError('signal log malformed or clock invalid; cannot safely enforce rate limit') from exc
    return found

def position_identity(s):
    return (s['token_id'], s['pool'], s['lower'], s['upper'], s['liquidity'])

def recover_episode(path, cfg):
    """Reconstruct the signal latch from durable snapshots and signals."""
    latest = None
    latch = None
    for x in read_records(path):
        if not scoped(x,cfg): continue
        if x.get('event') == 'snapshot':
            try:
                identity = position_identity(x)
                if latch is not None:
                    old_id, old_side = latch
                    side = x.get('buffered_exit')
                    if x.get('status') == 'IN_RANGE' or (old_id is not None and identity != old_id) or (side is not None and old_side not in (None,'unknown',side)):
                        latch = None
                latest = x
            except (KeyError,TypeError):
                raise ValueError('invalid snapshot in signal log; cannot reconstruct episode')
        elif x.get('event') == 'signal':
            saved = x.get('position_identity')
            if saved is not None:
                if not isinstance(saved,list) or len(saved)!=5: raise ValueError('invalid signal position identity')
                identity = tuple(saved)
            elif latest is not None and latest.get('token_id') == x.get('token_id'):
                identity = position_identity(latest)
            else:
                # Legacy signal without enough context: conservative latch until in-range.
                identity = None
            latch = (identity, x.get('side') or 'unknown')
    return latch, latest

def distances(price, low, high):
    if min(price,low,high) <= 0 or low >= high: raise ValueError('invalid price or range')
    return {'from_lower_boundary_percent':(price-low)/low*100, 'to_upper_boundary_percent':(high-price)/high*100,
            'rise_to_upper_from_current_percent':(high/price-1)*100, 'fall_to_lower_from_current_percent':(1-low/price)*100}

def show(s, cfg):
    price=s['price_usdc_per_weth']; low=s['lower_price']; high=s['upper_price']
    d=distances(price,low,high)
    side=buffered_exit(s,cfg.exit_buffer_percent)
    print(f"\nDOMA LP — READ-ONLY | block {s['block']} | NFT #{s['token_id']} | {s['status']}")
    print(f"WETH: {price:.6f} USDC.e | range: {low:.6f} — {high:.6f}")
    print(f"От текущей цены до верхней границы: {d['rise_to_upper_from_current_percent']:+.4f}% роста; до нижней: {d['fall_to_lower_from_current_percent']:+.4f}% падения")
    print(f"Расстояния с базой на границе: от нижней {d['from_lower_boundary_percent']:+.4f}%; до верхней {d['to_upper_boundary_percent']:+.4f}%")
    print(f"LP principal: {s['weth']:.12f} WETH | {s['usdc']:.6f} USDC.e | tick: {s['tick']} ({s['lower']}–{s['upper']})")
    print(f"Liquidity: {s['liquidity']} | pool: {s['pool']}")
    fees=s['fees']
    if fees['status']=='UNKNOWN': print('Fees: UNKNOWN (fee-growth data unavailable)')
    else:
        print(f"Fees since update: ~{fees['accrued_since_update_weth']:.12f} WETH | ~{fees['accrued_since_update_usdc']:.6f} USDC.e")
        print(f"Collectable estimate incl. owed (may include principal): ~{fees['collectable_weth_estimate']:.12f} WETH | ~{fees['collectable_usdc_estimate']:.6f} USDC.e")
    if side: msg=f'buffered exit {side}; awaiting confirmation/cooldown and quota'
    elif s['status']=='OUT_OF_RANGE': msg='outside LP range but within exit buffer; monitor'
    else: msg='in range; monitor'
    print(f"Recommendation: {msg}; proposed new range ±{cfg.range_percent}% (no execution)", flush=True)

def show_comparison(result):
    a=result['strategy_a']; b=result['strategy_b']
    print(f"Proposed range: {result['new_lower_price']:.6f} — {result['new_upper_price']:.6f} USDC.e (ticks {result['new_lower_tick']}..{result['new_upper_tick']})")
    print(f"A swap, ideal before costs: {a['capital_utilization_before_costs_percent']:.2f}%; {a['swap_direction']}; {a['swap_input_weth']:.12f} WETH / {a['swap_input_usdc']:.6f} USDC.e; ideal remainder 0 / 0")
    print(f"B no swap: {b['capital_utilization_before_costs_percent']:.2f}%; remainder {b['leftover_weth']:.12f} WETH / {b['leftover_usdc']:.6f} USDC.e")
    def fmt(value, unit): return 'UNKNOWN' if value is None else f'{value:.6f} {unit} (assumption)'
    print(f"A fee assuming this pool: ~{a['pool_fee_usdc_assuming_position_pool']:.6f} USDC.e; gas: {fmt(a['gas_usdc_assumption'],'USDC.e')}; router: {fmt(a['router_fee_usdc_assumption'],'USDC.e')}")
    print(f"A slippage: {fmt(a['slippage_usdc_assumption'],'USDC.e')}; impact: {fmt(a['price_impact_usdc_assumption'],'USDC.e')}; B gas: {fmt(b['gas_usdc_assumption'],'USDC.e')}. Actual costs/profit: UNKNOWN.",flush=True)

def initialize_state(cfg):
    scope={'chain':cfg.chain,'wallet':cfg.wallet.lower(),'manager':cfg.manager.lower()}
    state=load_state(cfg.log)
    if state is not None:
        if state['scope']!=scope: raise ValueError('durable state belongs to a different chain/wallet/manager')
        if any(not isinstance(t,(int,float)) or not math.isfinite(t) or t>time.time()+5 for t in state['signals']): raise ValueError('invalid signal state or clock')
        if state['latch'] is not None:
            latch=state['latch']
            if not isinstance(latch,list) or len(latch)!=2 or latch[1] not in ('below','above','unknown',None) or (latch[0] is not None and (not isinstance(latch[0],list) or len(latch[0])!=5)):
                raise ValueError('invalid latch state')
        if state['watch'] is not None and not isinstance(state['watch'],dict): raise ValueError('invalid watch state')
        if state['latest'] is not None and not isinstance(state['latest'],dict): raise ValueError('invalid latest state')
        return state
    # One-time migration is deliberately fail-closed on a damaged legacy log.
    if any(p != Path(cfg.log) for p in log_files(cfg.log)):
        raise ValueError('durable state missing while rotated logs exist; restore state backup')
    latch,latest=recover_episode(cfg.log,cfg)
    prior=previous_signals(cfg.log,cfg)
    pending=None
    for x in read_records(cfg.log):
        if not scoped(x,cfg): continue
        if x.get('event')=='watch_start': pending={'watch_id':x.get('watch_id'),'deadline_epoch':x.get('deadline_epoch'),'duration_seconds':x.get('duration_seconds')}
        elif x.get('event')=='watch_complete' and pending and x.get('watch_id')==pending.get('watch_id'): pending=None
    state={'version':1,'scope':scope,'signals':prior,'latch':latch,'watch':pending,'latest':latest}
    # JSON-safe legacy snapshot contains strings and integers.
    save_state(cfg.log,state)
    return state

def watch_deadline(cfg, duration, state=None):
    state=state if state is not None else initialize_state(cfg)
    pending=state['watch']
    if pending:
        if pending.get('duration_seconds') != duration: raise ValueError('another watch is unfinished; resume it first')
        deadline=pending.get('deadline_epoch'); wid=pending.get('watch_id')
        if not isinstance(deadline,(int,float)) or not math.isfinite(deadline) or deadline>time.time()+duration+5 or not isinstance(wid,str): raise ValueError('invalid watch checkpoint')
        return wid,deadline
    wid=uuid.uuid4().hex;deadline=time.time()+duration
    state['watch']={'watch_id':wid,'deadline_epoch':deadline,'duration_seconds':duration}
    save_state(cfg.log,state)
    write_log(cfg.log,'watch_start',chain=cfg.chain,wallet=cfg.wallet,manager=cfg.manager,watch_id=wid,duration_seconds=duration,deadline_epoch=deadline)
    return wid,deadline

class StopRequested(BaseException): pass

def complete_watch(cfg,state):
    watch=state.get('watch')
    if not watch: return False
    if time.time() < watch['deadline_epoch']: return False
    # First persist completion intent; after interruption the same watch can be reconciled.
    watch['completion_pending']=True
    save_state(cfg.log,state)
    existing=False
    for file in log_files(cfg.log):
        lines=file.read_bytes().splitlines(keepends=True)
        for index,line in enumerate(lines):
            try:
                if not line.endswith(b'\n'): raise ValueError('incomplete record')
                item=json.loads(line)
                if not isinstance(item,dict): raise ValueError('invalid record')
            except (ValueError,UnicodeDecodeError):
                if index == len(lines)-1: continue
                raise ValueError('corrupted interior log; cannot safely reconcile completion')
            if item.get('event')=='watch_complete' and item.get('watch_id')==watch['watch_id']:
                existing=True
    if not existing:
        write_log(cfg.log,'watch_complete',chain=cfg.chain,wallet=cfg.wallet,manager=cfg.manager,watch_id=watch['watch_id'],deadline_epoch=watch['deadline_epoch'])
    state['watch']=None
    save_state(cfg.log,state)
    return True

def finalize_watch(cfg):
    p=Path(cfg.log);p.parent.mkdir(parents=True,exist_ok=True)
    fd=os.open(str(p)+'.lock',os.O_CREAT|os.O_RDWR,0o600)
    try:
        try: fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: raise RuntimeError('monitor is running; stop it before finalizing')
        state=initialize_state(cfg)
        if not state['watch']:
            print('Нет незавершённого окна наблюдения.');return 0
        if complete_watch(cfg,state):
            print('Окно завершено, watch_complete записан.');return 0
        print('Срок ещё не наступил; состояние не изменено.');return 1
    finally: os.close(fd)

def backoff_delay(attempt, rng=random.uniform):
    if attempt <= 0: return 0
    base=min(300.0,5.0 * (2 ** min(attempt-1, 6)))
    return min(300.0, rng(base, min(300.0,base*1.25)))

def _run(cfg, once=False, simulate_now=False, duration_seconds=None, verbose_json=False):
    w3 = connect(cfg)
    state = initialize_state(cfg)
    latest=state['latest']
    latch=state['latch']
    if latch is not None: latch=(tuple(latch[0]) if latch[0] is not None else None,latch[1])
    gate = SignalGate(cfg.confirm, cfg.cooldown, cfg.limit, state['signals'], cfg.max_observation_gap, latch)
    last_position = None
    last_time = None
    last_block = None
    startup_gap = latest is not None and not once
    failures = 0
    rpc_failures = 0
    wid = deadline = monotonic_deadline = None
    completed=False
    if duration_seconds is not None:
        if duration_seconds <= 0: raise ValueError('watch duration must be positive')
        wid,deadline=watch_deadline(cfg,duration_seconds,state)
        monotonic_deadline = time.monotonic() + max(0, deadline-time.time())
    try:
        while True:
            if deadline is not None and (time.time() >= deadline or time.monotonic() >= monotonic_deadline):
                if complete_watch(cfg,state):
                    completed=True
                    print('Наблюдение завершено.',flush=True)
                    break
                # Tolerate tiny clock drift; a real backward jump must not fabricate completion.
                remaining=deadline-time.time()
                if 0 < remaining <= 2:
                    time.sleep(remaining)
                    continue
                raise RuntimeError('wall clock moved backwards; watch remains pending')
            try:
                s = snapshot(w3,cfg)
                now = time.time()
                identity = position_identity(s)
                if last_position is not None and identity != last_position: gate.reset_observation()
                gap_reason = None
                if startup_gap and not failures: gap_reason='restart_or_prior_session'
                elif not failures and last_time is not None and (now-last_time > cfg.max_observation_gap or now < last_time or s['block'] <= last_block): gap_reason='observation_gap_or_block_regression'
                if gap_reason:
                    gate.reset_observation()
                    write_log(cfg.log,'gap',chain=cfg.chain,wallet=cfg.wallet,manager=cfg.manager,reason=gap_reason,
                              previous_observation_time=latest.get('time') if latest else None, resumed_block=s['block'], elapsed_seconds=None if last_time is None else max(0,now-last_time))
                if failures:
                    write_log(cfg.log,'reconnect',chain=cfg.chain,wallet=cfg.wallet,manager=cfg.manager,failures=failures,block=s['block'],elapsed_seconds=None if last_time is None else max(0,now-last_time))
                failures=0; rpc_failures=0; startup_gap=False
                last_position=identity; last_time=now; last_block=s['block']
                show(s,cfg)
                side=buffered_exit(s,cfg.exit_buffer_percent)
                ds=distances(s['price_usdc_per_weth'],s['lower_price'],s['upper_price'])
                result = None
                try:
                    gas_price = None
                    if cfg.gas_units_a is not None or cfg.gas_units_b is not None:
                        try: gas_price = w3.eth.gas_price
                        except Exception: pass
                    result = simulate(s, cfg.range_percent, cfg.gas_units_a, cfg.gas_units_b, gas_price, cfg.router_bps, cfg.slippage_bps, cfg.impact_bps)
                except Exception as exc:
                    write_log(cfg.log,'simulation_error',chain=cfg.chain,wallet=cfg.wallet,manager=cfg.manager,block=s['block'],error_type=type(exc).__name__)
                    print(f'Simulation: UNKNOWN ({type(exc).__name__})',flush=True)
                if s.get('fees',{}).get('status') == 'UNKNOWN':
                    write_log(cfg.log,'partial_data',chain=cfg.chain,wallet=cfg.wallet,manager=cfg.manager,block=s['block'],field='fees',error_type=s['fees'].get('error_type'))
                write_log(cfg.log, 'snapshot', chain=cfg.chain, wallet=cfg.wallet, manager=cfg.manager, buffered_exit=side, distances=ds, simulation=result, **s)
                latest={'time':stamp(), **s}
                ready = gate.observe(side is None, now, s['block'], side, identity, s['status']=='IN_RANGE')
                state['latch']=gate.latched
                state['signals']=list(gate.signals)
                state['latest']={'time':stamp(),'token_id':s['token_id'],'pool':s['pool'],'lower':s['lower'],'upper':s['upper'],'liquidity':s['liquidity']}
                save_state(cfg.log,state)
                if (ready or simulate_now) and result is not None:
                    kind = 'signal' if ready and not simulate_now else 'manual_simulation'
                    if kind == 'signal':
                        gate.record(now)
                        state['signals']=list(gate.signals);state['latch']=gate.latched
                        save_state(cfg.log,state)
                    write_log(cfg.log, kind, chain=cfg.chain, wallet=cfg.wallet, manager=cfg.manager, token_id=s['token_id'], block=s['block'], side=side,position_identity=identity, simulation=result)
                    show_comparison(result)
                    if verbose_json: print(f"{kind.upper()}: {json.dumps(result, default=str, ensure_ascii=False)}", flush=True)
            except DurableStateError:
                raise
            except Exception as e:
                gate.reset_observation()
                failures+=1
                message = str(e).replace(cfg.rpc, '<RPC_URL>')
                print(f"ERROR {type(e).__name__}: {message}", flush=True)
                category='rpc' if isinstance(e,(RequestException,TimeoutError,ConnectionError,Web3Exception)) else 'data_or_monitor'
                if category=='rpc': rpc_failures+=1
                write_log(cfg.log, 'error', chain=cfg.chain,wallet=cfg.wallet,manager=cfg.manager,error_category=category,error_type=type(e).__name__, message=message)
                if not once and failures == 1:
                    write_log(cfg.log,'gap',chain=cfg.chain,wallet=cfg.wallet,manager=cfg.manager,reason='rpc_or_monitor_error', previous_observation_time=latest.get('time') if latest else None,resumed_block=None)
                if once: return 1
                w3 = connect(cfg)
            if once: return 0
            desired=backoff_delay(rpc_failures) if rpc_failures else (max(5,cfg.poll) if failures else cfg.poll)
            delay=desired if deadline is None else max(0,min(desired,deadline-time.time(),monotonic_deadline-time.monotonic()))
            if delay: time.sleep(delay)
    except StopRequested:
        if wid is not None and (state.get('watch') is None or complete_watch(cfg,state)):
            completed=True
            print('Наблюдение завершено.',flush=True)
    finally:
        if wid is not None and not completed:
            write_log(cfg.log,'watch_paused',chain=cfg.chain,wallet=cfg.wallet,manager=cfg.manager,watch_id=wid,deadline_epoch=deadline)
    return 0


def run(cfg, once=False, simulate_now=False, duration_seconds=None, verbose_json=False):
    # One writer per state/log pair; lock stays in the configured writable directory.
    p=Path(cfg.log);p.parent.mkdir(parents=True,exist_ok=True)
    fd=os.open(str(p)+'.lock',os.O_CREAT | os.O_RDWR,0o600)
    try:
        try: fcntl.flock(fd,fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: raise RuntimeError('monitor already running for this LOG_PATH')
        previous = None
        if threading.current_thread() is threading.main_thread():
            previous=signal.getsignal(signal.SIGTERM)
            def on_term(signum,frame): raise StopRequested()
            signal.signal(signal.SIGTERM,on_term)
        try:
            try: return _run(cfg,once,simulate_now,duration_seconds,verbose_json)
            except StopRequested:
                # SIGTERM can also arrive while creating or restoring the watch.
                state=load_state(cfg.log)
                if state and state.get('watch'):
                    if not complete_watch(cfg,state):
                        watch=state['watch']
                        write_log(cfg.log,'watch_paused',chain=cfg.chain,wallet=cfg.wallet,manager=cfg.manager,watch_id=watch['watch_id'],deadline_epoch=watch['deadline_epoch'])
                return 0
        finally:
            if previous is not None: signal.signal(signal.SIGTERM,previous)
    finally:
        os.close(fd)
