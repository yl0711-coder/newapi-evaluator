"""Print a verification plan without executing tests or reading runtime data."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.test_manifest import admission, diagnosis, image_quality, manifest_dict


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", choices=("admission", "diagnosis", "image-quality"))
    parser.add_argument("--sha", help="Include the detached-candidate legacy suite")
    args = parser.parse_args()
    output = Path("/EXTERNAL_TEST_EVIDENCE")
    if args.plan == "admission":
        suites = admission(sys.executable, output, sha=args.sha)
    elif args.plan == "diagnosis":
        suites = diagnosis(sys.executable, output, sha=args.sha)
    else:
        suites = image_quality(sys.executable, output)
    print(json.dumps(manifest_dict(suites, plan=args.plan), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
