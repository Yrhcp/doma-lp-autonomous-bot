# Doma LP Autonomous Bot

An experimental Python monitor and transaction executor for a dedicated
Uniswap V3-style WETH/USDC.e liquidity position on Doma (chain ID `97477`).
The autonomous mode follows an NFT position, detects a confirmed price exit,
and can close and reopen it through individually verified transactions.

**This is experimental software that can lose funds.** It has not had an
independent security audit. Use only an isolated wallet with funds you can
afford to lose. The LP pool price is not an independent oracle, USDC.e can
depeg, swaps can move the market, and gas and L1/data fees can change. Fee
estimates are not guarantees and the project does not calculate complete net
profit and loss. The operator is responsible for securing the machine, RPC,
wallet and journal.

## Features

- Read-only position, tick, token and estimated fee-growth monitoring.
- A dedicated-wallet autonomous mode for the configured Doma contracts and
  0.01% pool; contract identities and token/NFT ownership are checked.
- A fixed **−15/+15 USDC.e per WETH** target range, rounded to usable pool
  ticks. An exit must exceed the existing 0.1% buffer and persist for 60
  seconds. There is no one-hour cooldown or daily rebalance quota in this mode.
- Sequential decrease, collect, limited approvals, swap when needed, and mint.
  Each step uses fresh data, an `eth_call` check, amount limits and receipt
  reconciliation before advancing. Swap slippage is limited to 0.5%.
- After collect, the bot snapshots all available WETH and USDC.e in the
  dedicated wallet, including leftovers from previous cycles, and uses that
  saved budget to calculate the swap and mint. Native ETH remains for gas.
  Transfers received after this snapshot are considered in the next cycle.
  The bot can leave dust because of rounding, price moves, and its existing
  threshold that skips an indicative swap worth at most 0.01 USDC.e.
- Durable state, saved pending transaction hashes, position tracking across
  restarts, and a local process lock. Unknown transaction outcomes stop progress
  rather than being resent automatically.
- An unsigned pending mint with an expired deadline or stale price can be
  rebuilt after checking wallet nonces, the pool, balances and receipts for
  completed steps. The new mint is simulated before it replaces the pending
  record atomically; completed collect and swap operations are not repeated.
  If the changed price would require another swap, the bot may leave tokens
  outside the position or stop for operator review.
- A current-fee estimate for all eight possible steps with a 25% buffer before
  starting a cycle, per-step gas limits, and a warning when native ETH is below
  a target of $2 using Coinbase ETH/USD spot. The $2 target is not a hard
  stop. The bot does not sell or wrap native ETH to replenish gas.

The pre-cycle estimate uses current fee data and conservative gas limits. It
cannot guarantee the cost or completion of future steps. The warning's USD
conversion is an estimate from one external price source, not a verified
oracle. Transaction failures can leave assets outside the LP and require
operator intervention. Native ETH must remain available for gas.

## Requirements and installation

Python 3.11 or later, a reachable Doma RPC endpoint, and Linux for the file
locking used by the monitor. Start with a separate, low-value wallet and a
position you explicitly control. Never use your primary wallet.

```bash
git clone https://github.com/YOUR-ACCOUNT/doma-lp-autonomous-bot.git
cd doma-lp-autonomous-bot
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
cp .env.example .env
```

Edit `.env` locally. Set `WALLET_ADDRESS` and
`AUTONOMOUS_WALLET_ADDRESS` to the same dedicated wallet, and set
`AUTONOMOUS_INITIAL_NFT` to its initial position ID. List positions that must
never be touched as comma-separated `PROTECTED_NFT_IDS`. Confirm the pool,
contract addresses, network and RPC independently before any use. Do not
commit `.env`, keys, logs or state.

Autonomous execution uses a local key file at
`~/.local/share/doma-lp/test-wallet.key`, owned by the running user with no
group/other access. If you choose to use local signing, create it in an
interactive terminal with hidden input:

```bash
.venv/bin/python tools/create_test_key.py
.venv/bin/doma-lp --test-key-check
```

The program verifies that the key matches the configured dedicated wallet;
neither command submits a transaction. Do not put a seed phrase or key in
`.env`, terminal history, chat, version control or logs. A compromised host,
its administrator or root can still steal a local key.

## Run

Read one position snapshot without transactions:

```bash
.venv/bin/doma-lp --once
```

Watch and locally rehearse the first required action without broadcasting:

```bash
.venv/bin/doma-lp --autonomous-dry-run
```

Only after reviewing the code and configuration, explicitly enable real
transactions:

```bash
.venv/bin/doma-lp --autonomous-live --authorize-real-transactions
```

This command can sign and send **real transactions automatically**. Stop it
with Ctrl+C. A process supervisor must be configured separately. Do not run
the same wallet on two machines: locks are local and do not coordinate across
hosts. Do not copy a live or partially written journal between machines. Stop
the first instance, verify the actual on-chain state and pending transaction,
then preserve the complete journal before starting a successor.

Autonomous state is stored below `~/.local/share/doma-lp/`, including the
signal state and one file per rebalance cycle. These files are essential for
recovery and duplicate prevention; protect and back them up privately. The
read-only monitor uses `LOG_PATH` for diagnostic logs and related state.

An existing pending mint with a saved hash is never automatically replaced,
even when an RPC reports matching latest and pending nonces: the transaction
may have reached a different or private mempool. Reconcile it manually. An
unfinished cycle created by an earlier version may also require review before
an upgrade; do not clear or overwrite its state to bypass this protection.

For a systemd user service using `Restart=on-failure`, the optional drop-in
[`deploy/autonomous-stop-restart-loop.conf`](deploy/autonomous-stop-restart-loop.conf)
prevents restarting a persistent autonomous `Blocked` error (exit status 78)
and limits other repeated starts. Install and reload the drop-in as part of a
deliberate service deployment; it is not applied automatically by the CLI.

The legacy manual Rabby UI requires separate `MANUAL_INITIAL_NFT` and
`MANUAL_INITIAL_MINT_TX` settings. It should be independently reviewed before
use and is not required by the autonomous mode.

## Tests

```bash
.venv/bin/python -m pytest -q
```

The tests use synthetic values and do not establish production safety. Fork
research helpers are separate from the live mode and must be used only with
an isolated local fork and synthetic keys.

## License

GPL-2.0-or-later. See [LICENSE](LICENSE) and [NOTICE](NOTICE). The TickMath
implementation is adapted from Uniswap v3-core, whose source carries the same
license; the project is therefore not represented as MIT-licensed.
