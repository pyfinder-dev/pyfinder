# PyFinder's ShakeMap adapter

This adapter prepares native inputs and communicates with the separate ShakeMap
service. It is not yet connected to the manager or scheduled workflow. Durable
submission recording, restart reconciliation, product copying, and notifications
remain later integration work. Existing application execution remains inactive
at that downstream boundary.

## Preparing inputs

`pyfinder.utils.shakemap.ShakeMapExporter` accepts an already-selected
`FinderSolution`, an explicit caller calculation ID, and the physical earthquake
origin as a timezone-aware `datetime`:

```python
from pyfinder.utils.shakemap import ShakeMapExporter

files = ShakeMapExporter(
    solution,
    event_id=calculation_id,
    origin_time=physical_origin_time,
).export_all()
```

The result maps native basenames to bytes: `event.xml`, `event_dat.xml`, and
`rupture.json` when a rupture is supplied. Export does not write files, create
profiles, fetch configuration, run a command, or contact the service.

The caller chooses the raw or processed solution before export. The exporter
preserves that choice and all supplied channels, including artificial ones.
It uses FinDer's location, magnitude and depth, but requires physical origin time
separately because FinDer's internal timestamp has a different meaning. It does
not change the original solution or use the internal timestamp as a fallback.

PGA input is positive linear cm/s² and XML output explicitly uses percent g.
Component names are preserved. Native ShakeMap interprets E/N/Z suffixes,
unknown numeric directions, and names such as UNK according to its own rules;
export does not force unknown or vertical observations to horizontal. Parsing
success does not establish that every observation will participate in modeling.

ShakeMap's XML reader ignores location codes when identifying components. If
two selected observations would share its network/station/component key, export
fails with their identities rather than silently dropping one. Missing/nonfinite
required values also fail export instead of silently omitting observations.

An independent audit found another limitation of the current mapping: different
components at the same native station can carry different coordinates, and the
native reader then assigns the final station coordinates to every component.
This remains an open implementation defect; the readability revision does not
claim to fix it or add another calculation-rejection rule.

A native-parser experiment verified that using the existing full
network/station/location identity as the native station code preserves distinct
locations and their coordinates. That candidate mapping has not been activated.
Before applying it, check configured station exclusions, which use exact native
station identifiers. Contradictory coordinates within the same complete sensor
identity also require separate investigation. Giving every component a unique
station ID is not an equivalent shortcut: it changes aggregation and can change
native orientation inference.

Rupture coordinates are serialized as longitude/latitude/depth, with closure
added to a copy of the supplied ordered points when needed. The exporter does
not repair geometry, invent depth, or replace a supplied rupture with a point.
ShakeMap's native parser decides whether that geometry is supported.

## Service client

`pyfinder.services.shakemap_client.ShakeMapClient` uses the service's existing
unversioned API. Construct it with an HTTP(S) base URL and a positive finite
request timeout. It uses the Python standard library; tests can inject a
transport with the documented callable signature.

The client exposes operational health, effective configuration, configuration
names, event/queue views, submission, one status observation, current-product
information, and log-path references. Configuration names do not imply scientific
usability. The caller selects the configuration explicitly; the default is
`global`, and the client does not choose another configuration after failure.

`submit(calculation_id, files, configuration="global", overwrite=True)` returns
an `AcceptedJob` and the complete acknowledgement. Retain the event ID and
`internal_sequence` before proceeding with a production workflow. `poll(job)`
selects that exact sequence in current jobs or retained archives; it does not
wait in a loop. `current_products(job)` rejects a different current sequence.
Archive scope and unavailable retained records stay visible to the caller.

The client never retries POST. Transport interruption, an unusable
acknowledgement, or a potentially post-acceptance server failure raises
`ShakeMapSubmissionUncertain`. Treat that as unknown acceptance, not permission
to resubmit. Explicit HTTP rejection, read transport failure, malformed protocol
responses, and unavailable job records have distinct exceptions. Exception
bodies may contain service diagnostics and should not be logged indiscriminately.

Product and log operations return service metadata and shared-path references,
not file bytes. They do not reserve those paths against same-ID replacement.
Race-safe collection and host/container path translation belong to the later
workflow integration. Also, `overwrite` governs the previous calculation, not
cleanup of the caller's retained input directory. An empty files mapping asks
the service to snapshot existing inputs; it does not mean an empty calculation.

## Verification

Run host checks from the repository using the existing environment:

```sh
source /Users/savas/my-codes/eew/pyfinder-dev/.venv/bin/activate
python -m unittest discover -s tests/unit -v
```

The separate native parser check requires the existing running canonical
`shakemap-docker` container matching `shakemap-docker:latest`:

```sh
PYFINDER_RUN_SHAKEMAP_NATIVE=1 python -m unittest tests.integration.test_shakemap_native_inputs -v
```

It sends generated fixture bytes through stdin, parses them in disposable
container `/tmp` storage, and leaves the mounted runtime untouched. It neither
starts/stops containers nor submits a model calculation. These checks establish
adapter and parser behavior, not continuous-operation or deployment readiness.

## Retained legacy code

The former exporter, local runner, profile mutation, and product ZIP collection
are preserved unchanged in `legacy/shakemap.py`. See `legacy/README.md`. They
are historical reference, excluded from installed packages and Docker builds,
and are not used as a fallback. Related old helper modules remain present.
