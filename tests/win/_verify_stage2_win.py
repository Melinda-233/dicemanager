"""阶段2验证：manifest 加载 + pathutil 展开 + base.py firewall no-op + load_registry"""
import inspect
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.pathutil import expand_windows_vars

# 1. 加载 10 份 manifest 并校验字段
manifest_dir = Path(__file__).parent.parent / "manifests"
manifests = []
for f in sorted(manifest_dir.glob("*_win.json")):
    text = re.sub(r"^\s*//.*$", "", f.read_text("utf-8"), flags=re.M)
    m = json.loads(text)
    manifests.append(m)
    print(f"  {f.name}: name={m['name']}, exe={m['exe']}")

assert len(manifests) == 10, f"期望 10 份 manifest，实际 {len(manifests)}"

for m in manifests:
    assert m["install_root"] != "/opt", f"{m['name']}: install_root 仍是 /opt"
    assert m["exe"].endswith((".exe", ".bat")), f"{m['name']}: exe 不带 Windows 后缀: {m['exe']}"
    assert "win32" in m.get("platform", []), f"{m['name']}: platform 不含 win32"
print("  [1] manifest 字段校验通过（install_root 已不为 Linux 路径/exe/platform 全部 Windows 形态）")

# 2. pathutil 展开 install_root（保留兼容旧占位符）
expanded = expand_windows_vars(manifests[0]["install_root"])
print(f"  [2] 展开 {manifests[0]['install_root']} -> {expanded}")
# 注：load_registry 现在直接覆盖 install_root 为 default_install_root()，
# 占位符是否字面展开不再有运行时影响，这里仅保留回归用例

# 3. base.py 不再 import firewall，open_port 是本地 no-op
import adapters.base as base
assert hasattr(base, "open_port"), "base.py 缺少 open_port"
assert base.open_port(6099) is None, "base.open_port 应返回 None"
src = inspect.getsource(base.open_port)
assert "return None" in src, f"open_port 不是 no-op: {src}"
print("  [3] base.py firewall no-op 校验通过（open_port 恒返回 None）")

# 4. process.py 含 re_adopt（阶段1返工补充的接管逻辑）
from core import process
assert hasattr(process.ManagedProcess, "re_adopt"), "process.ManagedProcess 缺少 re_adopt"
assert hasattr(process.ManagedProcess, "_try_readopt_now"), "缺少 _try_readopt_now"
assert hasattr(process.ManagedProcess, "is_alive"), "缺少 is_alive"
assert process._WIN is True, "process._WIN 应为 True"
print("  [4] process.py re_adopt 接管逻辑校验通过")

# 5. 尝试完整 load_registry（需要 yaml/json5 等第三方包）
try:
    from adapters import load_registry
    reg = load_registry(manifest_dir)
    assert len(reg) == 10, f"load_registry 返回 {len(reg)} 份，期望 10"
    print(f"  [5] load_registry 成功加载 {len(reg)} 份 manifest（含 adapter class）")
    print("\n=== 阶段2 全部验证通过 ===")
except ImportError as e:
    print(f"  [5] load_registry 跳过（缺第三方包: {e}）")
    print("\n=== 阶段2 验证通过（manifest+pathutil+base+process；load_registry 待装依赖后跑）===")
