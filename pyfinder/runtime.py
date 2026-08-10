"""Fixed runtime paths and resources for one PyFinder workflow process."""

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import sys
import tempfile


SERVICE_ROOT = Path("/home/sysop/runtime/pyfinder")
_WORKFLOWS = frozenset(("continuous", "playback", "on-demand"))


class RuntimeBootstrapError(RuntimeError):
    """Report an unusable fixed runtime before operational modules start."""


def _require_directory(path):
    """Require one operator-owned runtime directory without creating it."""
    if not path.exists():
        raise RuntimeBootstrapError(
            "required runtime directory does not exist: {0}".format(path)
        )
    if not path.is_dir():
        raise RuntimeBootstrapError(
            "required runtime path is not a directory: {0}".format(path)
        )


def _probe_directory_write(path):
    """Exercise real filesystem write access and remove the temporary probe."""
    try:
        with tempfile.NamedTemporaryFile(
            prefix=".pyfinder-write-probe-",
            dir=path,
        ):
            pass
    except OSError as error:
        raise RuntimeBootstrapError(
            "required runtime directory is not writable: {0}: {1}".format(
                path,
                error,
            )
        ) from error


def _validate_log_destination(path):
    """Prove one owned log destination is appendable and close it at once."""
    try:
        with path.open("a", encoding="utf-8"):
            pass
    except OSError as error:
        raise RuntimeBootstrapError(
            "required log destination is unusable: {0}: {1}".format(
                path,
                error,
            )
        ) from error


def _utc_trigger_time(clock):
    trigger_time = clock()
    if not isinstance(trigger_time, datetime):
        raise RuntimeBootstrapError("runtime clock must return a datetime")
    if trigger_time.tzinfo is None or trigger_time.utcoffset() is None:
        raise RuntimeBootstrapError("runtime clock must return an aware UTC time")
    return trigger_time.astimezone(timezone.utc)


def _trigger_component(trigger_time):
    """Render one captured UTC time as one portable directory component."""
    return trigger_time.strftime("%Y%m%dT%H%M%S.%fZ")


@dataclass(frozen=True)
class RuntimeContext:
    """Resolved paths and resource factories owned by one command process."""

    workflow: str
    service_root: Path
    trigger_time: datetime
    state_directory: Path
    logs_directory: Path
    runs_directory: Path
    playbacks_directory: Path
    process_log_directory: Path
    process_log_path: Path
    scheduler_log_path: Path
    paramws_log_path: Path
    listener_log_path: Path | None
    operational_database_path: Path | None
    work_root: Path

    def isolated_configuration(self, configuration):
        """Select this process's work root without mutating shared settings."""
        isolated = deepcopy(configuration)
        isolated["finder-executable"]["output-root-folder"] = str(
            self.work_root
        )
        return isolated

    @contextmanager
    def playback_database(self):
        """Own one playback-only SQLite location outside mounted state."""
        if self.workflow != "playback":
            raise RuntimeBootstrapError(
                "only playback processes may allocate a synthetic database"
            )

        temporary_directory = tempfile.TemporaryDirectory(
            prefix="pyfinder-playback-"
        )
        try:
            database_path = (
                Path(temporary_directory.name) / "scheduled_queries.sqlite3"
            )
            try:
                database_path.resolve().relative_to(
                    self.service_root.resolve()
                )
            except ValueError:
                pass
            else:
                raise RuntimeBootstrapError(
                    "playback database must remain outside the mounted "
                    "service root: {0}".format(database_path)
                )
            yield database_path
        finally:
            temporary_directory.cleanup()


def build_runtime_context(
    workflow,
    *,
    service_root=SERVICE_ROOT,
    clock=None,
):
    """Validate fixed runtime storage and resolve one process's exact paths."""
    if workflow not in _WORKFLOWS:
        raise RuntimeBootstrapError(
            "unsupported workflow for runtime bootstrap: {0!r}".format(
                workflow
            )
        )

    root = Path(service_root)
    if not root.is_absolute():
        raise RuntimeBootstrapError(
            "runtime service root must be absolute: {0}".format(root)
        )

    state_directory = root / "state"
    logs_directory = root / "logs"
    runs_directory = root / "runs"
    playbacks_directory = root / "playbacks"
    required_directories = (
        root,
        state_directory,
        logs_directory,
        runs_directory,
        playbacks_directory,
    )
    for directory in required_directories:
        _require_directory(directory)
    for directory in required_directories:
        _probe_directory_write(directory)

    if clock is None:
        clock = lambda: datetime.now(timezone.utc)
    trigger_time = _utc_trigger_time(clock)

    try:
        if workflow == "continuous":
            process_log_directory = logs_directory / "continuous"
            process_log_directory.mkdir(exist_ok=True)
            process_log_path = process_log_directory / "monitoring.log"
            listener_log_path = (
                process_log_directory / "seismiclistener.log"
            )
            operational_database_path = (
                state_directory / "scheduled_queries.sqlite3"
            )
            work_root = runs_directory
        else:
            # Directory creation reserves the timestamp across concurrent
            # command processes. A collision captures a newer clock value;
            # it never shares another process's log files or adds a second
            # identity component.
            for _attempt in range(1000):
                process_log_directory = (
                    logs_directory
                    / "playbacks"
                    / _trigger_component(trigger_time)
                )
                try:
                    process_log_directory.mkdir(
                        parents=True,
                        exist_ok=False,
                    )
                except FileExistsError:
                    trigger_time = _utc_trigger_time(clock)
                    continue
                break
            else:
                raise RuntimeBootstrapError(
                    "could not reserve a distinct command-trigger log "
                    "directory beneath {0}".format(
                        logs_directory / "playbacks"
                    )
                )
            process_log_path = process_log_directory / "playback.log"
            listener_log_path = None
            operational_database_path = None
            work_root = playbacks_directory
    except OSError as error:
        raise RuntimeBootstrapError(
            "could not create process log directory {0}: {1}".format(
                process_log_directory,
                error,
            )
        ) from error

    _probe_directory_write(process_log_directory)
    scheduler_log_path = process_log_directory / "followupscheduler.log"
    paramws_log_path = process_log_directory / "paramws.log"
    if workflow == "continuous":
        log_destinations = (
            process_log_path,
            listener_log_path,
            scheduler_log_path,
            paramws_log_path,
        )
    elif workflow == "playback":
        log_destinations = (
            process_log_path,
            scheduler_log_path,
            paramws_log_path,
        )
    else:
        log_destinations = (
            process_log_path,
            paramws_log_path,
        )
    for log_destination in log_destinations:
        _validate_log_destination(log_destination)

    return RuntimeContext(
        workflow=workflow,
        service_root=root,
        trigger_time=trigger_time,
        state_directory=state_directory,
        logs_directory=logs_directory,
        runs_directory=runs_directory,
        playbacks_directory=playbacks_directory,
        process_log_directory=process_log_directory,
        process_log_path=process_log_path,
        scheduler_log_path=scheduler_log_path,
        paramws_log_path=paramws_log_path,
        listener_log_path=listener_log_path,
        operational_database_path=operational_database_path,
        work_root=work_root,
    )


def bootstrap_process(workflow, *, service_root=SERVICE_ROOT, clock=None):
    """Own ParamWS logging before any provider-dependent module is imported."""
    imported_paramws = sorted(
        name
        for name in sys.modules
        if name == "paramws" or name.startswith("paramws.")
    )
    if imported_paramws:
        raise RuntimeBootstrapError(
            "ParamWS was imported before runtime bootstrap: {0}".format(
                ", ".join(imported_paramws)
            )
        )

    context = build_runtime_context(
        workflow,
        service_root=service_root,
        clock=clock,
    )
    os.environ["PARAMWS_LOG_FILE"] = str(context.paramws_log_path)
    return context
