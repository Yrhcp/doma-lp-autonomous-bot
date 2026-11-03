"""Loopback-only UI for the existing manual-open5 CLI. No signing on server."""
import hmac
import json
import secrets
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from .config import Config
from .rpc import connect
from .manual_open import verify_before_wallet

MAX_BODY=4096

class Session:
    def __init__(self,cfg,rebalance=False):
        self.cfg=cfg;self.lock=threading.RLock();self.call=None;self.attempted=False;self.awaiting=False;self.status='Запускаю проверку сети…';self.error=None
        self.rebalance=rebalance;self.failed=False;self.hash_saved=threading.Event()
        self._start_child()
    def _start_child(self):
        self.child=subprocess.Popen([sys.executable,'-u','-m','doma_lp_bot.cli','--rebalance-internal' if self.rebalance else '--manual-open5'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,bufsize=0)
        self.start_reader()
    def _read_utf8(self):
        import codecs
        dec=codecs.getincrementaldecoder('utf-8')('replace');buf='';json_mode=False
        while True:
            chunk=self.child.stdout.read(1)
            if not chunk:break
            text=dec.decode(chunk)
            if not text:continue
            buf+=text
            if 'Проверьте все поля в кошельке.' in buf:json_mode=True
            if json_mode and '{' in buf:
                try:
                    obj,end=json.JSONDecoder().raw_decode(buf[buf.index('{'):])
                    if isinstance(obj,dict) and {'chainId','to','from','data','step'}<=obj.keys():
                        with self.lock:self.call=obj;self.attempted=False;self.awaiting=True;self.status='Данные проверены. Сверьте их и откройте Rabby.';self.error=None
                        buf='';json_mode=False
                except json.JSONDecodeError:pass
            if 'Ожидается подтверждение ранее показанного вызова:' in buf and '\n' in buf[buf.index('Ожидается подтверждение ранее показанного вызова:'):]:
                with self.lock:
                    self.awaiting=True
                    if self.call is None:self.status='Незавершённый шаг: введите hash из Rabby. Повторная отправка заблокирована.'
                buf=''
            if 'Ожидается проверка сохранённого hash:' in buf and '\n' in buf[buf.index('Ожидается проверка сохранённого hash:'):]:
                with self.lock:
                    self.call=None;self.awaiting=False;self.attempted=True
                    self.status='Hash сохранён; проверяю receipt и подтверждения. Повторная отправка заблокирована.'
                buf=''
            if 'Цена изменилась; пересчитываю mint' in buf:
                with self.lock:self.status='Цена изменилась; пересчитываю mint на свежем блоке…'
                buf=''
            if 'Котировка изменилась; безопасно пересчитываю swap' in buf:
                with self.lock:self.status='Котировка изменилась; пересчитываю swap на свежем блоке…'
                buf=''
            if 'ПОДТВЕРЖДЁННЫЙ ОТКАЗ:' in buf:
                with self.lock:self.awaiting=True;self.failed=True;self.status='Транзакция завершилась ошибкой. Можно проверить состояние и пересчитать.'
                buf=''
            if 'HASH_SAVED' in buf:
                self.hash_saved.set();buf=''
            if 'HASH_REJECTED:' in buf and '\n' in buf[buf.index('HASH_REJECTED:'):]:
                message=buf.split('HASH_REJECTED:',1)[1].split('\n',1)[0].strip()
                with self.lock:
                    self.hash_error=message;self.awaiting=True;self.attempted=True;self.error=message
                    self.status='Hash не принят; введите hash текущего шага.'
                self.hash_saved.set();buf=''
            if len(buf)>32768:buf=buf[-8192:]
        code=self.child.wait()
        with self.lock:
            self.call=None
            tail=buf.replace(self.cfg.rpc,'<RPC_URL>')[-1000:]
            if code==0:
                self.status=tail.strip() or 'Локальная команда завершилась.';self.error=None
            else:
                self.status='Локальная команда остановилась.';self.error=tail.strip() or f'Код завершения {code}'
    def start_reader(self):
        self.reader=threading.Thread(target=self._read_utf8,daemon=True);self.reader.start()
    def close(self):
        if self.child.poll() is None:
            self.child.terminate()
            try:self.child.wait(timeout=3)
            except subprocess.TimeoutExpired:self.child.kill();self.child.wait(timeout=3)
    def state(self):
        with self.lock:return {'status':self.status,'error':self.error,'call':self.call,'attempted':self.attempted,'awaiting':self.awaiting,'running':self.child.poll() is None,'rebalance':getattr(self,'rebalance',False),'failed':getattr(self,'failed',False)}
    def arm(self):
        with self.lock:
            if not self.awaiting or not self.call or self.attempted or self.child.poll() is not None:raise ValueError('Повторная отправка заблокирована; проверьте hash в Rabby.')
            verify=verify_before_wallet
            if getattr(self,'rebalance',False):
                from .rebalance_manual import verify_before_wallet as verify
            w=connect(self.cfg); cfg=self.cfg
            if getattr(self,'rebalance',False):
                from .rebalance_manual import resolve_cycle
                cfg=resolve_cycle(w,cfg)
            fresh=verify(w,cfg,self.call)
            self.attempted=True;self.status='Ожидайте окно подтверждения Rabby; не нажимайте повторно.'
            return {'call':self.call,'revalidation':fresh}
    def submit(self,value):
        with self.lock:
            if self.child.poll() is not None or not self.awaiting:raise ValueError('Нет шага, ожидающего hash; проверьте состояние команды.')
            if not isinstance(value,str) or len(value)!=66 or not value.startswith('0x'):
                raise ValueError('Нужен hash из 32 байт в формате 0x…')
            try:bytes.fromhex(value[2:])
            except ValueError as e:raise ValueError('Некорректный hash') from e
            if getattr(self,'rebalance',False):
                from .storage import load_state
                from .rebalance_manual import scope,cycle_base,resolve_cycle
                cfg=resolve_cycle(connect(self.cfg),self.cfg)
                saved=load_state(cycle_base(cfg))
                if not saved or saved.get('scope')!=scope(cfg) or not saved.get('pending'):
                    raise ValueError('Ожидающий шаг отсутствует в журнале; hash не принят')
                if any(str(x.get('hash','')).lower()==value.lower() for x in saved.get('completed',[])):
                    raise ValueError('Этот hash уже подтверждён на предыдущем шаге; введите hash текущей операции')
                pending=saved['pending']
                if pending.get('hash') or (self.call is not None and pending.get('call')!=self.call):
                    raise ValueError('Ожидающий шаг изменился; проверьте журнал')
                self.hash_error=None;self.hash_saved.clear()
            self.call=None;self.attempted=True;self.awaiting=False;self.status='Сверяю транзакцию и подтверждения…'
            self.child.stdin.write((value+'\n').encode());self.child.stdin.flush()
        if getattr(self,'rebalance',False):
            if not self.hash_saved.wait(timeout=20):
                raise ValueError('Не удалось подтвердить сохранение hash; проверьте hash в Rabby и журнал перед продолжением.')
            if self.hash_error:raise ValueError(self.hash_error)
    def cancel(self):
        with self.lock:
            if self.child.poll() is not None or not self.awaiting:raise ValueError('Нет ожидающего шага')
            self.call=None;self.attempted=True;self.awaiting=False;self.status='Проверяю nonce перед отменой…'
            self.child.stdin.write(b'cancel\n');self.child.stdin.flush()
    def wallet_rejected(self,code):
        """Release only an explicitly rejected decrease, after journal/RPC checks."""
        from .storage import load_state
        from .rebalance_manual import scope,cycle_base,resolve_cycle
        from .rpc import check_chain
        with self.lock:
            if (type(code) is not int or code!=4001 or not getattr(self,'rebalance',False) or
                not self.awaiting or not self.attempted or not self.call or
                self.call.get('step')!='decrease' or self.child.poll() is not None):
                raise ValueError('Явный отказ Rabby для ожидающего decrease не подтверждён; повтор заблокирован.')
            w=connect(self.cfg);cfg=resolve_cycle(w,self.cfg)
            saved=load_state(cycle_base(cfg))
            pending=saved.get('pending') if saved and saved.get('scope')==scope(cfg) else None
            if not pending or pending.get('hash') or pending.get('call')!=self.call:
                raise ValueError('Ожидающий шаг или hash в журнале изменился; повтор заблокирован.')
            check_chain(w,97477);nonce=self.call['nonce']
            if w.eth.get_transaction_count(self.cfg.wallet,'latest')!=nonce or w.eth.get_transaction_count(self.cfg.wallet,'pending')!=nonce:
                raise ValueError('Nonce изменился или есть ожидающая транзакция; повтор заблокирован.')
            # The child rechecks both nonces before atomically clearing pending.
            self.child.stdin.write(b'cancel\n');self.child.stdin.flush()
            self.call=None;self.awaiting=False;self.status='Отказ Rabby подтверждён; пересчитываю на свежем блоке…';self.error=None
    def wallet_unknown(self):
        with self.lock:
            if not self.awaiting or not self.attempted or not self.call or self.child.poll() is not None:
                raise ValueError('Нет ожидаемого результата кошелька')
            self.status='Результат неизвестен. Проверьте историю Rabby; повторная отправка заблокирована.'
    def retry_failed(self):
        with self.lock:
            if not getattr(self,'failed',False) or not self.awaiting or self.child.poll() is not None:raise ValueError('Нет подтверждённого отказа')
            self.failed=False;self.awaiting=False;self.status='Проверяю receipt, nonce и состояние позиции…'
            self.child.stdin.write(b'retry\n');self.child.stdin.flush()
    def refresh(self):
        with self.lock:
            if self.child.poll() is None:
                if not self.awaiting or not self.call or self.attempted:raise ValueError('Сначала проверьте результат в Rabby или введите hash; повтор заблокирован.')
                self.call=None;self.attempted=True;self.awaiting=False;self.status='Пересчитываю на свежем блоке…';self.error=None
                self.child.stdin.write(b'cancel\n');self.child.stdin.flush()
            else:
                if self.reader.is_alive():raise ValueError('Дождитесь завершения предыдущей проверки и повторите.')
                self.call=None;self.attempted=False;self.awaiting=False;self.status='Пересчитываю на свежем блоке…';self.error=None
                self._start_child()

class Server(ThreadingHTTPServer):
    daemon_threads=True
    def __init__(self,session,token):
        self.session=session;self.token=token
        super().__init__(('127.0.0.1',0),Handler)
        self.origin=f'http://127.0.0.1:{self.server_port}'

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def _send(self,status,body,typ='application/json; charset=utf-8'):
        raw=body if isinstance(body,bytes) else json.dumps(body,ensure_ascii=False).encode()
        self.send_response(status);self.send_header('Content-Type',typ);self.send_header('Content-Length',str(len(raw)));self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff');self.send_header('Referrer-Policy','no-referrer');self.send_header('X-Frame-Options','DENY')
        self.send_header('Content-Security-Policy',"default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
        self.end_headers();self.wfile.write(raw)
    def _host_ok(self):return self.headers.get('Host')==self.server.origin.removeprefix('http://')
    def _authorized(self):return self._host_ok() and hmac.compare_digest(self.headers.get('X-Local-Token',''),self.server.token)
    def do_GET(self):
        if not self._host_ok():return self._send(403,{'error':'invalid host'})
        if self.path=='/':return self._send(200,Path(__file__).with_name('rebalance_preview.html' if getattr(self.server.session,'preview_only',False) else 'rabby_local.html').read_bytes(),'text/html; charset=utf-8')
        if self.path=='/api/state' and self._authorized():return self._send(200,self.server.session.state())
        return self._send(403,{'error':'forbidden'})
    def do_POST(self):
        if not self._authorized() or self.headers.get('Origin')!=self.server.origin:return self._send(403,{'error':'forbidden'})
        try:
            if self.headers.get_content_type()!='application/json':raise ValueError('application/json required')
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<=MAX_BODY:raise ValueError('invalid request size')
            body=json.loads(self.rfile.read(length))
            if self.path=='/api/arm':result=self.server.session.arm();return self._send(200,result)
            if self.path=='/api/hash':self.server.session.submit(body.get('hash'));return self._send(200,{'ok':True})
            if self.path=='/api/cancel':self.server.session.cancel();return self._send(200,{'ok':True})
            if self.path=='/api/wallet-rejected':self.server.session.wallet_rejected(body.get('code'));return self._send(200,{'ok':True})
            if self.path=='/api/wallet-unknown':self.server.session.wallet_unknown();return self._send(200,{'ok':True})
            if self.path=='/api/retry-failed':self.server.session.retry_failed();return self._send(200,{'ok':True})
            if self.path=='/api/refresh':self.server.session.refresh();return self._send(200,{'ok':True})
            return self._send(404,{'error':'not found'})
        except Exception as e:return self._send(400,{'error':str(e).replace(self.server.session.cfg.rpc,'<RPC_URL>')})

def run(cfg,rebalance=False):
    session=Session(cfg,rebalance=rebalance);token=secrets.token_urlsafe(32);server=Server(session,token)
    print(f'Откройте в браузере с Rabby: {server.origin}/#{token}',flush=True)
    print('Остановка: Ctrl+C. Ни сервер, ни бот не подписывают транзакции.',flush=True)
    try:server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:pass
    finally:server.server_close();session.close()

class PreviewSession:
    preview_only=True
    def __init__(self,cfg):
        self.cfg=cfg;self.lock=threading.RLock();self.result=None;self.error=None;self.running=False
        self.refresh()
    def refresh(self):
        with self.lock:
            if self.running:raise ValueError('Проверка уже выполняется')
            self.running=True;self.error=None;self.result=None
        threading.Thread(target=self._read,daemon=True).start()
    def _read(self):
        from .rebalance_preview import preview
        try:
            result=json.loads(json.dumps(preview(connect(self.cfg),self.cfg),default=str,ensure_ascii=False))
            with self.lock:self.result=result
        except Exception as e:
            with self.lock:self.error=str(e).replace(self.cfg.rpc,'<RPC_URL>')
        finally:
            with self.lock:self.running=False
    def state(self):
        with self.lock:return {'running':self.running,'result':self.result,'error':self.error}
    def arm(self):raise ValueError('Подготовка транзакций отключена')
    def submit(self,value):raise ValueError('Подготовка транзакций отключена')
    def cancel(self):raise ValueError('Подготовка транзакций отключена')
    def close(self):pass

def run_preview(cfg):
    session=PreviewSession(cfg);server=Server(session,secrets.token_urlsafe(32))
    print(f'Откройте локальный просмотр: {server.origin}/#{server.token}',flush=True)
    print('Только просмотр. Транзакции отключены. Остановка: Ctrl+C.',flush=True)
    try:server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:pass
    finally:server.server_close();session.close()
