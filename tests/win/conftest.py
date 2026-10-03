"""tests/win：Windows（desktop）专属回归，其他平台整体跳过。

背景：共享内核重构后 Windows 测试从 windows/tests/ 并入 tests/win/，
而 CI 是 `pytest tests/` ——会连本目录一起收集。这些用例断言的是 desktop 行为
（项目根 data 目录、msvcrt 锁、taskkill 进程树、*_win.json 清单），
在 Linux（server）上必然失败，故按 edition 整体 skip。

放在 conftest 里用 collection hook 而不是逐个加 pytestmark，
是为了让**新增**用例自动继承，不必记得手动标注。
"""
import pytest

from core.edition import is_desktop


def pytest_collection_modifyitems(items, config):
    if is_desktop():
        return
    skip = pytest.mark.skip(reason="desktop（Windows）专属回归，非 desktop 平台跳过")
    for item in items:
        try:
            path = str(item.fspath)
        except AttributeError:              # pragma: no cover
            continue
        if "tests/win" in path or "tests\\win" in path:
            item.add_marker(skip)
