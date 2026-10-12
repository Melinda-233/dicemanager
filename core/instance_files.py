"""实例目录文件管理：list / read / write / mkdir / rename / delete。

硬约束（功能 2「实例文件管理」的权限底线）：

- 所有操作都**限制在实例自身目录内**。用 ``../`` 或绝对路径试图逃出实例目录的请求
  一律拒绝（realpath 前缀 containment）。这与「权限仅限于当前用户可操作的实例目录」
  是两个独立闸：归属校验由调用方 ``api.rest._own`` 完成，本模块只负责文件系统安全。
- 写有上限（单文件 16MB），避免有人往实例目录塞巨大文件拖垮磁盘。
- 读有上限（单文件 4MB 直接预览，更大走下载），避免把二进制/大文件整页读进内存。

本模块是适配器无关的纯文件系统逻辑，便于单测（不依赖网络、不依赖真实实例）。
"""
import base64
import shutil
from pathlib import Path

MAX_READ = 4 * 1024 * 1024       # 单文件预览上限 4MB
MAX_WRITE = 16 * 1024 * 1024      # 单文件写入上限 16MB


def _resolve(inst_dir: str, rel: str) -> Path:
    """把相对路径解析为实例目录内的绝对路径；越界抛 ``PermissionError``。

    ``rel`` 为空表示实例根。``target.relative_to(base)`` 在越界时抛 ``ValueError``，
    同时天然允许 ``target == base``（根目录本身）。
    """
    base = Path(inst_dir).resolve()
    target = (base / (rel or "")).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        raise PermissionError(f"路径越界: {rel!r}") from None
    return target


def download_file(inst_dir: str, rel: str) -> Path:
    """返回待下载文件的绝对路径（已做 containment）。大小上限 50MB。"""
    if not rel:
        raise FileNotFoundError("需要 path")
    p = _resolve(inst_dir, rel)
    base = Path(inst_dir).resolve()
    if p == base or not p.is_file():
        raise FileNotFoundError(f"不是文件: {rel!r}")
    if p.stat().st_size > 50 * 1024 * 1024:
        raise ValueError("文件过大（>50MB）暂不支持下载")
    return p


def list_files(inst_dir: str, rel: str = "") -> dict:
    """列出目录内容（按 目录优先、名称不区分大小写 排序）。"""
    root = _resolve(inst_dir, rel)
    if not root.is_dir():
        raise FileNotFoundError(f"不是目录: {rel!r}")
    items = []
    for p in sorted(root.iterdir(), key=lambda x: (x.is_file(), x.name.lower())):
        try:
            st = p.stat()
        except OSError:
            continue
        items.append({
            "name": p.name,
            "type": "dir" if p.is_dir() else "file",
            "size": st.st_size if p.is_file() else None,
            "mtime": int(st.st_mtime),
        })
    return {"ok": True, "path": rel, "items": items}


def _is_text(data: bytes) -> bool:
    """含 NUL 即二进制；否则前 1MB 能 utf-8 解码即视为文本。"""
    if b"\x00" in data[:min(len(data), 8192)]:
        return False
    try:
        data[:min(len(data), 1 << 20)].decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def read_file(inst_dir: str, rel: str) -> dict:
    """读取文件。文本返回 ``content``（utf-8）；二进制返回 base64。"""
    p = _resolve(inst_dir, rel)
    if not p.is_file():
        raise FileNotFoundError(f"不是文件: {rel!r}")
    st = p.stat()
    if st.st_size > MAX_READ:
        raise ValueError(f"文件过大（>{MAX_READ} 字节），请用下载")
    data = p.read_bytes()
    if _is_text(data):
        return {"ok": True, "name": p.name, "is_text": True,
                "content": data.decode("utf-8"), "size": st.st_size,
                "mtime": int(st.st_mtime)}
    return {"ok": True, "name": p.name, "is_text": False,
            "content": base64.b64encode(data).decode("ascii"), "size": st.st_size,
            "mtime": int(st.st_mtime)}


def write_file(inst_dir: str, rel: str, content: str, encoding: str = "text") -> dict:
    """创建或覆盖文件。``encoding="base64"`` 用于二进制上传。

    原子写：先写临时文件再 ``replace``，避免半截文件。父目录不存在则自动创建。
    """
    p = _resolve(inst_dir, rel)
    if p.is_dir():
        raise IsADirectoryError(f"目标是目录: {rel!r}")
    if encoding == "base64":
        try:
            raw = base64.b64decode(content, validate=True)
        except Exception as e:                       # noqa: BLE001
            raise ValueError(f"base64 解码失败: {e}") from e
    else:
        raw = content.encode("utf-8")
    if len(raw) > MAX_WRITE:
        raise ValueError(f"写入内容过大（>{MAX_WRITE} 字节）")
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.parent / (".dm_write_tmp_" + (p.name or "x"))
    try:
        tmp.write_bytes(raw)
        tmp.replace(p)
    finally:
        tmp.unlink(missing_ok=True)
    return {"ok": True, "name": p.name, "size": len(raw),
            "mtime": int(p.stat().st_mtime)}


def mkdir(inst_dir: str, rel: str) -> dict:
    """在实例目录内新建文件夹。"""
    p = _resolve(inst_dir, rel)
    if p.exists():
        raise FileExistsError(f"已存在: {rel!r}")
    p.mkdir(parents=True, exist_ok=False)
    return {"ok": True, "name": p.name}


def rename_file(inst_dir: str, rel: str, new_name: str) -> dict:
    """重命名（限同一目录内）。``new_name`` 不含 ``/``，且目标仍须落在实例目录内。"""
    p = _resolve(inst_dir, rel)
    if not p.exists():
        raise FileNotFoundError(f"不存在: {rel!r}")
    new_name = (new_name or "").strip().strip("/")
    if not new_name or "/" in new_name or new_name in (".", ".."):
        raise ValueError(f"非法新名称: {new_name!r}")
    base = Path(inst_dir).resolve()
    dst = (p.parent / new_name).resolve()
    try:
        dst.relative_to(base)
    except ValueError:
        raise PermissionError(f"重命名目标越界: {new_name!r}") from None
    if dst.exists():
        raise FileExistsError(f"目标已存在: {new_name!r}")
    p.rename(dst)
    return {"ok": True, "name": dst.name}


def delete_file(inst_dir: str, rel: str) -> dict:
    """删除文件或（递归）目录。禁止删除实例根目录本身。"""
    p = _resolve(inst_dir, rel)
    base = Path(inst_dir).resolve()
    if p == base:
        raise PermissionError("不能删除实例根目录")
    if p.is_dir():
        shutil.rmtree(p)
    else:
        p.unlink()
    return {"ok": True, "name": p.name}
