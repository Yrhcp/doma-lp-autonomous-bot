import argparse
import json
from .config import Config
from .monitor import run

def main():
    parser = argparse.ArgumentParser(description='Doma LP monitor and separately authorized test-wallet automation')
    mode=parser.add_mutually_exclusive_group()
    mode.add_argument('--once', action='store_true', help='one snapshot')
    mode.add_argument('--simulate', action='store_true', help='one diagnostic comparison without spending signal quota')
    mode.add_argument('--diagnostic', action='store_true', help='one diagnostic comparison without spending signal quota')
    mode.add_argument('--watch24', action='store_true', help='monitor for 24 hours; resume unfinished window after restart')
    mode.add_argument('--watch-seconds', type=float, help='bounded observation duration in seconds, useful for short tests')
    mode.add_argument('--prepare50', action='store_true', help='read-only blocked preflight for a separate new LP')
    mode.add_argument('--manual-open5', action='store_true', help='prepare one manual LP position, no signing or sending')
    mode.add_argument('--rabby-local', action='store_true', help='local Rabby interface for manual-open5')
    mode.add_argument('--rabby-rebalance', action='store_true', help='local manual rebalance for the journal active NFT')
    mode.add_argument('--test-key-check', action='store_true', help='verify the local test-wallet key without displaying it or sending transactions')
    mode.add_argument('--autonomous-dry-run', action='store_true', help='monitor test NFT and rehearse the next step; never broadcast')
    mode.add_argument('--autonomous-live', action='store_true', help='monitor and rebalance the dedicated test NFT; requires explicit authorization flag')
    mode.add_argument('--rebalance-internal', action='store_true', help=argparse.SUPPRESS)
    mode.add_argument('--compare-nfts', nargs=2, type=int, metavar=('NFT_001','NFT_005'), help='read-only comparison of two specified new NFT positions')
    mode.add_argument('--finalize-watch', action='store_true', help='reconcile an expired watch without RPC')
    mode.add_argument('--report', nargs='?', const='', metavar='JSONL', help='analyze the configured log or a specified JSONL file offline')
    parser.add_argument('--all-history', action='store_true', help='with --report, include the whole journal instead of the latest watch')
    parser.add_argument('--compare-watch', action='store_true', help='with --compare-nfts, continuously refresh until interrupted')
    parser.add_argument('--json', action='store_true', help='show detailed JSON for diagnostics or report')
    parser.add_argument('--authorize-real-transactions', action='store_true', help='explicitly authorize real transactions with --autonomous-live')
    args = parser.parse_args()
    try:
        cfg = Config.load()
        if args.authorize_real_transactions and not args.autonomous_live:
            raise ValueError('--authorize-real-transactions requires --autonomous-live')
        if args.autonomous_live:
            if not args.authorize_real_transactions:
                raise ValueError('реальная отправка требует --authorize-real-transactions')
            from .autonomous_cycle import run_authorized_monitor
            run_authorized_monitor(cfg, authorized=True)
            return
        if args.autonomous_dry_run:
            from .autonomous_cycle import run_dry_monitor
            run_dry_monitor(cfg)
            return
        if args.test_key_check:
            from pathlib import Path
            from .autonomous_dry_run import _account_from_file, TEST_WALLET
            account = _account_from_file(str(Path.home()/'.local/share/doma-lp/test-wallet.key'))
            if account.address != TEST_WALLET:
                raise ValueError('файл не соответствует тестовому кошельку')
            print('Тестовый ключ подтверждён; транзакции не подписывались и не отправлялись.')
            return
        if args.rabby_rebalance:
            from .rabby_local import run as run_rabby
            run_rabby(cfg,rebalance=True)
            return
        if args.rebalance_internal:
            from .rebalance_manual import run as run_rebalance
            raise SystemExit(run_rebalance(cfg))
        if args.rabby_local:
            from .rabby_local import run as run_rabby
            run_rabby(cfg)
            return
        if args.manual_open5:
            from .manual_open import run as run_manual
            raise SystemExit(run_manual(cfg))
        if args.compare_watch and args.compare_nfts is None: raise ValueError('--compare-watch requires --compare-nfts')
        if args.compare_nfts is not None:
            from .compare_positions import run as run_comparison
            raise SystemExit(run_comparison(cfg,args.compare_nfts,watch=args.compare_watch))
        if args.report is not None:
            from .report import analyze,render
            result=analyze(args.report or cfg.log,cfg,latest_watch=not args.all_history)
            if not args.report or args.report == cfg.log:
                from .storage import load_state
                import time
                try:
                    state=load_state(cfg.log)
                    if state and state.get('watch') and state['watch'].get('deadline_epoch',float('inf')) <= time.time():
                        result['warnings'].append('Окно просрочено без финализации: после остановки службы выполните --finalize-watch, затем повторите отчёт.')
                except Exception:
                    result['warnings'].append('Состояние недоступно или повреждено; завершение окна не подтверждено.')
            print(json.dumps(result,default=str,ensure_ascii=False,indent=2) if args.json else render(result))
            return
        if args.all_history: raise ValueError('--all-history is only valid with --report')
        if args.prepare50:
            from .preflight import live_preflight
            plan=live_preflight(cfg)
            print(json.dumps(plan,default=str,ensure_ascii=False,indent=2))
            raise SystemExit(2)  # BLOCKED; automation must never interpret this as readiness
        if args.finalize_watch:
            from .monitor import finalize_watch
            raise SystemExit(finalize_watch(cfg))
        duration=86400.0 if args.watch24 else args.watch_seconds
        if duration is not None:
            import math
            if not math.isfinite(duration) or duration <= 0: raise ValueError('watch duration must be finite and positive')
        raise SystemExit(run(cfg, once=args.once or args.simulate or args.diagnostic,
                             simulate_now=args.simulate or args.diagnostic, duration_seconds=duration, verbose_json=args.json))
    except KeyboardInterrupt:
        print('\nStopped; журнал сохранён.')
    except Exception as e:
        print(f'ERROR {type(e).__name__}: {e}')
        raise SystemExit(1)

if __name__ == '__main__': main()
