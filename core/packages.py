"""程序包缓存：部署优先解压本地包，避免国内直连 GitHub 拉包超时

存储：<DM_STATE_DIR>/packages/<dice>.<ext>（+ .meta.json 记录来源/时间）。
支持的压缩格式：.zip / .tar.gz(.tgz) / .tar.xz / .tar.bz2 / .tar（按魔数识别，
不依赖扩展名）。来源两种：WebUI 上传；或首次在线下载后落在这里供后续部署复用。
路径函数均按调用时环境变量取值，保证测试可在 import 后覆盖 DM_STATE_DIR。
"""
import os
import time
import zipfile
from pathlib import Path

MAX_PKG_BYTES = 2 * 1024 * 1024 * 1024     # 2GB 上限，防误传超大文件撑爆磁盘

KNOWN_EXTS = (".tar.gz", ".tar.xz", ".tar.bz2", ".tgz", ".tar", ".zip")


def pkg_dir() -> Path:
    d = Path(os.environ.get("DM_STATE_DIR", "/var/lib/dicemanager")) / "packages"
    d.mkdir(parents=True, exist_ok=True)
    return d


def archive_path(dice: str, ext: str = ".zip") -> Path:
    """缓存包路径；ext 为真实压缩扩展名（如 .tar.gz）。"""
    return pkg_dir() / f"{dice}{ext}"


def find_archive(dice: str) -> Path | None:
    for ext in KNOWN_EXTS:
        p = archive_path(dice, ext)
        if p.exists():
            return p
    return None


def _meta_path(dice: str) -> Path:
    return pkg_dir() / f"{dice}.meta.json"


def mark_source(dice: str, source: str) -> None:
    mp = _meta_path(dice)
    try:
        ts = time.strftime("%Y-%m-%d %H:%M")
        mp.write_text('{"source": "%s", "updated_at": "%s"}' % (source, ts),
                      encoding="utf-8")
    except OSError:
        pass                                        # 元数据写失败不影响主流程


def _ext_for_magic(head: bytes) -> str | None:
    """按文件头魔数推断压缩扩展名；无法识别返回 None。"""
    if head[:4] == b"PK\x03\x04":
        return ".zip"
    if head[:2] == b"\x1f\x8b":                      # gzip → .tar.gz / .tgz
        return ".tar.gz"
    if head[:6] == b"\xfd7zXZ":                      # xz
        return ".tar.xz"
    if head[:3] == b"BZh":                           # bzip2
        return ".tar.bz2"
    if head[257:262] == b"ustar":                    # 纯 tar
        return ".tar"
    return None


def detect_kind(path: Path) -> str | None:
    return _ext_for_magic(path.read_bytes()[:263])


def _validate(path: Path, ext: str) -> None:
    """校验压缩包完整性；损坏则抛 ValueError（半截/伪造包不会被接受）。"""
    if ext == ".zip":
        if not zipfile.is_zipfile(path):
            raise ValueError("不是有效的 zip 压缩包")
        with zipfile.ZipFile(path) as zf:
            if zf.testzip() is not None:
                raise ValueError("zip 内有损坏条目")
    else:
        import tarfile
        try:
            with tarfile.open(path, "r:*") as tf:
                tf.getmembers()                       # 读取中央目录/索引，验证可读
        except tarfile.TarError as e:
            raise ValueError("不是有效的 tar 压缩包: %s" % e) from e


def _store(dice: str, tmp: Path, source: str) -> dict:
    """对已落盘的临时包做魔数识别 + 完整性校验，再原子改名到缓存路径。"""
    head = tmp.read_bytes()[:263]
    ext = _ext_for_magic(head)
    if ext is None:
        raise ValueError("不支持的压缩格式（仅接受 zip / tar.gz / tar.xz / tar.bz2 / tar）")
    _validate(tmp, ext)
    target = archive_path(dice, ext)
    tmp.replace(target)
    mark_source(dice, source)
    return info_of(dice)


def save_archive(dice: str, data: bytes, source: str = "upload") -> dict:
    if not data:
        raise ValueError("压缩包内容为空")
    if len(data) > MAX_PKG_BYTES:
        raise ValueError("压缩包超过大小上限（%d MB）" % (MAX_PKG_BYTES // 1048576))
    tmp = pkg_dir() / f"{dice}.up.tmp"
    tmp.write_bytes(data)
    try:
        return _store(dice, tmp, source)
    finally:
        tmp.unlink(missing_ok=True)


def commit_archive(dice: str, tmp: Path, source: str = "upload") -> dict:
    """流式上传端点落盘后调用：校验 + 原子改名。"""
    return _store(dice, tmp, source)


def info_of(dice: str) -> dict:
    p = find_archive(dice)
    if not p:
        return {"dice": dice, "exists": False}
    meta = {}
    try:
        import json
        meta = json.loads(_meta_path(dice).read_text("utf-8"))
    except (OSError, ValueError):
        pass
    default_ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(p.stat().st_mtime))
    return {"dice": dice, "exists": True,
            "size_mb": round(p.stat().st_size / 1048576, 1),
            "source": meta.get("source", "download"),
            "updated_at": meta.get("updated_at", default_ts)}


def list_archives() -> list[dict]:
    out = []
    for f in sorted(os.listdir(pkg_dir())):
        for ext in KNOWN_EXTS:
            if f.endswith(ext):
                out.append(info_of(f[: -len(ext)]))
                break
    return out


def remove_archive(dice: str) -> bool:
    found = False
    for ext in KNOWN_EXTS:
        p = archive_path(dice, ext)
        if p.exists():
            p.unlink()
            found = True
    _meta_path(dice).unlink(missing_ok=True)
    return found
