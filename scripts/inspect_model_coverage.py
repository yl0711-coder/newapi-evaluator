"""Read model configuration without decryption, initialization, network or mutations."""
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from features.model_coverage.catalog import DEFAULT_MODELS


def inspect(directory=None):
    models = [{'model':m[0], 'protocol':m[3]} for m in DEFAULT_MODELS]
    channels = []
    if directory is not None:
        database = Path(directory).resolve() / 'channels.db'
        if not database.is_file():
            raise ValueError('channel database missing')
        with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as conn:
            names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for row in conn.execute('SELECT id,base_url,enabled,version FROM channels ORDER BY id'):
                host = urlsplit(row[1]).hostname or ''
                channels.append({'channel_alias':f'channel-{row[0]}',
                                 'masked_host':'host-' + hashlib.sha256(host.encode()).hexdigest()[:12],
                                 'enabled':bool(row[2]), 'version':row[3]})
            if 'model_catalog' in names:
                models = [{'model':r[0],'protocol':r[1]} for r in conn.execute('SELECT model,protocol FROM model_catalog ORDER BY id')]
    fingerprint = hashlib.sha256(json.dumps([channels,models],sort_keys=True).encode()).hexdigest()
    return {'channels':channels,'models':models,'fingerprint':fingerprint,'extracted_at':int(time.time()),
            'requests_sent':0,'secrets_read':False,'discovery_mode':'manual'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir',type=Path,help='Optional authorized existing channel data; opened read-only')
    args=parser.parse_args()
    try:
        result=inspect(args.data_dir)
    except (ValueError, sqlite3.Error):
        parser.error('cannot read the requested configuration')
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__': main()
