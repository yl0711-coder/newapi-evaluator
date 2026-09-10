"""Run the complete suite; artifacts and temporary files stay outside source trees."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True
os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
DATA = Path('/Users/lmurder/Desktop/api中转站/中转站极限测试数据')
temp = DATA / 'tmp'
temp.mkdir(parents=True, exist_ok=True)
os.environ['TMPDIR'] = str(temp)
tempfile.tempdir = str(temp)

if __name__ == '__main__':
    suite = unittest.defaultTestLoader.discover(str(ROOT / 'tests'), top_level_dir=str(ROOT))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())
