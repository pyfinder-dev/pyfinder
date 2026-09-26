# PyFinder's ShakeMap adapter

This adapter prepares native inputs and communicates with the separate ShakeMap
service. The manager and scheduler now have an explicit continuous-operation
handoff, durable submission queue, and exact-job monitoring. It is disabled by
default until the service endpoint and caller-owned canonical input mount are
configured. Product copying and notifications remain separate unfinished work.

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

The exporter preserves the existing XML representation: `netid` is the network,
`code` is the station, `loc` carries the supplied source location code, and each
selected channel produces a station element in input order. The optional XML
`loc` attribute is documented by USGS as free-form location description; it does
not establish a separate sensor identity. See the
[USGS v4.4.1 input documentation](https://code.usgs.gov/ghsc/esi/shakemap/-/blob/v4.4.1/doc/manual4_0/sg_input_formats.rst?ref_type=tags).

The installed native XML reader (esi-shakelib 1.2.1 in ShakeMap 4.4.9) groups
stations by network/station, without the separate `loc` attribute. For repeated
station/component/PGA entries, the later amplitude replaces the earlier one.
Different components survive under that shared station, but use the final
station element's coordinates. Preserving all XML observations therefore does
not guarantee that native parsing or modeling retains each one independently.

This library limitation is accepted for the current adapter package. Export
neither rejects those collisions nor renames, deduplicates, reorders, averages,
or relocates observations to work around native grouping. Location variants
accepted by the former exporter remain accepted. Missing/nonfinite required
values still fail serialization instead of silently omitting observations.
Changing native station identity remains deferred: it can affect component
grouping and configured station exclusions, so it is not an incidental fix.

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

## Durable submission and monitoring

`pyfinder.services.shakemap_workflow.ShakeMapWorkflow` wraps the client with an
additive `shakemap_submissions` table in an explicitly supplied SQLite database.
The path must name a persistent filesystem database; empty paths, in-memory
names, and SQLite URI names are rejected. The helper shares the scheduler database
but leaves scheduled lifecycle changes to the scheduler/EventTracker boundary. External
records survive scheduled-row cleanup. Opening this helper creates its table;
development and verification use temporary databases, not the operator database.

```python
from pyfinder.services.shakemap_client import ShakeMapClient
from pyfinder.services.shakemap_workflow import ShakeMapWorkflow

client = ShakeMapClient(service_url, timeout=30.0)
workflow = ShakeMapWorkflow(workflow_database_path, client)
try:
    record = workflow.submit(
        attempt_id, calculation_id, files,
        configuration=selected_configuration, overwrite=True,
    )
    if record["submission_state"] == "ACCEPTED":
        record = workflow.poll(attempt_id)  # One observation; no waiting loop.
finally:
    workflow.close()
```

The caller supplies a stable `attempt_id` identifying this invocation. It is
separate from the service's public calculation ID and internal sequence. This
helper does not invent an ID scheme or decide when another attempt is justified.
It records the endpoint, public ID, configuration, overwrite choice, and file
sizes/SHA-256 fingerprints before POST. Fingerprints identify the submitted
bytes; they neither retain those bytes nor prove which managed scientific data
was used. Direct callers remain responsible for input-file retention. The integrated
scheduler separately retains exact prepared bytes in its scheduled-attempt
association before dispatch. An empty
mapping retains the client's existing service-input snapshot semantics; its
fingerprint cannot describe those service-side inputs.

The submission states describe local evidence, separately from native job state:

- `SUBMITTING`: intent was committed. The request may be in flight, or the
  process may have stopped before sending or recording its response.
- `ACCEPTED`: a validated acknowledgement and exact internal sequence were saved.
- `UNCERTAIN`: the client could not establish whether the service accepted it.
- `REJECTED`: the client reported an explicit HTTP rejection.

An existing attempt never causes another POST. Repeating the same call returns
its stored record; changing its endpoint, selections, or input bytes raises a
local conflict. This also applies to rejected and unresolved attempts. A local
validation error before recording intent leaves no reserved attempt. Unexpected
interruption after intent leaves `SUBMITTING`, which requires the same caution
as `UNCERTAIN`; it is not proof of either acceptance or rejection.

If remote acceptance succeeds but saving it fails, `ShakeMapRecordingError`
retains `.job` and `.acknowledgement` for the caller. The original persistence
error remains its cause. The durable intent still prevents replay. No automatic
resolution or inference from the event's latest sequence is provided.

After reopening, `get(attempt_id)` and `unresolved()` expose retained records
without contacting the service. `poll(attempt_id)` reads only a durably accepted
sequence from its recorded endpoint, including retained archives. It saves
`observation` with `scope` and native `details`, and an `observed_at` timestamp.
Read failures propagate and set `last_error` to their exception class while
preserving previous evidence and its timestamp. A missing job, timeout, or bad
response never becomes a synthetic native `FAILED` result. Successful later
reads clear that monitoring error. Raw error response bodies are not copied
into this diagnostic field.

`unresolved()` includes intents, uncertain submissions, accepted jobs without a
terminal observation, and accepted jobs with a subsequent monitoring error.
Native `SUCCESS` and `FAILED` are terminal observations. An archived `SUCCESS`
may have `products_ready=False`; terminality does not prove accessible products,
artifact collection, notification delivery, or scheduled-chain success.

Use one monitoring owner and close the helper after its work finishes. Its
synchronous operations serialize within an instance; a database uniqueness
constraint also prevents two instances from sending the same attempt twice.
It supplies no distributed monitoring ownership or retry loop. Endpoint URL
pinning cannot detect an operator replacing the service runtime and reusing its
sequence numbers. Such service-runtime replacement recovery remains outside this package.
No submissions are activated by importing it.

## Manager and scheduler integration

`FinDerManager.run()` retains its `FinderSolution | None` result. The explicit
`prepare_shakemap(solution)` handoff uses that selected solution, authoritative
EventContext, and existing augmented calculation ID. It requires an explicit
timezone in the physical-origin text and does not round-trip through the old
host-dependent epoch helper. Continuous EMSC timestamps include UTC; a provider
context that lost separate timezone metadata still needs that upstream metadata
preserved before an experimental handoff can use it.

The public calculation ID stays unchanged. Every deliberately assigned execution
gets a fresh internal `execution_id`, including retries and later registration
after cleanup. Reusing the public ID creates a new calculation, with
`overwrite=True` by default. Internal attempt records prevent accidental replay
of that same request; they do not suppress a later deliberate calculation.
The scheduler's composite row identity and FinDer workspace names are unchanged.

The SQLite upgrade is additive: `event_tracker.execution_id` and the separate
`shakemap_scheduled_attempts` table preserve existing rows and retained external
history. Associations guard lifecycle writes by execution token, so a late result
cannot complete a newly registered row or reopen a row failed during restart.

After FinDer succeeds, the scheduler binds the execution and persists the exact
exported bytes, configuration, overwrite choice, and endpoint using
`prepare_scheduled_submission`. These prepared bundles can be inspected through
`prepared_scheduled_submissions`; preparation itself does not mean a POST occurred.
The scheduler reads only active bundles during dispatch. Retained historical
bundles are not automatically deleted or decoded on every monitoring cycle.

Same-ID requests dispatch in their retained preparation order. Before posting a
later request, PyFinder saves the preceding accepted job's terminal observation;
otherwise `overwrite=True` could erase the evidence before it is observed. A
`SUBMITTING` or `UNCERTAIN` predecessor holds later requests for that ID without
changing their bytes or IDs. It could still be writing inputs server-side after
a client timeout. Other IDs can progress. Unknown acceptance is not resolved by
guessing the latest service sequence, and no automatic operator-resolution
mechanism is supplied in this package.

A dedicated observer performs one pass per scheduler discovery cycle, even when
no events are due. Native execution does not occupy the FinDer worker pool while
waiting for its result. Read errors retain the accepted sequence and are retried
as observations, without rerunning FinDer or POST. Explicit pre-acceptance rejection
uses the existing three-attempt/ten-second execution retry policy. Native `FAILED`
is recorded as terminal without another scientific calculation, following the
approved integration direction. Native `SUCCESS` requires `products_ready` before
local completion; an unavailable archive is not usable chain success. These
outcomes do not imply copied products or delivered notifications.

A stored terminal result whose local transition failed is reapplied through its
association, even though `unresolved()` excludes terminal jobs. Interrupted local
retry persistence after rejection receives guarded failure finalization without
incrementing its retry count again. Local persistence errors remain observable.

Startup still marks abandoned local processing rows failed. Accepted external
jobs can continue to be observed, but never reopen those rows. Prepared requests
that had not been sent are retained for inspection and are not automatically
started after their local execution was abandoned. Orderly shutdown drains
finite in-flight operations, finalizes remaining local ownership, and retains
external records. It does not wait for native calculations to finish.

## Continuous configuration and input ownership

Only `start_monitoring` composes the external workflow. Configure the existing
`shakemap` settings explicitly:

```python
"service-enabled": True,
"service-url": "http://<reachable-shakemap-host>:<port>",
"request-timeout-seconds": 30.0,
"input-directory": "/absolute/resolved/caller-visible/data/inputs",
"configuration": "global",  # Or an explicitly selected service configuration name.
"overwrite": True,
```

The input directory must be the same canonical storage the service uses and be
readable/writable by both processes. It must already exist as an absolute,
resolved directory. Do not point it at products or an unrelated operator folder.
This code does not establish deployment mounts, network routing, credentials,
regional data suitability, or host/container path equivalence. Operators must
provide those deployment resources before enabling the feature.

`PreparedShakeMapInputs` requires exclusive caller ownership of these event
inputs. It holds a per-event PyFinder advisory lock across preparation and POST,
and uses the service's event-directory lock while inspecting/removing stale input.
The latter lock is released before POST so the service can acquire it. Locks
serialize requests rather than reject a deliberate same-ID submission.
Only an omitted stale `rupture.json` is removed; new `event.xml` and `event_dat.xml`
are always uploaded as a complete bundle. Unknown files, symlinks, and special
entries are refused untouched. Products, service state, and datasets are not
modified by this helper. All callers that write these inputs must respect this
ownership; unrelated REST writers do not participate in the PyFinder lock.

The service configuration is explicitly caller-selected, defaults to `global`,
and has no fallback. Legacy local regional paths and the FinDer profile name
are not silently translated into ShakeMap configuration selections. Existing
legacy modules remain retained; removed commented call sequences are preserved
in `legacy/manager-downstream-reference.md`.

Playback and on-demand are not automatically enabled by these settings. They
retain their existing isolation and do not use the operational scheduler database.
Experimental external execution and its retention policy remain separate work.

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
and are not used as a fallback. The removed manager/scheduler call sequences and
notification construction remain in `legacy/manager-downstream-reference.md`.
Related old helper modules remain present.
