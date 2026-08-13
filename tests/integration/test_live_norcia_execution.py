"""Live NORCIA acceptance through the supported canonical-container command.

This module is deliberately outside deterministic unit discovery. Importing it
has no live side effect. Invoke it separately with the standard integration
suite command only when provider access, one real FinDer execution, and retained
deployment output have been explicitly authorized.

The test never creates, starts, stops, restarts, removes, or replaces the
canonical container. It preserves every existing workspace entry and supports
repeat runs against retained FinDer output.
"""

from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import time
import unittest


REPOSITORY_ROOT = Path("/Users/savas/my-codes/eew/pyfinder-dev/pyfinder")
DEPLOYMENT_RUNTIME = Path(
    "/Users/savas/my-codes/eew/pyfinder-dev/pyfinder-deploy/runtime"
)
SERVICE_ROOT = DEPLOYMENT_RUNTIME / "pyfinder"
PLAYBACK_ROOT = SERVICE_ROOT / "playbacks"
PROCESS_LOG_ROOT = SERVICE_ROOT / "logs/playbacks"
EVENT_ID = "20161030_0000029"
AUGMENTED_EVENT_ID = EVENT_ID + "_t00000"
WORKSPACE = PLAYBACK_ROOT / AUGMENTED_EVENT_ID
CONTAINER_WORKSPACE = Path(
    "/home/sysop/runtime/pyfinder/playbacks"
) / AUGMENTED_EVENT_ID
CONTAINER_NAME = "pyfinder-docker"
IMAGE_NAME = "pyfinder:dev"
LAUNCHER = REPOSITORY_ROOT / "scripts/pyfinder"
EXPECTED_USER = "1000:1000"
EXPECTED_ENTRYPOINT = ["/usr/local/bin/pyfinder-entrypoint"]
EXPECTED_COMMAND = ["continuous"]
CONTAINER_RUNTIME = "/home/sysop/runtime"


def _sha256(path):
    """Hash one retained regular file without changing it."""
    digest = hashlib.sha256()
    with path.open("rb") as input_file:
        for block in iter(lambda: input_file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _entry_type(mode):
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISLNK(mode):
        return "symlink"
    return "other"


def _workspace_snapshot():
    """Record stable metadata for the exact workspace without creating it."""
    if not WORKSPACE.exists():
        return {
            "exists": False,
            "entries": {},
            "pyfinder_log": None,
            "temp_entries": [],
            "temp_data_entries": [],
        }

    entries = {}
    candidates = [WORKSPACE, *sorted(WORKSPACE.rglob("*"))]
    for candidate in candidates:
        entry_stat = candidate.lstat()
        relative_path = (
            "." if candidate == WORKSPACE else candidate.relative_to(WORKSPACE).as_posix()
        )
        entry = {
            "type": _entry_type(entry_stat.st_mode),
            "size": entry_stat.st_size,
            "mtime_ns": entry_stat.st_mtime_ns,
        }
        if stat.S_ISREG(entry_stat.st_mode):
            entry["sha256"] = _sha256(candidate)
        elif stat.S_ISLNK(entry_stat.st_mode):
            entry["target"] = os.readlink(candidate)
        entries[relative_path] = entry

    pyfinder_log = entries.get("pyfinder.log")
    return {
        "exists": True,
        "entries": entries,
        "pyfinder_log": pyfinder_log,
        "temp_entries": sorted(
            path for path in entries if path == "temp" or path.startswith("temp/")
        ),
        "temp_data_entries": sorted(
            path
            for path in entries
            if path == "temp_data" or path.startswith("temp_data/")
        ),
    }


def _process_log_directories():
    """List existing command-process log directories without creating them."""
    if not PROCESS_LOG_ROOT.is_dir():
        return []
    return sorted(
        path.name for path in PROCESS_LOG_ROOT.iterdir() if path.is_dir()
    )


def _docker_inspect(*arguments):
    """Return one read-only Docker inspection or fail with its diagnostic."""
    completed = subprocess.run(
        ["docker", *arguments],
        cwd=REPOSITORY_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(
            "Docker inspection failed for {0}: {1}".format(
                " ".join(arguments),
                completed.stderr.strip() or completed.stdout.strip(),
            )
        )
    try:
        inspected = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise AssertionError(
            "Docker inspection did not return JSON for {0}".format(
                " ".join(arguments)
            )
        ) from error
    if not isinstance(inspected, list) or len(inspected) != 1:
        raise AssertionError(
            "Docker inspection did not identify exactly one target for {0}".format(
                " ".join(arguments)
            )
        )
    return inspected[0]


def _canonical_snapshot():
    """Require the existing canonical container to match the supported tag."""
    try:
        container = _docker_inspect("container", "inspect", CONTAINER_NAME)
    except AssertionError as error:
        raise AssertionError(
            "canonical container pyfinder-docker is absent or cannot be inspected; "
            "this live test never creates or starts it"
        ) from error

    state = container["State"]
    if not state["Running"]:
        raise AssertionError(
            "canonical container pyfinder-docker is stopped; this live test never "
            "starts or restarts it"
        )

    image = _docker_inspect("image", "inspect", IMAGE_NAME)
    config = container["Config"]
    mounts = container["Mounts"]
    expected_mount = {
        "Type": "bind",
        "Source": str(DEPLOYMENT_RUNTIME),
        "Destination": CONTAINER_RUNTIME,
        "RW": True,
    }
    observed_mounts = [
        {
            "Type": mount["Type"],
            "Source": mount["Source"],
            "Destination": mount["Destination"],
            "RW": mount["RW"],
        }
        for mount in mounts
    ]

    required_values = (
        (config["Image"] == IMAGE_NAME, "canonical image reference differs"),
        (container["Image"] == image["Id"], "canonical image is stale"),
        (config["User"] == EXPECTED_USER, "canonical runtime user differs"),
        (
            config["Entrypoint"] == EXPECTED_ENTRYPOINT,
            "canonical entrypoint differs",
        ),
        (config["Cmd"] == EXPECTED_COMMAND, "canonical command differs"),
        (observed_mounts == [expected_mount], "canonical runtime mount differs"),
    )
    for condition, message in required_values:
        if not condition:
            raise AssertionError(message)

    return {
        "container_id": container["Id"],
        "image_id": container["Image"],
        "started_at": state["StartedAt"],
        "restart_count": container["RestartCount"],
        "running": state["Running"],
    }


def _changed_paths(before, after):
    """Return every workspace entry whose presence or metadata changed."""
    before_entries = before["entries"]
    after_entries = after["entries"]
    return sorted(
        relative_path
        for relative_path in set(before_entries) | set(after_entries)
        if before_entries.get(relative_path) != after_entries.get(relative_path)
    )


def _new_log_segment(before, after):
    """Read only bytes appended to the workspace event log by this command."""
    if not after["pyfinder_log"]:
        return b""
    before_size = before["pyfinder_log"]["size"] if before["pyfinder_log"] else 0
    after_size = after["pyfinder_log"]["size"]
    if after_size < before_size:
        raise AssertionError("pyfinder.log shrank during the live invocation")
    with (WORKSPACE / "pyfinder.log").open("rb") as log_file:
        log_file.seek(before_size)
        return log_file.read()


def _returned_identifier(appended_log):
    """Mirror FinDer stdout extraction and retain the final matching value."""
    identifier = None
    for line in appended_log.decode("utf-8", errors="replace").splitlines():
        if "Event_ID" not in line:
            continue

        # The transient event logger appends its source location after the raw
        # FinDer stdout message. Remove only that logging envelope before
        # applying the production extraction rule to the original message.
        # This does not validate, normalize, or compare the returned identifier.
        message = line.rsplit(" (", 1)[0]
        identifier = message.split("=")[-1].strip()
    return identifier


def _data_row_count(path):
    """Count non-comment live records without asserting their changing values."""
    return sum(
        1
        for line in path.read_text(encoding="ascii").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )


def _lock_is_released(path):
    """Probe the retained lock nonblockingly without altering its contents."""
    with path.open("rb") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        else:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            return True


class LiveNorciaExecutionTests(unittest.TestCase):
    """Exercise one authorized provider-to-real-FinDer on-demand invocation."""

    def test_supported_norcia_on_demand_execution(self):
        self.assertTrue(LAUNCHER.is_file(), "supported host launcher is absent")
        before_container = _canonical_snapshot()
        before_workspace = _workspace_snapshot()
        before_process_logs = _process_log_directories()

        start_utc = datetime.now(timezone.utc)
        start_monotonic = time.monotonic()
        completed = subprocess.run(
            [str(LAUNCHER), "on-demand", "--test"],
            cwd=REPOSITORY_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        elapsed_seconds = time.monotonic() - start_monotonic
        end_utc = datetime.now(timezone.utc)

        after_workspace = _workspace_snapshot()
        after_process_logs = _process_log_directories()
        after_container = _canonical_snapshot()
        changed_paths = _changed_paths(before_workspace, after_workspace)
        appended_log = _new_log_segment(before_workspace, after_workspace)
        returned_identifier = _returned_identifier(appended_log)
        new_process_logs = sorted(
            set(after_process_logs) - set(before_process_logs)
        )

        required_artifacts = (
            "finder_file.config",
            "data_0",
            "pyfinder_amplitudes_to_Finder.txt",
            "pyfinder.log",
            ".pyfinder.lock",
        )
        data_rows = None
        companion_rows = None
        lock_released = None
        returned_directory = None
        returned_activity = []
        if after_workspace["exists"]:
            if (WORKSPACE / "data_0").is_file():
                data_rows = _data_row_count(WORKSPACE / "data_0")
            companion_path = WORKSPACE / "pyfinder_amplitudes_to_Finder.txt"
            if companion_path.is_file():
                companion_rows = _data_row_count(companion_path)
            lock_path = WORKSPACE / ".pyfinder.lock"
            if lock_path.is_file():
                lock_released = _lock_is_released(lock_path)
        if returned_identifier is not None:
            returned_directory = WORKSPACE / "temp_data" / returned_identifier
            returned_prefix = "temp_data/{0}/".format(returned_identifier)
            returned_activity = [
                path for path in changed_paths if path.startswith(returned_prefix)
            ]

        result = {
            "after_entry_count": len(after_workspace["entries"]),
            "before_entry_count": len(before_workspace["entries"]),
            "before_workspace_exists": before_workspace["exists"],
            "changed_paths": changed_paths,
            "command": [str(LAUNCHER), "on-demand", "--test"],
            "container_workspace": str(CONTAINER_WORKSPACE),
            "data_0_rows": data_rows,
            "elapsed_seconds": elapsed_seconds,
            "end_utc": end_utc.isoformat(),
            "host_workspace": str(WORKSPACE),
            "lock_released": lock_released,
            "new_process_log_directories": new_process_logs,
            "pyfinder_log_after": after_workspace["pyfinder_log"],
            "pyfinder_log_before": before_workspace["pyfinder_log"],
            "returncode": completed.returncode,
            "returned_directory": str(returned_directory) if returned_directory else None,
            "returned_identifier": returned_identifier,
            "stderr": completed.stderr,
            "stdout": completed.stdout,
            "start_utc": start_utc.isoformat(),
            "amplitude_companion_rows": companion_rows,
        }
        print("PYFINDER_LIVE_NORCIA_RESULT=" + json.dumps(result, sort_keys=True))

        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        self.assertIsNotNone(
            returned_identifier,
            "current pyfinder.log append contains no FinDer Event_ID line",
        )
        self.assertTrue(after_workspace["exists"], "accepted NORCIA workspace is absent")
        for relative_path in required_artifacts:
            artifact = WORKSPACE / relative_path
            self.assertTrue(artifact.is_file(), "required artifact is absent: {0}".format(artifact))
        self.assertTrue((WORKSPACE / "temp").is_dir(), "real FinDer temp/ is absent")
        self.assertTrue(returned_directory.is_dir(), "returned-ID output directory is absent")
        self.assertTrue(
            returned_activity,
            "returned-ID directory has no current before/after activity",
        )
        self.assertTrue(changed_paths, "current invocation changed no workspace artifact")
        for relative_path in (
            "finder_file.config",
            "data_0",
            "pyfinder_amplitudes_to_Finder.txt",
        ):
            self.assertGreater(
                (WORKSPACE / relative_path).stat().st_size,
                0,
                "invocation-owned input is empty: {0}".format(relative_path),
            )
        self.assertIsNotNone(data_rows)
        self.assertIsNotNone(companion_rows)
        self.assertTrue(lock_released, "workspace lock remains held after command return")
        self.assertEqual(len(new_process_logs), 1, "one on-demand process log directory was not created")
        self.assertEqual(after_container["container_id"], before_container["container_id"])
        self.assertEqual(after_container["image_id"], before_container["image_id"])
        self.assertEqual(after_container["started_at"], before_container["started_at"])
        self.assertEqual(after_container["restart_count"], before_container["restart_count"])
        self.assertTrue(after_container["running"])


if __name__ == "__main__":
    unittest.main()
