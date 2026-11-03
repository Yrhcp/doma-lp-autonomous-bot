"""Interactive one-time secret setup; never print the secret or replace a file."""
import getpass
import os
import sys
import warnings
from pathlib import Path
from eth_account import Account
from dotenv import load_dotenv
load_dotenv()
from doma_lp_bot.autonomous_dry_run import TEST_WALLET


def interactive_terminal():
    return sys.stdin.isatty() and sys.stderr.isatty()


def main():
    target = Path.home() / '.local/share/doma-lp/test-wallet.key'
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not interactive_terminal():
        raise SystemExit('Нужен интерактивный терминал со скрытым вводом; файл не создан.')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', getpass.GetPassWarning)
            secret = getpass.getpass('Приватный ключ тестового кошелька (ввод скрыт): ').strip()
    except getpass.GetPassWarning:
        raise SystemExit('Не удалось скрыть ввод; файл не создан.') from None
    try:
        account = Account.from_key(secret)
    except Exception:
        raise SystemExit('Неверный формат ключа; файл не создан.') from None
    if account.address != TEST_WALLET:
        raise SystemExit('Ключ не соответствует тестовому кошельку; файл не создан.')
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    except FileExistsError:
        raise SystemExit('Файл уже существует; он не изменён.') from None
    with os.fdopen(fd, 'wb') as f:
        f.write(secret.encode('ascii'))
        f.write(b'\n')
        f.flush()
        os.fsync(f.fileno())
    print('Файл создан с правами 600:', target)


if __name__ == '__main__':
    main()
