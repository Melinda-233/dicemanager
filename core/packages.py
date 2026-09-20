"""程序包缓存：部署优先解压本地包，避免国内直连 GitHub 拉包超时

存储：<DM_STATE_DIR>/packages/<dice>.zip（+ .meta.json 记录来源/时间）。
来源两种：WebUI 上传；或首次在线下载后落在这里供后续部署复用。
路径函数均按调用时环境变量取值，保证测试可在 import 后覆盖 DM_STATE_DIR。
"""
import os
import time
import zipfile
from pathlib import Path

MAX_PKG_BYTES = 2 * 1024 * 1024 * 1024     # 2GB 上限，防误传超大文件撑爆磁盘


def pkg_dir() -> Path:
    d = Path(os.environ.get("DM_STATE_DIR", "/var/lib/dicemanager")) / "packages"
    d.mkdir(parents=True, exist_ok=True)
    return d


def archive_path(dice: str) -> Path:
    return pkg_dir() / f"{dice}.zip"


def find_archive(dice: str) -> Path | None:
    p = archive_path(dice)
    return p if p.exists() else None


def _meta_path(dice: str) -> Path:
    return pkg_dir() / f"{dice}.meta.json"


def mark_source(dice: str, source: str) -> None:
    mp = _meta_path(dice)
    try:
        mp.write_text(
            f'{{"source": "{source}", "updated_at": "{time.strftime("%Y-%m-%d %H:%M")}"}}',
            encoding="utf-8")
    except OSError:
        pass                                        # 元数据写失败不影响主流程


def save_archive(dice: str, data: bytes, source: str = "upload") -> dict:
    if not data:
        raise ValueError("压缩包内容为空")
    if len(data) > MAX_PKG_BYTES:
        raise ValueError(f"压缩包超过大小上限（{MAX_PKG_BYTES // 1048576} MB）")
    tmp = archive_path(dice).with_suffix(".zip.tmp")
    tmp.write_bytes(data)
    try:
        return commit_archive(dice, tmp, source)
    finally:
        tmp.unlink(missing_ok=True)


def commit_archive(dice: str, tmp: Path, source: str = "upload") -> dict:
    """对已落盘的临时包做完整性校验后原子改名——半截包不会顶掉旧的好包。"""
    target = archive_path(dice)
    if not zipfile.is_zipfile(tmp):
        raise ValueError("不是有效的 zip 压缩包")
    with zipfile.ZipFile(tmp) as zf:                # 顺带检验中央目录可读
        if zf.testzip() is not None:
            raise ValueError("zip 内有损坏条目")
    tmp.replace(target)
    mark_source(dice, source)
    return info_of(dice)


def info_of(dice: str) -> dict:
    p = archive_path(dice)
    if not p.exists():
        return {"dice": dice, "exists": False}
    meta = {}
    try:
        import json
        meta = json.loads(_meta_path(dice).read_text("utf-8"))
    except (OSError, ValueError):
        pass
    return {"dice": dice, "exists": True,
            "size_mb": round(p.stat().st_size / 1048576, 1),
            "source": meta.get("source", "download"),
            "updated_at": meta.get("updated_at",
                                   time.strftime("%Y-%m-%d %H:%M",
                                                 time.localtime(p.stat().st_mtime)))}


def list_archives() -> list[dict]:
    return [info_of(f[:-4]) for f in sorted(os.listdir(pkg_dir()))
            if f.endswith(".zip")]


def remove_archive(dice: str) -> bool:
    p = archive_path(dice)
    if not p.exists():
        return False
    p.unlink()
    _meta_path(dice).unlink(missing_ok=True)
    return True
