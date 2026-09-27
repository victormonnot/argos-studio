# ARGOS Studio

A local UAV engineering workspace for recording an experiment, inspecting its
measurements and retaining the evidence behind an observation.

The first workflow combines a synthetic attitude source, persistent sessions,
time-linked annotations, reception-gap analysis and replay. An optional adapter
imports existing ARGOS recordings through their native validator. The interface
is currently in French; source code and technical documentation are in English.

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

1. Enter a session name and an objective, then select **Démarrer**. Roll, pitch
   and gyro X are generated at a nominal 20 Hz and saved as they arrive.
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

The generated angles follow deterministic mathematical signals. They do not model
flight dynamics or sensor performance. A requested two-second interruption and
the measured interval between surrounding samples are separate observations;
scheduling and sample cadence also contribute to that interval.

Gap analysis describes received data. It does not identify a network fault,
count lost packets or establish sensor-to-display latency. The same analysis
functions serve synthetic sessions and imported recordings.

## Import an ARGOS recording

Configure an existing ARGOS source checkout and its Python environment before
starting Studio. That environment must already contain ARGOS's MAVLink
dependencies; Studio does not install or modify the other checkout.

```sh
ARGOS_STUDIO_ARGOS_ROOT=/absolute/path/to/argos \
ARGOS_STUDIO_ARGOS_PYTHON=/absolute/path/to/argos/.venv/bin/python \
.venv/bin/argos-studio
```

Stop any active synthetic session, expand **Importer un enregistrement**, and
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
for sessions, samples and events, and `recordings/` for imported originals.
`ARGOS_STUDIO_DATA_DIR` also selects the directory. An interrupted acquisition is
retained and marked as interrupted when the application restarts.

| Boundary | Limit |
| --- | --- |
| Concurrent acquisition | One synthetic source |
| Synthetic session | Three minutes |
| Sessions per data directory | 100 |
| Imported recording | 10 MiB and 100,000 native events |
| Native import validation | 20 seconds |

There is no automatic deletion or total disk quota. When the session limit is
reached, retain the existing directory and start with another data directory.
For a backup, stop the server and copy the **whole** data directory, including
any SQLite auxiliary files and original recordings.

This version has no physical vehicle connection, flight or bench commands,
language-model integration, or integrated UAV Debugger/Meridian module. It
provides the acquisition, persistence and analysis workflow on which those
separate capabilities can build.

## Implementation and API

| Component | Responsibility |
| --- | --- |
| [`core.py`](src/argos_studio/core.py) | SQLite persistence, validation and deterministic window analysis |
| [`simulator.py`](src/argos_studio/simulator.py) | Synthetic acquisition, bounded interruptions and lifecycle |
| [`app.py`](src/argos_studio/app.py) | Local FastAPI endpoints, process ownership and HTTP boundaries |
| [`argos_import.py`](src/argos_studio/argos_import.py) | Optional subprocess adapter to ARGOS's native recording reader |
| [`static/`](src/argos_studio/static/) | Browser interface, charts, timeline and replay without a build step |

The interface uses the same HTTP analysis endpoints available to other local
clients. The [OpenAPI schema](http://127.0.0.1:8765/openapi.json) describes request
and response routes. Key endpoints are:

| Endpoint | Purpose |
| --- | --- |
| `GET /api/health` | Runtime limits and optional import configuration |
| `GET /api/sessions` / `POST /api/sessions` | List sessions or start synthetic acquisition |
| `GET /api/sessions/{id}` | Measurements, events and source status; optional `start_s` / `end_s` |
| `POST /api/sessions/{id}/annotations` | Attach an observation to a session time |
| `POST /api/sessions/{id}/dropout` / `stop` | Interrupt or finish the active synthetic source |
| `GET /api/sessions/{id}/analysis` | Evidence for a selected reception-time window |
| `GET /api/sessions/{id}/export` / `raw` | Export session JSON or retrieve an imported original |
| `POST /api/import/argos` | Validate and import a native recording |

Writes use JSON, except the import endpoint, which accepts
`application/octet-stream`. Browser writes are restricted to the same origin.

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
replay, mobile layout and connection errors without touching saved sessions.
