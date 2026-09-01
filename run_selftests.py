"""按顺序跑全部自测，每个起服务的套件都先清库、先杀残留进程。

用法：
    python run_selftests.py          # 全跑
    python run_selftests.py rules    # 只跑名字含 rules 的

起服务的套件都从空状态开始断言，所以必须一个一个跑、每次跑前清库。
这个脚本存在的唯一原因就是防止手工连跑两次 —— 第二次会因为库里有上次的数据而误报失败。
"""
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent

# 自测专用库。**绝不能是 platform.db** —— 那是正式运行的库，
# 里面有真实渠道、历史任务和能力标杆。自测每次跑前清库，
# 共用一个文件的话，跑一次自测就把你真实跑出来的标杆全删了。
DB_NAME = "selftest.db"
assert DB_NAME != "platform.db", "自测库不能是正式库"
METRIC_DB_NAME = "metrics-selftest.db"
DB_FILES = [
    f"data/{DB_NAME}", f"data/{DB_NAME}-wal", f"data/{DB_NAME}-shm",
    f"data/{METRIC_DB_NAME}", f"data/{METRIC_DB_NAME}-wal", f"data/{METRIC_DB_NAME}-shm",
]
TEST_BACKUP_DIR = (BASE / "data/selftest-backups").resolve()
TEST_BACKUP_KEY = (BASE / "data/selftest-backup.key").resolve()

# (脚本, 是否需要起服务)
SUITES = [
    ("selftest_grading.py", False),
    ("selftest_evaluation_packs.py", False),
    ("selftest_hardbank.py", False),
    ("selftest_placement.py", False),
    ("selftest_admission_pack.py", False),
    ("selftest_rules.py", False),
    ("selftest_transport.py", False),
    ("selftest_load.py", False),
    ("selftest_repository_security.py", False),
    ("selftest_security.py", True),
    ("selftest_insights.py", True),
    ("selftest_specialty.py", True),
    ("selftest_lifecycle.py", True),
    ("selftest_incidents.py", True),
    ("selftest_local_runner.py", True),
    ("selftest_reliability.py", True),
    ("selftest_backup.py", True),
    ("selftest_external_sync.py", True),
    ("selftest_scheduled.py", True),
    ("selftest_workspace_v2.py", True),
    ("selftest_paired_admission.py", True),
    ("selftest_api.py", True),
    ("selftest_capability.py", True),
]


def clean_db(retries: int = 3) -> bool:
    """删库并确认真的删掉了。删不掉就不要往下跑，否则断言全是假的。

    故意不去杀进程：按镜像名杀 python.exe 会连带杀掉你自己开的其它 Python，
    代价太大。残留进程一般几秒内自己退出，所以这里只重试等待。
    """
    for attempt in range(retries):
        stuck = False
        for f in DB_FILES:
            try:
                (BASE / f).unlink(missing_ok=True)
            except OSError:
                stuck = True
        try:
            TEST_BACKUP_KEY.unlink(missing_ok=True)
            if TEST_BACKUP_DIR.exists():
                TEST_BACKUP_DIR.relative_to(BASE.resolve())
                shutil.rmtree(TEST_BACKUP_DIR)
        except OSError:
            stuck = True
        if not stuck and not (BASE / DB_FILES[0]).exists():
            return True
        if attempt < retries - 1:
            time.sleep(2)
    return False


def run_one(script: str, needs_server: bool) -> bool:
    if needs_server:
        if not clean_db():
            print(f"!! {script}: 库文件被占用删不掉，跳过。"
                  f"等几秒重试，或手工删掉 data/platform.db*")
            return False
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1",
           "TEST_DB_NAME": DB_NAME,
           "TEST_METRIC_DB_NAME": METRIC_DB_NAME,
           "TEST_BACKUP_DIR": str(TEST_BACKUP_DIR),
           "TEST_BACKUP_KEY_PATH": str(TEST_BACKUP_KEY),
           "TEST_BOOTSTRAP_USERNAME": "selftest-admin",
           "TEST_BOOTSTRAP_PASSWORD": "selftest-password-2026",
           "TEST_COOKIE_SECURE": "0",
           "TEST_PAIRED_SELFTEST_MODE": "1",
           "TEST_EGRESS_ALLOWLIST": "127.0.0.1,localhost,example.com"}
    r = subprocess.run([sys.executable, script], cwd=BASE, env=env,
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    out = r.stdout or ""
    ok = "失败项：无" in out
    n_ok = out.count("  OK   ")
    n_bad = out.count("  FAIL ")
    print(f"{'PASS' if ok else 'FAIL'}  {script:26s} {n_ok:3d} 项通过"
          + (f"，{n_bad} 项失败" if n_bad else ""))
    if not ok:
        for line in out.splitlines():
            if line.startswith("  FAIL") or "失败项：" in line:
                print("      " + line.strip())
    return ok


def main() -> int:
    want = sys.argv[1] if len(sys.argv) > 1 else ""
    suites = [s for s in SUITES if not want or want in s[0]]
    if not suites:
        print(f"没有匹配 {want!r} 的套件")
        return 1
    results = [run_one(s, srv) for s, srv in suites]
    clean_db()          # 跑完清干净，别把测试数据留给用户
    print("-" * 52)
    print(f"{sum(results)}/{len(results)} 套通过")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
