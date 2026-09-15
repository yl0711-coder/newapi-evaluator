"""Build and exercise only this task's temporary image and containers."""
import argparse
import base64
import json
from pathlib import Path
import signal
import subprocess
import time
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]


def command(args, timeout=60, **options):
    return subprocess.run(args, check=True, text=True, capture_output=True, timeout=timeout, **options).stdout.strip()


class InspectionInterrupted(BaseException):
    pass


class DockerUnavailable(Exception):
    pass


def interrupted(_signum, _frame):
    raise InspectionInterrupted()


def remove_owned(args, identity):
    try:
        command(args, timeout=1.5)
    except subprocess.CalledProcessError as exc:
        # An interrupted creation may not have reached the Docker daemon.
        if not any(message + identity in exc.stderr for message in ("No such image: ", "No such container: ")):
            raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if output == ROOT or ROOT in output.parents:
        parser.error("构建数据必须位于仓库外")
    output.mkdir(parents=True, exist_ok=False)
    tag = "image-quality-check:" + uuid.uuid4().hex
    active = None
    checks = []
    failures = []
    incomplete = []
    docker_ready = False
    resources = {"image": tag, "containers": []}
    (output / "resources.json").write_text(json.dumps(resources))
    previous = {number: signal.signal(number, interrupted) for number in (signal.SIGTERM, signal.SIGINT)}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        try:
            command(["docker", "info", "--format", "{{.ServerVersion}}"])
        except (OSError, subprocess.SubprocessError):
            raise DockerUnavailable() from None
        docker_ready = True
        with (output / "build.log").open("w") as log:
            subprocess.run(["docker", "build", "--tag", tag, "."], cwd=ROOT,
                           stdout=log, stderr=subprocess.STDOUT, check=True, timeout=720)
        for mode in ("all", "image-quality"):
            data = output / mode
            data.mkdir(mode=0o777)
            data.chmod(0o777)
            active = "image-quality-" + uuid.uuid4().hex
            resources["containers"].append(active)
            (output / "resources.json").write_text(json.dumps(resources))
            command(["docker", "run", "--detach", "--name", active, "--read-only",
                     "--tmpfs", "/tmp:rw,nosuid,noexec,size=64m",
                     "--publish", "127.0.0.1::8090",
                     "--mount", "type=bind,source=" + str(data) + ",target=/data",
                     "--env", "PLATFORM_DATA_DIR=/data",
                     "--env", "PLATFORM_USERNAME=synthetic",
                     "--env", "PLATFORM_PASSWORD=synthetic-container-password",
                     tag, "python", "run.py", "--app", mode, "--host", "0.0.0.0", "--port", "8090"])
            port = command(["docker", "port", active, "8090/tcp"]).split(":")[-1]
            auth = base64.b64encode(b"synthetic:synthetic-container-password").decode()
            def get(path):
                request = urllib.request.Request("http://127.0.0.1:" + port + path,
                                                  headers={"Authorization": "Basic " + auth})
                with opener.open(request, timeout=2) as response:
                    return response.read()
            deadline = time.monotonic() + 40
            while True:
                try:
                    assert json.loads(get("/api/health"))["status"] == "ok"
                    break
                except (OSError, AssertionError):
                    if time.monotonic() >= deadline:
                        raise RuntimeError("container_health_timeout") from None
                    time.sleep(0.25)
            features = json.loads(get("/api/platform"))["features"]
            assert any(item["id"] == "image-quality" for item in features)
            assert b"image-form" in get("/image-quality/")
            assert len(get("/image-quality/assets/app.js")) > 100
            inspected = json.loads(command(["docker", "exec", active, "python", "-m",
                                             "features.image_quality", "inspect-config", "--config",
                                             "tests/fixtures/image_quality/config.json"]))
            assert inspected["network_requested"] is False
            checks.append({"mode": mode, "status": "passed"})
            command(["docker", "rm", "--force", active])
            active = None
    except InspectionInterrupted:
        incomplete.append("interrupted")
    except (DockerUnavailable, OSError, subprocess.TimeoutExpired) as exc:
        incomplete.append(type(exc).__name__)
    except (subprocess.SubprocessError, AssertionError, RuntimeError, ValueError) as exc:
        failures.append(type(exc).__name__)
    finally:
        # The outer runner gives this owned process group 5 seconds before SIGKILL.
        # Bound the two cleanup commands to 3 seconds total and ignore repeat signals.
        for number in previous:
            signal.signal(number, signal.SIG_IGN)
        if active:
            try:
                remove_owned(["docker", "rm", "--force", active], active)
            except (OSError, subprocess.SubprocessError):
                failures.append("container_cleanup_failed")
        if docker_ready:
            try:
                # Also attempt removal if build was interrupted after the daemon tagged it.
                remove_owned(["docker", "image", "rm", tag], tag)
            except (OSError, subprocess.SubprocessError):
                failures.append("image_cleanup_failed")
    status = "failed" if failures else "passed" if len(checks) == 2 and not incomplete else "incomplete"
    report = {"status": status, "checks": checks, "failures": failures,
              "failure_count": len(failures), "incomplete": incomplete, "resources": resources}
    (output / "checks.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report), flush=True)
    for number, handler in previous.items():
        signal.signal(number, handler)
    return 0 if status == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
