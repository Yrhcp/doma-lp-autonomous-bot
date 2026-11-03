"""Local bounded diagnostic logs and crash-safe persistent state (Linux)."""
import json
import os
import tempfile
from pathlib import Path

MAX_BYTES = 10_000_000
FILE_COUNT = 5

def log_files(path):
    p=Path(path)
    return [p.with_name(p.name+f'.{i}') for i in range(FILE_COUNT-1,0,-1) if p.with_name(p.name+f'.{i}').exists()] + ([p] if p.exists() else [])

def sync_dir(path):
    fd=os.open(path,os.O_RDONLY | getattr(os,'O_DIRECTORY',0))
    try: os.fsync(fd)
    finally: os.close(fd)

def state_path(log_path):
    p=Path(log_path)
    return p.with_name(p.name+'.state.json')

def save_state(log_path,state):
    p=state_path(log_path);p.parent.mkdir(parents=True,exist_ok=True)
    fd,name=tempfile.mkstemp(prefix='.'+p.name+'.',suffix='.tmp',dir=p.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf8') as f:
            json.dump(state,f,ensure_ascii=False,separators=(',',':'),allow_nan=False)
            f.write('\n');f.flush();os.fsync(f.fileno())
        os.replace(name,p);sync_dir(p.parent)
    finally:
        if os.path.exists(name): os.unlink(name)

def load_state(log_path):
    p=state_path(log_path)
    if not p.exists(): return None
    try:
        with p.open(encoding='utf8') as f: s=json.load(f)
        if not isinstance(s,dict) or s.get('version')!=1 or not isinstance(s.get('signals'),list) or not isinstance(s.get('scope'),dict) or 'latch' not in s or 'watch' not in s or 'latest' not in s:
            raise ValueError('invalid state schema')
        return s
    except (ValueError,TypeError,KeyError) as e:
        raise ValueError('durable state damaged; stop and restore backup') from e

def append_record(path,payload):
    p=Path(path);p.parent.mkdir(parents=True,exist_ok=True)
    raw=payload.encode('utf8')
    if len(raw)>MAX_BYTES: raise ValueError('log record exceeds file limit')
    recovered=False
    if p.exists() and p.stat().st_size > MAX_BYTES:
        raise ValueError('legacy log exceeds 10 MB: after state migration archive it separately before restarting')
    if p.exists() and p.stat().st_size:
        # Inspect the last record only. A truncated final record is discarded;
        # critical state is never reconstructed from this diagnostic file.
        with p.open('rb+') as f:
            f.seek(0,os.SEEK_END); end=f.tell(); start=max(0,end-MAX_BYTES)
            f.seek(start); tail=f.read(); trimmed=tail[:-1] if tail.endswith(b'\n') else tail
            offset=trimmed.rfind(b'\n')+1
            last=trimmed[offset:]
            try:
                if not tail.endswith(b'\n'): raise ValueError('incomplete')
                json.loads(last.decode('utf8'))
            except (ValueError,UnicodeDecodeError):
                if start and offset==0: raise ValueError('oversized or corrupted log tail')
                f.seek(start+offset);f.truncate();f.flush();os.fsync(f.fileno());recovered=True
    if recovered:
        marker=json.dumps({'event':'log_recovery','note':'invalid final record removed; history incomplete'}).encode()+b'\n'
        raw=marker+raw
    if len(raw)>MAX_BYTES: raise ValueError('log record exceeds file limit')
    if p.exists() and p.stat().st_size+len(raw)>MAX_BYTES:
        oldest=p.with_name(p.name+f'.{FILE_COUNT-1}')
        if oldest.exists(): oldest.unlink()
        for i in range(FILE_COUNT-2,0,-1):
            a=p.with_name(p.name+f'.{i}');b=p.with_name(p.name+f'.{i+1}')
            if a.exists(): os.replace(a,b)
        os.replace(p,p.with_name(p.name+'.1'));sync_dir(p.parent)
    with p.open('ab') as f:
        f.write(raw);f.flush();os.fsync(f.fileno())
    return recovered
