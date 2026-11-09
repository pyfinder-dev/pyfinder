# pyfinder

Python wrapper for the FinDer executable and its library.

## Overview

PyFinder acquires seismic observations, runs FinDer, and can submit its selected
solution to a separate ShakeMap service. Continuous operation schedules event
updates and reports their terminal outcomes by email when configured. Playback
runs selected event IDs through the same calculation and notification services.
FinDer-backed workflows run inside the PyFinder application container.

- [Installation and runtime](#installation-and-runtime)
- [Quick Start](#quick-start)
- [Observation providers and USGS event association](#observation-providers-and-usgs-event-association)
- [Current execution boundaries](#current-execution-boundaries)
- [Sequence Diagram](#sequence-diagram)
- [Terminal email alerts](#terminal-email-alerts)

## Installation and runtime

Use the [deployment guide](../pyfinder-deploy/README.md) to build the application
image, prepare shared storage and configure the separate ShakeMap service. Keep
`pyfinder` and `pyfinder-deploy` as sibling checkouts. The component host launcher
resolves its own checkout, including symlink invocation, and uses the sibling
`pyfinder-deploy/runtime` directory regardless of the shell's working directory.
It does not build images or choose another runtime from an environment variable.

The application runs as UID/GID `1000:1000`. Its mandatory shared runtime mount
is `/home/sysop/runtime`, with PyFinder state, logs, runs and playbacks under
`/home/sysop/runtime/pyfinder`. These are container paths, not host checkout
locations. The host directories must be writable by that runtime account.

## Quick Start

From the **PyFinder checkout**, inspect the host launcher without starting work:

```bash
scripts/pyfinder --help
scripts/pyfinder status
```

The host launcher manages the canonical `pyfinder-docker` container. Inside the
configured application container, the installed workflow commands are:

```bash
pyfinder continuous
pyfinder playback --list
pyfinder playback --event-ids EVENT_ID
pyfinder playback --event-ids EVENT_ID_1 EVENT_ID_2 --full-schedule --fast
```

These workflow commands require the runtime and dependencies prepared by the
deployment guide. Use the host launcher's corresponding commands to invoke
playback in the existing application container; playback does not start a second
container. It keeps its state separate from the continuous scheduler database.

### Playback

`pyfinder playback` uses the maintained list of event IDs. Supply one or more
IDs with `--event-ids` to select other events; `--list` displays the maintained
list without running calculations. Each attempt queries the configured providers
and uses their priority-selected earthquake context, including for maintained
events. It does not recreate historical observation availability.

By default, each event gets one immediate execution of the full chain:
acquisition, FinDer, ShakeMap, and configured terminal notification handling.
`--full-schedule` adds the configured follow-ups, currently at 5, 15, 60, 180,
360, 1440, and 2880 minutes after registration. `--fast` makes those follow-ups
due two minutes apart, so the complete schedule is due at 0, 2, 4, 6, 8, 10,
12, and 14 minutes for each event. It has no effect on a single-step schedule.

Fast timing applies within each event; different events may be due together.
It controls when work becomes eligible, not when FinDer finishes or the
ShakeMap HTTP request is sent. Existing queueing, retry timing, and same-ID
serialization still apply. Calculation IDs retain their nominal schedule step;
logs distinguish that step from the actual due and execution times.

Playback requires the ShakeMap configuration described below. It reports a
missing prerequisite rather than treating FinDer-only execution as a completed
full chain. Email follows the existing alert configuration. To run a manual
calculation without sending email, explicitly disable alert configuration for
that command, for example from the PyFinder checkout:

```bash
PYFINDER_ALERT_CONFIG='' scripts/pyfinder playback --event-ids EVENT_ID
```

Playback prints the path to its retained database:
`runtime/pyfinder/state/playbacks/<command-trigger-UTC>/scheduled_queries.sqlite3`.
Its captured alert evidence is stored in `alert-evidence/` beside that database.
Use this invocation's database, rather than the continuous database, when
inspecting playback delivery records with the command below.

The command waits for the selected schedules and accepted ShakeMap jobs to settle.
It returns zero for successful calculation chains with successful or deliberately
suppressed email handling. Calculation failures, uncertain submissions, observation
or evidence errors, and failed, blocked, unknown, or partial delivery produce a
nonzero result; Ctrl+C returns 130. State is retained for inspection. A later
playback invocation starts new work and does not resume or automatically replay
previous uncertain submissions or messages. If another invocation still owns the
same public calculation ID, playback reports that conflict before submitting it.
An uncertain or interrupted owner can retain that protection after exit; inspect
the named owner database rather than deleting the marker to force replacement.

The public `on-demand`, singular `--event-id`, and `--test` options are removed.
Use `playback --event-ids` for a specific event. `--verbosity` controls application
logging; it does not change calculation behavior.

---

## Regional ShakeMap selection

PyFinder selects a ShakeMap service profile from the selected FinDer solution's
location, using the same coordinates it exports in `event.xml`. Configure
`shakemap["regional-configurations"]` in `pyfinder/pyfinderconfig.py` with arbitrary
human region labels. Each entry declares a packaged WKT coverage file and its
service profile name. Labels and profile names do not impose national boundaries.

A region may cover Switzerland, Italy and Germany together, split a CH-labelled
area into separate configurations, or contain disjoint `MULTIPOLYGON` areas.
Several regions may share one service profile. FinDer's computational regions and
configuration names remain independent.

Matching areas are resolved to service profiles before ambiguity is assessed:
adjacent/overlapping areas using the same profile select it once. Different
matching profiles produce a warning and use `global`; no priority order is
inferred. Valid locations outside all registered WKT areas also use `global`.
Invalid solution coordinates remain errors. Missing or unusable optional WKT
resources log the region label, file and correction, then conservatively use
global for that calculation; the service processes remain running.

The independent files live in `pyfinder/extern/shakemap_regional_wkt/`. They must
contain valid WGS84 longitude/latitude `POLYGON` or `MULTIPOLYGON` geometry. The nine
initial seeds preserve the previous footprints; their
[provenance](docs/shakemap-region-coverage.md) is recorded separately.
Edit/add resources and rebuild/redeploy the caller for intended regional coverage.
Only explicitly registered files are used. Runtime selection does not read a
country-code database or copy FinDer's WKT resources.

A selected native profile is attempted even if service discovery reports it
missing. Only its confirmed eligible configuration failure permits one explicit
global recovery; both outcomes remain visible. Native scientific assets remain
service-owned; use its supported provisioning or image-installation paths when
those files are supplied. Their presence, compatibility and scientific
qualification are separate from caller coverage selection.

The process-wide `PYFINDER_SHAKEMAP_CONFIGURATION` override and old local-profile
settings are obsolete: PyFinder warns and ignores them without interrupting
startup. Remove them when convenient; they no longer select a profile. Runtime
warnings are retained in the owning continuous or playback process log, and the
source settings check includes them as separate warning metadata. Invalid
supported settings still fail validation. The service's per-request configuration field
and manual `shake-in-docker` selection remain supported. See the
[adapter configuration](docs/shakemap-adapter.md#workflow-configuration-and-input-ownership) and
[deployment migration instructions](../pyfinder-deploy/README.md#upgrade-the-caller)
for coordinated settings/image updates. Existing prepared requests retain their
selected profile and inputs across restart; this change requires no queue reset.

## Observation providers and USGS event association

ESM ShakeMap, RRSM Peak Motion, EMSC felt reports and USGS DYFI are enabled by
default.
With the shipped settings, each normal attempt includes DYFI as a global
observation source, including events outside European service coverage. This
does not guarantee usable reports: event association, publication and scientific
filtering can still leave an event without observations.

USGS ShakeMap station amplitudes remain opt-in. Add
`USGS_SHAKEMAP_SERVICE` to `pyfinderconfig["general"]["services-enabled"]` in
`pyfinder/pyfinderconfig.py` to include the station amplitudes. These are
maintained application settings, not additional CLI or Make commands. From the
deployment checkout, rebuild with
`make build COMPONENT=pyfinder`. A running container still uses its installed
image: stop it and deliberately recreate the canonical caller using the
[deployment guide](../pyfinder-deploy/README.md), preserving mounted runtime
and any needed container-only files. Restarting an old container does not
install the new settings.

Provider membership is configurable: add or remove the corresponding service
constant in `services-enabled` to enable or disable a source. The USGS
configuration names are `USGS_ShakeMap` and `USGS_DYFI`.

The shipped `services-priority` places USGS ShakeMap and then USGS DYFI after
ESM, RRSM, and EMSC felt reports. Thus the default DYFI source preserves established
providers' preference for playback event metadata and duplicate instrumental
stations. To change that preference, reorder `services-priority`; if supplying
an entirely custom list, include every enabled source. Membership and priority
are separate settings.

For each attempt that enables a USGS source, PyFinder asks the
[EMSC EventID service](https://www.seismicportal.eu/eventid/) for the preferred
USGS association of the original **UNID** (for example `20230206_0000008`). UNID
is distinct from the numeric EMSC event-page ID. This is an estimated association
based on event parameters, not a guaranteed identity crosswalk. The service's
default matching parameters were tested for earthquakes above magnitude 4.5;
smaller or newly reported events may have no match. PyFinder retains the service's
misfit and does not introduce a local matching threshold or ranking algorithm.

One association lookup and one ParamWS ComCat query serve both enabled products.
PyFinder validates the returned ComCat ID against the query ID or ComCat's own
confirmed aliases. The original EMSC workflow ID, scheduled times, and FinDer
and downstream ShakeMap calculation IDs remain unchanged. Continuous runs still
use their persisted EMSC origin metadata; playback uses the configured provider
priority. USGS GeoJSON origin milliseconds are converted to explicit UTC time
when adapting a candidate provider context.

A missing association, timeout, malformed response, or conflicting preferred
IDs makes only the dependent USGS sources unavailable. Existing providers can
still contribute; the normal scheduler decides whether an insufficient attempt
needs retrying. No association result is permanently cached. A usable USGS
product survives failure of the other product, and later scheduled attempts may
find data that was not yet published.

USGS ShakeMap station PGA is normalized from its declared units, with its own
source provenance. DYFI MMI is converted using the existing Worden relation;
it is not relabeled as EMSC EMS-98. A DYFI grid cell uses its polygon centroid as
a representative location, retaining its original geometry and coordinate
method with the normalized observation. The centroid is a planar approximation
for the small cell, handles holes and date-line crossings, and is not a geodesic
calculation. Supplied Point geometry keeps its coordinates. Invalid polygons
and unsupported geometry, including MultiPolygon, are skipped with a diagnostic.
It is an area report, not an instrumental station or an exact respondent
location. Felt cells are not deduplicated as instrumental stations. DYFI uses
the existing Allen felt-intensity filter before Worden conversion.

Provider outcome metadata and logs distinguish the original workflow ID, mapped
query ID, returned canonical ID, association misfit, product, and failure reason.
Normalized records retain source, original value and units, plus DYFI area
provenance. The existing captured `event-context.json` retains selected USGS
observations and their acquisition outcomes alongside the FinDer input evidence.
It contains the USGS observations surviving normalization and merging, not every
fetched or rejected row. If an attempt stops before FinDer input capture, inspect
its provider diagnostics and logs. This association changes observation acquisition
only; the separate ShakeMap calculation service and its configuration fallback
policy are unchanged.

**Current timestamp limitation:** observation normalizers share a helper that
interprets supported `Z` timestamps in the process's local timezone. Run the
application process in UTC until that helper is corrected. The ComCat adapter's
explicit UTC conversion does not fix this later step; host-timezone-independent
observation timestamps are not yet guaranteed.

Offline tests and independent source audits cover the USGS integration. They do
not establish live-provider availability, installation in the application image,
or a successful full-chain playback with these sources. After enabling them and
rebuilding, verify the installed caller and perform an explicit playback through
the configured deployment before relying on USGS operationally.

## Current execution boundaries

Continuous operation and playback use the same ShakeMap REST adapter to submit
and monitor jobs. Continuous operation leaves it disabled by default; playback
requires it for the full chain. Enabling the adapter requires
an explicit endpoint and shared caller-owned input directory, supplied through
the documented `PYFINDER_SHAKEMAP_*` environment variables. See the
[adapter configuration and limitations](docs/shakemap-adapter.md).

General product distribution is not provided by the adapter. Terminal email
uses retained input and diagnostic evidence, configured separately below.
Enabling the adapter does not validate regional data, establish network access
or confirm delivery to an SMTP server.

---

## Sequence diagram

The diagrams show the current continuous workflow. The external ShakeMap
steps run only when explicitly enabled; notification delivery remains separate.

### Listening event alerts from EMSC

```mermaid
sequenceDiagram
    autonumber
    participant SLA as ServiceLauncher
    participant SLI as SeismicListener
    participant FUS as FollowUpScheduler
    participant DB as ThreadSafeDB

    SLA->>SLI: start_emsc_listener()
    SLA->>FUS: init(), run_forever()

    SLI->>DB: Persist update schedules
```

### Execution of update schedule
```mermaid
sequenceDiagram
    autonumber
    participant DB as ThreadSafeDB
    participant ET as EventTracker
    participant FUS as FollowUpScheduler
    participant FM as FinderManager
    participant P as ParamWS package
    participant FE as FinDerExecutable
    participant SM as ShakeMapService

    loop periodic 
      FUS->>ET: poll_due_events()
      ET->>DB: query_due()
      DB-->>ET: events
      ET-->>FUS: due events
    end

    alt for each due event
      FUS->>FM: Trigger update
      FM->>P: Query remote web services
      P-->>FM: Return data
      FM->>FE: Execute FinDer
      FE-->>FM: Return solution
      FM-->>FUS: Return selected solution
      opt ShakeMap service enabled and solution usable
        FUS->>FM: Prepare native input bytes
        FM-->>FUS: Existing calculation ID and inputs
        FUS->>DB: Retain request and submission intent
        FUS->>SM: Submit when preceding same-ID outcome is recorded
        SM-->>FUS: Acknowledge accepted sequence
        FUS->>DB: Retain accepted sequence
      end
      Note right of FUS: Terminal email uses retained evidence; full product copying is separate
    end

    opt ShakeMap service enabled
      loop Observer passes, including when no events are due
        FUS->>SM: Read exact accepted job sequence
        SM-->>FUS: Job outcome
        FUS->>DB: Save observation and guarded local outcome
      end
    end
```


## Terminal email alerts

Continuous operation reads the private email configuration. The search order is `pyfinder/services/.pyfinder_alert_config.json`, then
`pyfinder/.pyfinder_alert_config.json`; those files are excluded from images and
Git. The [configuration template](pyfinder/.pyfinder_alert_config_template.json)
shows the supported existing fields; provide your own account and recipients.
An optional `PYFINDER_ALERT_CONFIG` selects an absolute path to the same
JSON format, for example an operator-owned file under the existing shared
runtime mount. An empty value explicitly disables mail. No configuration means
no delivery. The launcher forwards this variable by name and refuses an explicit
mismatch with an existing container; it does not recreate containers or copy
credentials. Keep the file readable only by the intended runtime account.

The configuration uses `smtp_server`, `smtp_port`, `from`, `to`, `password` and
`subject`. SMTP authentication uses `from`; the historical `address` field is
accepted but unused. Delivery connects to `smtp_server`.
`security` defaults to `starttls` (or explicitly `tls`); `timeout` defaults to 30
seconds and must be positive and at most 300. Recipients remain hidden from each
other in message headers. Settings are kept outside scientific configuration
and never included in diagnostic logs.

Without `alert_lists`, the existing `to` recipients form one `default` audience
for SUCCESS and FAILED outcomes. To choose separate audiences, add `alert_lists`
to that same private file. Each entry has `name`, `recipients`, `outcomes` and
`required_attachments`; outcomes can include SUCCESS, FAILED, INTERRUPTED and
UNKNOWN. No recipients are provided automatically. An empty required list means
all retained attachments are optional; absent native artifacts are identified in
the report. Require specific filenames such as `data_0`, `service.log`,
`provenance.json`, `product-manifest.json` or `intensity.jpg` when appropriate.
Missing required evidence holds delivery as BLOCKED. Native requirements apply
to the final attempt, not an earlier failed regional run.

Messages identify the public calculation, private attempt, native sequence and
selected configuration. Service selection and materialization are reported
separately from evidence that native execution occurred. When PyFinder requests
global after a confirmed regional configuration failure, the report retains
both attempts, the fallback reason and the final outcome.
Where FinDer input was prepared, the manager also retains its authoritative
earthquake and provider display metadata for the readable email summary.
Observation time is labelled separately from earthquake origin. The selected
FinDer solution summary is separately retained at manager return,
with depth in km and magnitude unchanged. Physical origin comes from authoritative
earthquake metadata, never FinDer's internal timestamp. Missing values remain
explicitly unavailable; no catalogue magnitude type is assigned to FinDer.
FinDer input bytes and available native request/log/provenance/manifest/image
artifacts are captured in caller-owned `state/alert-evidence` before mutable
same-ID paths are reused. This is an email evidence snapshot, not a complete
scientific product archive. No scientific retry is caused by email delivery.

Delivery records live alongside workflow state in the existing SQLite database.
PENDING survives restart; SENT is not replayed. Explicit rejection is FAILED,
partial recipient acceptance is PARTIAL, and an uncertain SMTP completion or
interrupted SENDING record is UNKNOWN. UNKNOWN and PARTIAL are never replayed
automatically; exactly-once SMTP is not promised. A changed audience configuration
blocks already queued mail rather than silently changing its recipients.
SMTP runs outside the scheduler's ShakeMap phase lock.

Inside the application container, inspect delivery state and missing required
artifacts without sending mail:

```sh
python3.12 -m pyfinder.services.alert_delivery \
  --database /home/sysop/runtime/pyfinder/state/scheduled_queries.sqlite3 list
```

A definite FAILED delivery can be explicitly requeued with `retry-failed
<execution-id> <audience>` in place of `list`. This command sends nothing;
the running continuous owner drains the queued item. UNKNOWN/PARTIAL cannot be
requeued through it. The command never reads credentials or prints message
bodies or recipient lists. If running the inspection command on the host,
activate the project's Python environment and supply the corresponding absolute
host database path. Do not point experimental tests at this operational database.

Missing configuration suppresses delivery. Invalid configuration stops workflow
startup; connection or authentication failure records a delivery failure without
repeating scientific work. Validate the intended server and recipients before
enabling operational mail. See [verification](docs/shakemap-adapter.md#verification)
for the distinction between offline tests and deployed checks.

The former sender is retained as historical reference in
[legacy/alert.py](legacy/alert.py); it is not an active fallback.
