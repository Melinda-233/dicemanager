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


# ---------- dotenv（nonebot2 的 .env）----------
# pydantic-settings 读 .env，语义与 JSON 有本质差异，故不能复用 atomic_write_json：
#   · 值是字符串常量，不是 JSON；写 OB11_WS_URLS=["ws://..."] 时引号由python-dotenv
#     按 shell 规则处理，我们写入时统一不带引号，避免"写出去≠ 读回来"
#   · 注释（#）与空行是合法内容，剥掉等于毁掉用户手写的说明
#   · 重复键后者胜出（同 shell），按行序处理天然满足


def _unquote(val: str) -> str:
    """剥掉配对的单/双引号；未配对时原样返回（宁可不动也不截断）。"""
    v = val.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        return v[1:-1]
    return val


def _split_key(raw: str) -> tuple[str, str] | None:
    """从一行里取出 (key, 原始值)；注释/空行/无等号返回 None。"""
    line = raw.strip()
    if not line or line.startswith("#"):
        return None
    if line.startswith("export "):                # shell 习惯的导出写法
        line = line[len("export "):].lstrip()
    key, sep, val = line.partition("=")
    if not sep:
        return None
    return key.strip(), _unquote(val)


def read_dotenv(path) -> dict:
    """读 .env 为 dict；文件缺失/损坏返回 {}。

    只按首个等号切分（token 里可能含 =）。配对引号会被剥掉——python-dotenv 与
    pydantic-settings 都视 "abc" 与 abc 为同一值，保留引号会让幂等比较失真。
    """
    p = Path(path)
    try:
        text = p.read_text("utf-8", errors="ignore")
    except OSError:
        return {}
    out: dict[str, str] = {}
    for raw in text.splitlines():
        kv = _split_key(raw)
        if kv:
            out[kv[0]] = kv[1]
    return out


def atomic_write_dotenv(path, mutate) -> dict:
    """原子更新 .env：mutate 收到 dict，返回新 dict（返回 None 表示不改）。

    **保留原有行序与注释**：只改写值真的变了的键，其余行原样写回。整文件按 key
    重排会毁掉用户手写的分组与注释，而 .env 常被直接手工编辑。
    """
    with _write_lock:
        p = Path(path)
        try:
            text = p.read_text("utf-8", errors="ignore") if p.exists() else ""
        except OSError:
            text = ""
        before = read_dotenv(p) if text else {}
        after = mutate(dict(before))
        if after is None:
            after = before

        lines = text.splitlines()
        seen: set[str] = set()
        for i, raw in enumerate(lines):
            kv = _split_key(raw)
            if not kv:
                continue                            # 注释/空行原样保留
            key, val = kv
            if key in seen:
                continue                            # 重复键：原样保留（后者胜出语义）
            seen.add(key)
            if key in after and after[key] != val:  # 值真的变了才重写该行
                new = after[key]
                lines[i] = f"{key}={new}" if new != "" else f"{key}="
        for key, val in after.items():              # mutate 新增的键追加到末尾
            if key not in seen:
                lines.append(f"{key}={val}" if val != "" else f"{key}=")
        while lines and not lines[-1].strip():
            lines.pop()                              # 去掉尾部空行，避免追加键后多出空行
        body = "\n".join(lines)
        if body:
            body += "\n"
        write_atomic(p, body.encode("utf-8"))
    return after
