"""端口分配表：默认端口起步 +1 重试；文件锁互斥；持久化到 ports.json"""
import json, socket
from pathlib import Path
from core.atomicio import atomic_write_json
from core.locks import port_allocation_lock

class PortAllocator:
    def __init__(self, path): self._path = Path(path)

    @staticmethod
    def _system_in_use(port: int) -> bool:
        """真实系统占用探测：能绑定 = 空闲；被监听（含其他服务）= 占用。"""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("0.0.0.0", int(port))); return False
            except OSError:
                return True

    def _next_free(self, tbl: dict, base: int) -> int:
        p = int(base)
        while str(p) in tbl or self._system_in_use(p): p += 1
        return p

    def allocate(self, program: str, default_port: int | None, owner: str) -> int | None:
        with port_allocation_lock():
            chosen = None
            def _m(tbl: dict) -> dict:
                nonlocal chosen
                if default_port is None: return tbl
                chosen = self._next_free(tbl, default_port)
                tbl[str(chosen)] = owner
                return tbl
            atomic_write_json(self._path, _m)
            return chosen                      # 去掉 _last 属性 hack（原实现可能返回上次的陈旧值）

    def allocate_many(self, program: str, ports: dict, owner: str) -> dict:
        """批量分配（同一互斥锁内），如 {"webui":3080,"ob11":3001,"milky":3010,"satori":5600}"""
        result: dict[str, int] = {}
        with port_allocation_lock():
            def _m(tbl: dict) -> dict:
                for role, base in ports.items():
                    if base is None: continue
                    p = self._next_free(tbl, base)
                    tbl[str(p)] = f"{owner}:{role}"
                    result[role] = p
                return tbl
            atomic_write_json(self._path, _m)
        return result

    def release(self, port: int) -> None:
        with port_allocation_lock():
            def _m(tbl: dict) -> dict:
                tbl.pop(str(port), None)
                return tbl        # 显式返回整表（原 lambda 写法会把整表覆写成 false，数据全毁）
            atomic_write_json(self._path, _m)

    def release_owner(self, owner: str) -> None:
        """删除实例时按 owner 前缀释放其全部端口。"""
        with port_allocation_lock():
            def _m(tbl: dict) -> dict:
                for k in [k for k, v in tbl.items() if str(v).startswith(owner)]:
                    del tbl[k]
                return tbl
            atomic_write_json(self._path, _m)
