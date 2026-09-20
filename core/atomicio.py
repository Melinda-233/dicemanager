"""原子读写：同目录 mkstemp + os.replace + fsync，杜绝半写文件"""
import json
import os
import tempfile
from pathlib import Path
from threading import Lock

_write_lock = Lock()

def write_atomic(path, data: bytes):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp_", suffix=path.name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data); f.flush(); os.fsync(f.fileno())
        os.replace(tmp, path)                       # 原子替换
    finally:
        if os.path.exists(tmp): os.unlink(tmp)

def read_json_any(path) -> dict:
    """JSON；含注释/尾随逗号（JSON5，LLBot 配置）时回退 json5。"""
    text = Path(path).read_text("utf-8")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        import json5  # pip install json5
        return json5.loads(text)

def atomic_write_json(path, mutate, source_json5: bool = False) -> dict:
    with _write_lock:
        p = Path(path)
        data = read_json_any(p) if (p.exists() and source_json5) else \
               (json.loads(p.read_text("utf-8")) if p.exists() else {})
        result = mutate(data)
        if result is not None: data = result
        write_atomic(p, json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"))
        return data

def atomic_write_text(path, text: str):
    write_atomic(Path(path), text.encode("utf-8"))
