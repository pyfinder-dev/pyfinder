"""Retain the exact inputs and native evidence before mutable paths are reused."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile


def identity(contents):
    return {"sha256": hashlib.sha256(contents).hexdigest(), "size_bytes": len(contents)}


def read_regular(path):
    """Refuse redirected artifacts rather than copying unrelated operator files."""
    path = Path(path)
    if path.resolve(strict=True) != path or not path.is_file():
        raise ValueError("Evidence must be an ordinary file without symlink components")
    return path.read_bytes()


class EvidenceStore:
    """Immutable directories keyed by private attempt tokens, never public IDs.

    A complete bundle is published by rename. An interrupted temporary copy is
    harmless; an existing published bundle is validated and never overwritten.
    """

    def __init__(self, root, service_root):
        self.root = Path(root)
        self.service_root = Path(service_root)
        if not self.root.is_absolute() or not self.service_root.is_absolute():
            raise ValueError("Evidence roots must be absolute")
        # Resolve explicit roots once, including macOS /var -> /private/var.
        # Descendant artifacts still reject symlinks in read_regular(); accepting
        # the platform's root alias must not permit redirected evidence files.
        self.root = self.root.resolve()
        self.service_root = self.service_root.resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _destination(self, key):
        return self.root / hashlib.sha256(key.encode()).hexdigest()

    def load(self, key):
        destination = self._destination(key)
        if not destination.exists():
            return None
        manifest = json.loads(read_regular(destination / "evidence.json"))
        for name, item in manifest["files"].items():
            if Path(name).name != name:
                raise ValueError("Unsafe retained evidence name")
            if identity(read_regular(destination / name)) != item:
                raise ValueError("Retained evidence identity changed")
        return {"directory": str(destination), **manifest}

    def publish(self, key, files, metadata):
        existing = self.load(key)
        if existing is not None:
            return existing
        destination = self._destination(key)
        temporary = Path(tempfile.mkdtemp(prefix=".capture-", dir=self.root))
        try:
            identities = {}
            for name, contents in files.items():
                if Path(name).name != name or name in {".", "..", "evidence.json"}:
                    raise ValueError("Unsafe evidence filename")
                with (temporary / name).open("xb") as stream:
                    stream.write(contents)
                    stream.flush()
                    os.fsync(stream.fileno())
                identities[name] = identity(contents)
            manifest = {"files": identities, "metadata": metadata}
            with (temporary / "evidence.json").open("x", encoding="utf-8") as stream:
                json.dump(manifest, stream, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                temporary.rename(destination)
            except OSError:
                if not destination.exists():
                    raise
            directory_fd = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
        return self.load(key)

    def capture_finder(self, execution_id, files):
        """Called synchronously while the FinDer workspace lock is still held."""
        kind = "finder-summary" if set(files) == {"finder-summary.json"} else "finder"
        return self.publish(
            kind + ":" + execution_id,
            {name: value if isinstance(value, bytes) else read_regular(value)
             for name, value in files.items()},
            {"execution_id": execution_id, "kind": "finder-input" if kind == "finder" else "finder-summary"},
        )

    def capture_shakemap(self, record):
        """Validate exact sequence and retain available evidence before replacement.

        A native failure may legitimately lack shake.log or products. Record
        that absence; a file that exists but cannot be read is a capture error.
        The caller must then keep the same-ID replacement blocked.
        """
        sequence = record.get("internal_sequence")
        observation = record.get("observation")
        if not observation or observation["details"].get("status") not in {"SUCCESS", "FAILED"}:
            # Local interruption or uncertain submission is not native failure.
            # Keep the last known record without reading mutable native files.
            return self.publish(
                "incomplete:" + record["attempt_id"],
                {"observation.json": json.dumps(record, indent=2).encode()},
                {"attempt_id": record["attempt_id"], "internal_sequence": sequence,
                 "kind": "incomplete-observation", "unavailable": ["terminal native evidence"]},
            )
        key = "shakemap:" + record["attempt_id"] + ":" + str(sequence)
        existing = self.load(key)
        if existing is not None:
            return existing
        event_id = record["request"]["event_id"]
        if not event_id or Path(event_id).name != event_id or event_id in {".", ".."}:
            raise ValueError("Unsafe event identity")
        details = record["observation"]["details"]
        if details["internal_sequence"] != sequence or details["status"] not in {"SUCCESS", "FAILED"}:
            raise ValueError("Evidence requires the exact terminal observation")
        event_root = self.service_root / ".service/events" / event_id
        products_root = self.service_root / "products" / event_id
        if observation.get("scope") == "archives":
            # REST exposes host paths while this caller may see container paths.
            # Map only the contracted archive suffix under our explicit mount,
            # then verify its durable event/sequence before copying any bytes.
            archive = Path(details["shared_paths"]["archive"])
            if (not archive.is_absolute() or archive.parent.name != "archive"
                    or archive.parent.parent.name != ".service"
                    or not archive.name.startswith(event_id + "-")
                    or ".." in archive.parts):
                raise ValueError("Unsafe archive evidence path")
            local_archive = self.service_root / ".service/archive" / archive.name
            event_root = local_archive / "service"
            products_root = local_archive / "products"
        status_path = event_root / "status.json"

        def check_current():
            current = json.loads(read_regular(status_path))
            if current["internal_sequence"] != sequence or current["event_id"] != event_id:
                raise ValueError("Current service tree no longer belongs to this attempt")

        check_current()
        files = {"observation.json": json.dumps(record, indent=2).encode()}
        missing = []
        candidates = {
            "service.log": event_root / "logs/service.log",
            "shake.log": event_root / "logs/shake.log",
            "provenance.json": event_root / "provenance.json",
            "product-manifest.json": event_root / "product-manifest.json",
            "intensity.jpg": products_root / "current/products/intensity.jpg",
        }
        for name in ("event.xml", "event_dat.xml", "rupture.json"):
            candidates[name] = event_root / "request" / name
        for name, path in candidates.items():
            if not path.exists():
                missing.append(name)
                continue
            contents = read_regular(path)
            if name in {"provenance.json", "product-manifest.json"}:
                document = json.loads(contents)
                if document["event_id"] != event_id or document["internal_sequence"] != sequence:
                    raise ValueError("Native evidence identity differs from the accepted attempt")
            files[name] = contents
        check_current()
        return self.publish(key, files, {
            "attempt_id": record["attempt_id"], "event_id": event_id,
            "internal_sequence": sequence, "configuration": record["request"]["configuration"],
            "status": details["status"], "unavailable": missing,
        })
