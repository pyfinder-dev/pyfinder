"""Prepare caller-owned canonical inputs for a complete ShakeMap submission."""

from collections.abc import Mapping
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import stat


class PreparedShakeMapInputs:
    """Remove a stale optional rupture while serializing same-ID submissions.

    The configured root is the service's canonical input directory, shared with
    this caller. This helper requires exclusive ownership of its event inputs:
    the advisory lock coordinates PyFinder processes, not unrelated writers.
    It never touches calculation products or service-owned runtime records.
    """

    _REQUIRED = frozenset({"event.xml", "event_dat.xml"})
    _ALLOWED = _REQUIRED | {"rupture.json"}
    _LOCK_DIRECTORY = ".pyfinder-locks"

    def __init__(self, root):
        """Require an existing explicit root; never create deployment mounts."""
        self.root = Path(root)
        if not self.root.is_absolute():
            raise ValueError("ShakeMap canonical input root must be absolute")

        # A redirected mount/path is too ambiguous for destructive cleanup.
        # Operators supply the resolved real directory that they own.
        if self.root.resolve(strict=True) != self.root:
            raise ValueError("ShakeMap canonical input root must not use symlinks")
        if not self.root.is_dir():
            raise ValueError("ShakeMap canonical input root must be a directory")

    @contextmanager
    def submission(self, event_id, files):
        """Wait for the event lock, then hold it through cleanup and REST upload.

        Existing required files remain in place for the REST upload to replace.
        Only an omitted rupture.json is deleted. Unknown inputs are refused
        untouched, so a future input format cannot silently lose scientific data.
        """
        if (
            not isinstance(event_id, str)
            or not event_id
            or event_id in {".", "..", self._LOCK_DIRECTORY}
            or any(character in event_id for character in ("/", "\\", "\0", ":"))
        ):
            raise ValueError("ShakeMap event ID must be one safe path component")

        if not isinstance(files, Mapping):
            raise TypeError("ShakeMap input bundle must be a mapping")
        if not self._REQUIRED <= files.keys() or not files.keys() <= self._ALLOWED:
            raise ValueError("ShakeMap submission requires a complete known input bundle")
        if any(not isinstance(value, bytes) or not value for value in files.values()):
            raise ValueError("ShakeMap input files must contain nonempty bytes")

        # Directory descriptors keep cleanup attached to the opened directory,
        # while O_NOFOLLOW refuses symlink event directories and lock files.
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        root_fd = os.open(self.root, directory_flags)
        lock_dir_fd = lock_fd = event_fd = None
        try:
            self._mkdir(self._LOCK_DIRECTORY, root_fd)
            lock_dir_fd = os.open(
                self._LOCK_DIRECTORY, directory_flags, dir_fd=root_fd
            )
            lock_fd = os.open(
                event_id + ".lock",
                os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
                0o600,
                dir_fd=lock_dir_fd,
            )
            self._require_regular(os.fstat(lock_fd), "submission lock")
            # A deliberate later calculation waits for the earlier upload; lock
            # contention must not rerun FinDer or discard that new submission.
            # The caller holds this lock only through its finite HTTP request.
            fcntl.flock(lock_fd, fcntl.LOCK_EX)

            self._mkdir(event_id, root_fd)
            event_fd = os.open(event_id, directory_flags, dir_fd=root_fd)
            # The service locks this same directory inode while publishing
            # uploads and snapshotting inputs. Wait for that read/write window
            # to finish before removing a stale optional rupture.
            fcntl.flock(event_fd, fcntl.LOCK_EX)
            try:
                names = os.listdir(event_fd)

                # Validate every existing entry before removing anything. Required
                # files and optional rupture must be ordinary, unshared files.
                for name in names:
                    if name not in self._ALLOWED:
                        raise ValueError(f"Unrecognized ShakeMap input left untouched: {name}")
                    self._require_regular(
                        os.stat(name, dir_fd=event_fd, follow_symlinks=False), name
                    )

                if "rupture.json" in names and "rupture.json" not in files:
                    os.unlink("rupture.json", dir_fd=event_fd)
            finally:
                # Release the service's directory lock before POST: its request
                # handler must acquire it. The outer PyFinder lock still guards
                # this caller's entire cleanup-and-upload operation.
                fcntl.flock(event_fd, fcntl.LOCK_UN)

            yield
        finally:
            # Closing the lock releases it on success and on upload failure.
            # Keep the lock inode for subsequent callers; unlinking it would
            # allow overlapping holders to lock different files for one ID.
            for descriptor in (event_fd, lock_fd, lock_dir_fd, root_fd):
                if descriptor is not None:
                    os.close(descriptor)

    @staticmethod
    def _mkdir(name, parent_fd):
        """Create a caller-owned directory without replacing existing entries."""
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent_fd)
        except FileExistsError:
            pass

    @staticmethod
    def _require_regular(file_stat, label):
        """Refuse links and special files before operating on owned inputs."""
        if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_nlink != 1:
            raise ValueError(f"ShakeMap {label} must be an unshared regular file")
