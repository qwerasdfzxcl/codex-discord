#!/usr/bin/env python3
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_metadata(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save_metadata(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def main() -> int:
    if len(sys.argv) < 5:
        print(
            "usage: background_operation_runner.py <metadata-path> <log-path> <cwd> <command> [args...]",
            file=sys.stderr,
        )
        return 2

    metadata_path = Path(sys.argv[1]).resolve()
    log_path = Path(sys.argv[2]).resolve()
    cwd = Path(sys.argv[3]).resolve()
    command = sys.argv[4:]

    payload = load_metadata(metadata_path)
    payload["status"] = "running"
    payload["started_at"] = now_utc_iso()
    payload["cwd"] = str(cwd)
    payload["args"] = command
    payload["log_path"] = str(log_path)
    save_metadata(metadata_path, payload)

    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with log_path.open("wb") as log_handle:
            completed = subprocess.run(
                command,
                cwd=str(cwd),
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                check=False,
            )
        payload["status"] = "succeeded" if completed.returncode == 0 else "failed"
        payload["returncode"] = completed.returncode
        payload["completed_at"] = now_utc_iso()
        save_metadata(metadata_path, payload)
        return completed.returncode
    except FileNotFoundError as exc:
        payload["status"] = "failed"
        payload["returncode"] = None
        payload["completed_at"] = now_utc_iso()
        payload["error_message"] = str(exc)
        save_metadata(metadata_path, payload)
        return 127
    except Exception as exc:
        payload["status"] = "failed"
        payload["returncode"] = None
        payload["completed_at"] = now_utc_iso()
        payload["error_message"] = str(exc)
        save_metadata(metadata_path, payload)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
