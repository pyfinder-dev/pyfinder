"""Opt-in host adapter checks against an existing, explicitly selected service.

This experiment creates and retains one new synthetic calculation and an archive.
It never starts the production listener, runs FinDer, or removes runtime evidence.
The installed PyFinder container and scientific accuracy remain separate checks.
"""

from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import time
import unittest
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import urlopen

from pyfinder.eventcontext import EventContext
from pyfinder.findermanager import FinDerManager
from pyfinder.services.shakemap_client import (
    AcceptedJob, ShakeMapClient, ShakeMapJobUnavailable,
)
from pyfinder.services.shakemap_inputs import PreparedShakeMapInputs
from pyfinder.services.shakemap_workflow import ShakeMapWorkflow
from tests.integration.test_shakemap_native_inputs import _solution


_ENVIRONMENT = (
    "PYFINDER_SHAKEMAP_TEST_URL",
    "PYFINDER_SHAKEMAP_TEST_INPUT_ROOT",
    "PYFINDER_SHAKEMAP_TEST_EVENT_ID",
    "PYFINDER_SHAKEMAP_TEST_EVIDENCE",
)


def _settings(environment):
    """Require explicit destinations before any HTTP or filesystem mutation."""
    missing = [name for name in _ENVIRONMENT if not environment.get(name)]
    if missing:
        raise ValueError("Required live-test settings: " + ", ".join(missing))

    url, root, event_id, evidence = (environment[name] for name in _ENVIRONMENT)
    ShakeMapClient.validate_submission(event_id, {})
    if not event_id.startswith("pyfinder-live-") or not event_id.endswith("_t00000"):
        raise ValueError("Use a new pyfinder-live- fixture ID ending in _t00000")

    root = Path(root)
    if not root.is_absolute() or root.resolve(strict=True) != root:
        raise ValueError("Input root must be an existing absolute resolved path")
    if root.name != "inputs" or root.parent.name != "data":
        raise ValueError("Input root must identify the canonical data/inputs directory")

    evidence = Path(evidence)
    # Evidence must be new and outside the operator runtime. The integration
    # harness never opens the operator's existing workflow database.
    if not evidence.is_absolute() or evidence.parent.resolve(strict=True) != evidence.parent:
        raise ValueError("Evidence parent must be an existing absolute resolved path")
    if evidence.exists() or evidence.is_symlink():
        raise ValueError("Evidence directory already exists; retain it and choose a new one")
    if evidence.is_relative_to(root.parent.parent):
        raise ValueError("Evidence must be outside the service runtime")

    return url, root, event_id, evidence


@unittest.skipUnless(
    os.environ.get("PYFINDER_RUN_SHAKEMAP_SERVICE") == "1",
    "set PYFINDER_RUN_SHAKEMAP_SERVICE=1 and all explicit live-test settings",
)
class ShakeMapServiceWorkflowTests(unittest.TestCase):
    """Verify finite-to-point replacement and archive retention for one new ID."""

    def _save(self, name, value):
        (self.evidence / name).write_text(
            json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8",
        )

    def _no_collision(self):
        """Stop before touching inputs if any current or historical owner exists."""
        for parent in ("data/inputs", "products", ".service/events"):
            path = self.service_root / parent / self.event_id
            self.assertFalse(path.exists() or path.is_symlink(), str(path))
        self.assertEqual(
            list((self.service_root / ".service/archive").glob(self.event_id + "-*")), [],
        )
        try:
            with urlopen(
                self.url + "/events/" + quote(self.event_id, safe=""), timeout=30,
            ) as response:
                self.fail(f"Fixture ID already visible: HTTP {response.status}")
        except HTTPError as error:
            self.assertEqual(error.code, 404, "Only confirmed absence permits a new fixture")

    def _owned_path(self, path):
        """Only read service-advertised paths inside the inventoried host mount."""
        path = Path(path)
        self.assertTrue(path.is_absolute())
        self.assertEqual(path.resolve(strict=True), path)
        self.assertTrue(path.is_relative_to(self.service_root), str(path))
        return path

    def _verify_success(self, label, record):
        """Check exact-sequence evidence before another submission replaces it."""
        details = record["observation"]["details"]
        self.assertEqual(details["status"], "SUCCESS", details)
        self.assertIs(details["job_completed"], True)
        self.assertIs(details["products_ready"], True)
        sequence = record["internal_sequence"]
        job = AcceptedJob(self.event_id, sequence)
        products = self.client.current_products(job)
        self._save(label + "-products.json", products)
        paths = details["shared_paths"]
        product_root = self._owned_path(paths["products"])
        manifest_path = self._owned_path(paths["product_manifest"])
        provenance_path = self._owned_path(paths["provenance"])
        manifest = json.loads(manifest_path.read_text())
        provenance = json.loads(provenance_path.read_text())
        self._save(label + "-manifest.json", manifest)
        self._save(label + "-provenance.json", provenance)

        for document in (manifest, provenance):
            self.assertEqual(document["event_id"], self.event_id)
            self.assertEqual(document["internal_sequence"], sequence)
        self.assertIs(manifest["partial"], False)
        self.assertEqual(manifest["inventory_failures"], [])
        required = manifest["required_products"]
        self.assertIs(required["passed"], True)
        self.assertTrue(required["checks"])
        self.assertTrue(all(check["passed"] for check in required["checks"]))
        inventory = {item["path"]: item for item in manifest["products"]}
        self.assertTrue(set(required["paths"]) <= inventory.keys())
        self.assertTrue({"shake_result.hdf", "grid.xml", "intensity.jpg"} <= inventory.keys())
        for relative, item in inventory.items():
            path = self._owned_path(product_root / relative)
            self.assertTrue(path.is_relative_to(product_root))
            self.assertTrue(path.is_file())
            content = path.read_bytes()
            self.assertEqual(len(content), item["size"], relative)
            self.assertEqual(sha256(content).hexdigest(), item["sha256"], relative)

        materialization = provenance["configuration"]["materialization"]
        self.assertEqual(materialization["selected_configuration"], "global")
        self.assertIs(materialization["materialized"], True)
        for name, relative in (
            ("vs30", "global/vs30/global_vs30.grd"),
            ("topography", "global/topo/topo_30sec.grd"),
        ):
            # Provenance records selection and manifest identity; its explicit
            # calculation_validation field is retained, not upgraded to a claim
            # that this test independently validated full grid coverage.
            asset = provenance["large_datasets"]["global"][name]
            self.assertEqual(Path(asset["path"]), self.service_root / "data" / relative)
            self.assertTrue(asset["manifest_identity"]["sha256"])
        for name in ("service_log", "shake_log"):
            content = self._owned_path(paths[name]).read_bytes()
            self.assertTrue(content, name)
            (self.evidence / f"{label}-{name}.log").write_bytes(content)
        return job

    def _run_calculation(self, label, solution, *, overwrite):
        """Submit once and observe that accepted sequence with a finite deadline."""
        event_id, files = self.manager.prepare_shakemap(solution)
        self.assertEqual(event_id, self.event_id)
        bundle = self.evidence / label
        bundle.mkdir()
        for name, content in files.items():
            (bundle / name).write_bytes(content)

        with self.inputs.submission(event_id, files):
            record = self.workflow.submit(
                label, event_id, files, configuration="global", overwrite=overwrite,
            )
        self._save(label + "-accepted.json", record)
        self.assertEqual(record["submission_state"], "ACCEPTED")
        print(f"{label}: accepted sequence {record['internal_sequence']}", flush=True)
        deadline = time.monotonic() + 600
        previous_status = None
        while time.monotonic() < deadline:
            record = self.workflow.poll(label)
            self._save(label + "-observed.json", record)
            status = record["observation"]["details"]["status"]
            if status != previous_status:
                print(f"{label}: {status}", flush=True)
                previous_status = status
            if status in {"SUCCESS", "FAILED"}:
                break
            time.sleep(2)
        else:
            self.fail("Native calculation exceeded ten minutes; retained without resubmission")

        job = self._verify_success(label, record)
        snapshot = self.service_root / ".service/events" / event_id / "request"
        self.assertEqual(set(path.name for path in snapshot.iterdir()), set(files))
        for name, content in files.items():
            self.assertEqual((snapshot / name).read_bytes(), content)
        print(f"{label}: validated SUCCESS sequence {job.internal_sequence}", flush=True)
        return job

    def test_same_id_replacement_and_archive(self):
        self.url, root, self.event_id, self.evidence = _settings(os.environ)
        self.url = self.url.rstrip("/")
        self.service_root = root.parent.parent
        self.client = ShakeMapClient(self.url, timeout=30)
        configuration = self.client.configuration()
        self.assertEqual(Path(configuration["shared_service_root"]), self.service_root)
        self.assertIs(self.client.health()["ready"], True)
        self.assertEqual(self.client.queue()["capacity"]["running"], 0)
        self.assertEqual(self.client.queue()["capacity"]["queued"], 0)
        self._no_collision()

        # All prior checks are read-only. This new evidence directory and the
        # supplied fixture ID are the only destinations this test owns.
        self.evidence.mkdir(mode=0o700)
        self._save("service-configuration.json", configuration)
        self.inputs = PreparedShakeMapInputs(root)
        self.workflow = ShakeMapWorkflow(self.evidence / "workflow.sqlite", self.client)
        self.addCleanup(self.workflow.close)
        self.manager = object.__new__(FinDerManager)
        self.manager.entry_kind = FinDerManager.ALERT_BACKED
        self.manager.metadata = {"current_delay": 0}
        self.manager.event_context = EventContext(
            self.event_id.removesuffix("_t00000"), 42.05, 13.0, 5.5, 6.0,
            datetime(2026, 9, 26, 12, 30, 15, 250000, tzinfo=timezone.utc).isoformat(),
        )

        finite = self._run_calculation("finite", _solution(), overwrite=True)
        point = _solution()
        point.rupture = None
        replacement = self._run_calculation("point-replacement", point, overwrite=True)
        self.assertGreater(replacement.internal_sequence, finite.internal_sequence)
        self.assertFalse((root / self.event_id / "rupture.json").exists())
        with self.assertRaises(ShakeMapJobUnavailable):
            self.client.poll(finite)
        self.assertEqual(
            list((self.service_root / ".service/archive").glob(self.event_id + "-*")), [],
        )

        final = self._run_calculation("point-archive", point, overwrite=False)
        self.assertGreater(final.internal_sequence, replacement.internal_sequence)
        archived = self.client.poll(replacement)
        self.assertEqual(archived.scope, "archives")
        self.assertIs(archived.products_ready, True)
        self._save("archived-predecessor.json", archived.details)
        archive = self._owned_path(archived.details["shared_paths"]["archive"])
        self.assertTrue((archive / "products/current/products").is_dir())
        archived_status = json.loads((archive / "service/status.json").read_text())
        self.assertEqual(archived_status["internal_sequence"], replacement.internal_sequence)
        self._save("summary.json", {
            "event_id": self.event_id,
            "sequences": [finite.internal_sequence, replacement.internal_sequence, final.internal_sequence],
            "observed_dispositions": ["new", "preceding_discarded", "preceding_archived"],
            "archive": str(archive),
            "input_root": str(root),
            "evidence_scope": "Host manager preparation and real service; no FinDer or deployed caller check",
        })
