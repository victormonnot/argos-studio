# ARGOS Studio

A local UAV engineering workspace for recording an experiment, inspecting its
measurements and retaining the evidence behind an observation.

Record a local MAVLink/SITL stream or a synthetic attitude source, attach
observations, inspect reception gaps and replay the retained evidence. An
optional adapter imports existing ARGOS recordings through their native
validator. Versioned reception investigations retain findings, evidence and
proposed checks across sessions. The interface is currently in French; source code and technical
documentation are in English.

## Run locally

Use Python **3.12 or newer** on Linux or macOS. The process lock uses `fcntl`;
native Windows execution is not supported. Run these commands from the checkout:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.lock
.venv/bin/argos-studio
```

Open [http://127.0.0.1:8765](http://127.0.0.1:8765). The server binds to loopback
and serves the interface and API together. No frontend build, account, API key,
simulator installation or physical drone is needed for the synthetic workflow.

Use `--port 8766` to choose another port, or `--data-dir /path/to/data` to choose
the storage directory. One process owns each data directory.

When working over SSH, forward the local service with
`ssh -L 8765:127.0.0.1:8765 user@development-machine`, then open the same URL
on your own computer.

## Inspect a first experiment

1. Select **Simulation synthétique**, enter a session name and an objective,
   then select **Démarrer**. Roll, pitch and gyro X are generated at a nominal
   20 Hz and saved as they arrive.
2. Add an observation with **Marquer**. Each annotation retains its position in
   the session timeline.
3. Select **Provoquer une interruption de 2 s**. The source suspends sample
   reception for two seconds, records the intervention and resumes automatically.
4. Select **Arrêter la session**. Acquisition also stops automatically after
   three minutes. Reopen the session from the sidebar, including after a server
   restart, and use **Lire** or the time cursor to inspect the saved measurements.
5. Select a time window. **Continuité du flux** reports reception intervals and
   links detected gaps to the samples that bound them. Export the session as JSON
   with its metadata, samples, events and analysis.
6. In **Investigation de réception**, optionally record a working hypothesis,
   then select **Investiguer cette fenêtre**. Open a finding's evidence to
   inspect its exact measurement pair and return to that window in the chart.
   Reopen saved reports from the history or export a report as JSON.

The generated angles follow deterministic mathematical signals. They do not model
flight dynamics or sensor performance. A requested two-second interruption and
the measured interval between surrounding samples are separate observations;
scheduling and sample cadence also contribute to that interval.

Gap analysis describes received data. It does not identify a network fault,
count lost packets or establish sensor-to-display latency. The same analysis
functions serve synthetic sessions, live MAVLink reception and imported recordings.

## Investigate reception

An investigation operates on one consistent database snapshot, during acquisition
or replay. It combines the same receipt-interval analysis used by the chart with
source-clock regressions, capture dispositions and recorded experiment events.
For MAVLink captures, it examines datagrams strictly between the measurements
bounding each gap. A retained HEARTBEAT or other accepted frame from the selected
source contradicts total reception silence over that interval; it does not prove
continuous connectivity or explain the missing ATTITUDE measurements. Excluded
traffic cannot establish the presence of the selected source.

Each finding separates the observation, references, hypotheses, uncertainty and
a proposed verification. Boundary silence and incomplete acquisitions are shown
separately from intervals bounded by two samples. The 0.25-second threshold is
an inspection threshold, not a declared vehicle stream-rate requirement. At most
the 12 largest gaps receive individual findings; all gaps are counted and any
omissions are stated. Imported ARGOS sessions expose ATTITUDE samples to these
tools; other message types remain in their original recording and are not
correlated by this version.

Reports are **deterministic and do not use a language model**. The optional context
is retained as user-supplied text, without automatic interpretation. Suggested
checks are recorded, not executed. A changed hypothesis or new observations can
be examined by creating another report; existing reports remain unchanged.

The snapshot ends at the persisted session duration, which can lag current time
during a live reception silence. Reports include the algorithm version, tool
parameters/results, snapshot counts, evidence references and a SHA-256 digest.
`investigation.fingerprint()` hashes canonical UTF-8 JSON (sorted keys, compact
separators) from `Store.investigation_input()`: the session, all samples, events
and datagram metadata, including each payload's SHA-256. Window and context are
separate report parameters. The digest identifies recorded inputs, not their
authenticity. The snapshot stores the original session fields and last sample,
datagram and event identifiers, so its input can be reconstructed after later
acquisition or annotations. Evidence excerpts are embedded in the report;
complete UDP bytes remain in the separate raw capture export.

## Receive a local MAVLink/SITL stream

Select **MAVLink local · SITL déclaré**, enter a name and objective, and confirm
the local UDP port and expected MAVLink system/component identifiers. Defaults
are `127.0.0.1:14580` and source `1 / 1`. Select **Démarrer** to begin listening,
then start your separately configured simulator or telemetry forwarder.

For ArduPilot SITL, the following simulator startup option directs a configured
MAVLink serial channel to Studio:

```text
--serial0=udpclient:127.0.0.1:14580
```

When using `sim_vehicle.py`, pass the option through `-A`, as described in the
[ArduPilot UDP setup guide](https://ardupilot.org/dev/docs/using-sitl-for-ardupilot-testing.html#using-a-different-gcs-instead-of-mavproxy-via-udp).
Configure the sender to stream ATTITUDE measurements before the test. Studio
sends no heartbeat, stream-rate request, parameter update or vehicle command,
and the application does not launch a simulator automatically. The isolated
check below provides a separate, reproducible simulator setup.

The interface distinguishes a listening socket with no matching source from
received telemetry. ATTITUDE measurements become stale after 0.3 seconds without
a valid reception; heartbeat age is shown separately with a 2.5-second threshold.
These are local reception-age thresholds, not vehicle readiness checks. Stopping
the sender leaves the last measurements visible with their increasing age.
**Arrêter la session** closes Studio's listener and preserves the session; it
does not stop the external source. The interruption button is available only for
the synthetic source.

The first validated datagram containing the selected system/component pins its
UDP peer for the session. Traffic from another peer or source is retained as
excluded evidence when it comes from an IPv4 loopback address; other sender
addresses are ignored. If a simulator restart changes its UDP source port, start a
new Studio session. The source is declared as simulation by selecting this
profile; neither the declaration nor the peer address authenticates its origin.

The receiver accepts complete unsigned MAVLink 1/2 datagrams in the
`ardupilotmega` dialect. Malformed, partial, unknown-message and bad-CRC
datagrams cannot update measurements. Signed traffic is unsupported, and a
configured `MAV_IGNORE_CRC` bypass prevents acquisition. Within the capture
limits, original datagrams, including rejected ones, remain available. This decoder uses
Pymavlink's [direct dialect interface](https://mavlink.io/en/mavgen_python/).

ATTITUDE angles are converted from radians to degrees and angular rates from
radians/second to degrees/second. Its `time_boot_ms` remains vehicle boot time.
Studio records host receipt time separately and uses a monotonic host clock for
session-relative intervals; no clock offset or end-to-end latency is inferred.
See the [MAVLink ATTITUDE definition](https://mavlink.io/en/messages/common.html#ATTITUDE).

## Import an ARGOS recording

Configure an existing ARGOS source checkout and its Python environment before
starting Studio. That environment must already contain ARGOS's MAVLink
dependencies; Studio does not install or modify the other checkout.

```sh
ARGOS_STUDIO_ARGOS_ROOT=/absolute/path/to/argos \
ARGOS_STUDIO_ARGOS_PYTHON=/absolute/path/to/argos/.venv/bin/python \
.venv/bin/argos-studio
```

Stop any active acquisition, expand **Importer un enregistrement**, and
select a native ARGOS `.jsonl` recording. For example, ARGOS's provided
`examples/demo-flight/05aa147d371144c793c8f88ec3ef0509.jsonl` contains a recorded
Gazebo/ArduPilot simulation; it is suitable for telemetry inspection without
starting either simulator.

The adapter runs ARGOS's passive `read_recording` parser in a bounded subprocess.
It validates the recording format, MAVLink frames, reception order, completion
record and checksum before importing ATTITUDE measurements. A recording must
contain ATTITUDE data from exactly one system/component pair. Other MAVLink
messages and visual sidecars are not exposed as Studio measurements in this
version. The original uploaded file is retained unchanged and available through
`GET /api/sessions/{id}/raw`.

Provenance includes the source checksum, recording and decoder versions, vehicle
identifiers and native capture context. Environment labels describe the original
capture's declaration. Recordings without that declaration remain **unknown**.
Replay never opens a vehicle connection or reissues commands.

Receiver-monotonic timestamps and vehicle boot time remain separate clocks.
Session-relative time is derived from the recording's receipt origin; native
receipt values are never relabeled as Unix timestamps. An optional recorded UTC
capture date is retained as metadata without assuming clock synchronization.

## Data and limits

Data is stored in the ignored `.data/` directory by default: a SQLite database
for sessions, samples, events, investigations and raw UDP datagrams, and `recordings/` for
imported originals.
`ARGOS_STUDIO_DATA_DIR` also selects the directory. An interrupted acquisition is
retained and marked as interrupted when the application restarts. Earlier Studio
databases are migrated transactionally at startup, preserving existing sessions.

UDP payloads are stored as binary data with receipt timestamps, peer, disposition
and references to the derived sample sequence range. After stopping acquisition,
`GET /api/sessions/{id}/capture` exports this evidence as JSON with base64 payloads.
The normal session export includes measurements, analysis and capture counts;
the separate capture export includes every retained datagram's bytes.

| Boundary | Limit |
| --- | --- |
| Concurrent acquisition | One synthetic or MAVLink UDP source |
| Acquisition session | Three minutes |
| MAVLink capture | 10 MiB, 20,000 datagrams and 100,000 derived samples |
| MAVLink datagram | 128 complete frames |
| Sessions per data directory | 100 |
| Imported recording | 10 MiB and 100,000 native events |
| Native import validation | 20 seconds |
| Investigations per session | 50 immutable reports, up to 4 MiB each |

There is no automatic deletion or total disk quota. When the session limit is
reached, retain the existing directory and start with another data directory.
For a backup, stop the server and copy the **whole** data directory, including
any SQLite auxiliary files and original recordings.

The implemented live profile is local UDP reception for declared simulation.
TCP, serial/USB acquisition, hardware commands, language-model integration and
integrated UAV Debugger/Meridian modules are outside this version.

## Implementation and API

| Component | Responsibility |
| --- | --- |
| [`core.py`](src/argos_studio/core.py) | SQLite persistence, validation and deterministic window analysis |
| [`simulator.py`](src/argos_studio/simulator.py) | Synthetic acquisition, bounded interruptions and lifecycle |
| [`mavlink.py`](src/argos_studio/mavlink.py) | Receive-only UDP, decoding, source selection and freshness |
| [`acquisition.py`](src/argos_studio/acquisition.py) | Route session operations to the active source |
| [`investigation.py`](src/argos_studio/investigation.py) | Snapshot-based reception tools, evidence references and versioned reports |
| [`app.py`](src/argos_studio/app.py) | Local FastAPI endpoints, process ownership and HTTP boundaries |
| [`argos_import.py`](src/argos_studio/argos_import.py) | Optional subprocess adapter to ARGOS's native recording reader |
| [`static/`](src/argos_studio/static/) | Browser interface, charts, timeline and replay without a build step |

The interface uses the same HTTP analysis endpoints available to other local
clients. The [OpenAPI schema](http://127.0.0.1:8765/openapi.json) describes request
and response routes. Key endpoints are:

| Endpoint | Purpose |
| --- | --- |
| `GET /api/health` | Runtime limits and optional import configuration |
| `GET /api/sessions` / `POST /api/sessions` | List sessions or start synthetic/MAVLink UDP acquisition |
| `GET /api/sessions/{id}` | Measurements, events and source status; optional `start_s` / `end_s` |
| `POST /api/sessions/{id}/annotations` | Attach an observation to a session time |
| `POST /api/sessions/{id}/dropout` / `stop` | Interrupt synthetic reception or stop the active acquisition |
| `GET /api/sessions/{id}/analysis` | Evidence for a selected reception-time window |
| `GET` / `POST /api/sessions/{id}/investigations` | List reports or investigate a window with optional context |
| `GET /api/sessions/{id}/investigations/{report_id}` | Read a saved report; append `/export` to download JSON |
| `GET /api/sessions/{id}/export` / `raw` | Export session JSON or retrieve an imported original |
| `GET /api/sessions/{id}/capture` | Export a stopped MAVLink session's raw UDP evidence |
| `POST /api/import/argos` | Validate and import a native recording |

Writes use JSON, except the import endpoint, which accepts
`application/octet-stream`. Browser writes are restricted to the same origin.
Investigation requests accept `start_s`, `end_s` and `context` (up to 2,000
characters). Omitted bounds select the whole persisted session; explicit bounds
must be finite, ordered and within its recorded duration. All tools run locally.

## Verify changes

```sh
.venv/bin/python -m pytest -q
.venv/bin/ruff check .
.venv/bin/ruff format --check .
```

These checks do not need a paid service or hardware. The native import integration
test is optional: set both ARGOS installation variables above when running pytest
to exercise the existing demo recording and corrupted-input rejection. Without
them, that integration test is skipped; adapter unit tests still run.

Browser checks require Node.js 20 or newer:

```sh
npm ci
npx playwright install chromium
npm run test:e2e
```

The browser suite starts its own server on port 8766 with a temporary data
directory. It exercises capture, annotation, interruption evidence, export,
replay, investigations, report history/export, mobile layout and connection errors
without touching saved sessions.

## Optional isolated SITL check

The native check requires **Linux x86_64**, `unshare`, `iproute2` and permission
to create user, network and PID namespaces. It uses an already installed
[ArduCopter 4.6.3 executable](https://firmware.ardupilot.org/Copter/stable-4.6.3/SITL_x86_64_linux_gnu/arducopter)
with SHA-256:

```text
7862662092edc2861fc03da3d6fb2f0136d1670e563ca324eb52c1a324d1e14b
```

No binary is downloaded or rebuilt by Studio. The check creates fresh simulator
state and a passive receiver in one loopback-only namespace. This isolates the
simulator's auxiliary RC socket, which the pinned
[ArduPilot RC backend](https://github.com/ArduPilot/ardupilot/blob/3fc7011a7d3dc047cbb17d8bd98ee94577d144c6/libraries/AP_RCProtocol/AP_RCProtocol_UDP.cpp)
binds on all interfaces. If namespace setup fails, the test skips without
launching SITL.

```sh
ARGOS_STUDIO_SITL_BINARY=/absolute/path/to/arducopter \
.venv/bin/python -m pytest tests/test_sitl.py -q
```

To retain an inspectable run, invoke the same helper from the checkout using a
**new** output directory:

```sh
unshare --user --map-root-user --net --pid --fork --kill-child=SIGKILL \
  /bin/sh -c 'ip link set dev lo up && exec "$@"' sh \
  "$PWD/.venv/bin/python" -B "$PWD/tools/run_sitl.py" \
  --binary /absolute/path/to/arducopter \
  --output "$PWD/.data/sitl-proof"
```

The helper verifies the binary hash, starts only its own disarmed simulator,
observes HEARTBEAT and ATTITUDE, stops that process, verifies stale reception,
and reopens the persisted session to check its measurements and raw references.
It retains `result.json`, `session.json`, `capture.json`, the SQLite database,
simulator parameters and output. This validates ground telemetry reception and
persistence, not flight behavior.

After the helper exits, inspect that session with a separate Studio instance:

```sh
.venv/bin/argos-studio --data-dir .data/sitl-proof --port 8767
```

Open [http://127.0.0.1:8767](http://127.0.0.1:8767). Viewing the saved evidence
does not restart the simulator.
