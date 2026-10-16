import argparse
import json

from .domain import contract
from features.integrity.strategies import canonical_hash


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["inspect-config"])
    parser.parse_args()
    value = contract()
    print(json.dumps({**value, "fingerprint": canonical_hash(value), "requests_sent": 0,
                      "credentials_read": False, "database_opened": False}, ensure_ascii=False))


if __name__ == "__main__":
    main()
