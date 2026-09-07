from contextlib import contextmanager
import os
from pathlib import Path


@contextmanager
def scheduler_lock(directory: Path):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / "scheduler.lock"
    file = path.open("a+b")
    path.chmod(0o600)
    try:
        if os.name == "nt":
            import msvcrt
            if path.stat().st_size == 0:
                file.write(b"0")
                file.flush()
            file.seek(0)
            msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        file.close()
        raise RuntimeError("该数据目录已有定时测试实例运行，请使用已有工作台或另一数据目录") from exc
    try:
        yield
    finally:
        file.close()
