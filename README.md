# ARGOS Studio

A local UAV engineering workspace for recording an experiment, inspecting its
measurements and retaining the evidence behind an observation.

Record a local MAVLink/SITL stream or a synthetic attitude source, attach
observations, inspect reception gaps and replay the retained evidence. An
optional adapter imports existing ARGOS recordings through their native
validator. Versioned reception investigations retain findings, evidence and
proposed checks across sessions. A bounded synthetic experiment can compare an
unperturbed control with a deliberate interruption. The interface is currently
in French; source code and technical documentation are in English. An optional
agent can use the same instruments and retain an inspectable trace of its work.

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
7. From a synthetic report, prepare the comparison protocol, then launch it.
   Inspect the control and interrupted captures, their comparison and the
   referenced interval. The original session and report remain unchanged.

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
is retained as user-supplied text, without automatic interpretation. Creating a
report does not execute its suggested checks. A changed hypothesis or new observations can
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

## Execute a synthetic comparison

Open a report from a **Simulation synthétique** session and prepare an experiment.
The proposal records a fixed protocol and its originating report. Preparation
does not start acquisition. Read the protocol, stop any existing acquisition,
and use the separate launch action. A proposal expires after ten minutes and
can start only once; repeating a finished or cancelled experiment requires a new
proposal. Reports from MAVLink captures and imported recordings cannot launch
this protocol.

| Phase | Duration | Source | Intervention |
| --- | --- | --- | --- |
| Control | 6 seconds | Synthetic attitude, nominal 20 Hz | None |
| Perturbed | 6 seconds | Same generator and cadence | Suspend for 2 seconds at approximately t = 2 s |

The executor creates two ordinary replayable sessions, linked to the proposal
and its original investigation. It reserves acquisition across both phases;
another session, import or manual interruption cannot interleave with the
protocol. A global progress panel keeps cancellation accessible even when a
different session is selected. Cancelling, or stopping an experiment's active
capture, stops the sequence and retains its partial evidence. Server shutdown
marks an active experiment interrupted; a process restart never resumes it.
Each generator phase stops itself after six seconds, and the orchestrator has a
20-second monotonic deadline. These are local software bounds for a synthetic
source, not a physical failsafe or hard real-time guarantee.

The comparison checks source/generator identity, nominal and observed cadence,
completed captures, coverage at both edges, and at least 80% of the nominal
sample count outside the suspension. The control must have no interruption;
the perturbed capture must contain one recorded suspension and resumption. The
expected response is one measured gap encompassing those events, between 2 and
2.35 seconds for this protocol. Timing tolerance is 0.25 seconds and cadence
tolerance is ±20%; these are declared comparison criteria, not inferred vehicle
requirements. The interface shows which conditions pass or fail.

A supported result means the controlled synthetic response was observed. An
incomplete or non-comparable capture yields an inconclusive result; usable
captures with a different response report that it was not reproduced. The
comparison also references the original report and the difference in gap
duration. Similar durations do not establish a common cause. A single pair of
captures does not establish repeatability, sensor latency or hardware behavior.

Completed and interrupted experiments remain in the history, with links to
their original report and both captures. Export their protocol, lifecycle and
comparison as JSON; each linked session has its own full measurement export.
No language model, simulator binary or vehicle connection is involved in this
synthetic protocol.

## Connect an investigation agent

The agent is disabled by default. Configure the OpenAI adapter and an explicit
model supporting Responses function calling before starting Studio. From the
checkout, create a local configuration file:

```sh
cp -n .env.example .env
chmod 600 .env
```

Edit `.env` locally to fill `ARGOS_STUDIO_AGENT_MODEL` and `OPENAI_API_KEY`, then
run `.venv/bin/argos-studio`. The `.env` file is ignored by Git; `.env.example`
contains only public placeholders. Credentials stay on the server, outside the
browser and database.

At startup, Studio uses [python-dotenv](https://github.com/theskumar/python-dotenv)
to load only `.env` in the launch directory; it does not search parent directories.
Existing process variables take precedence, including explicitly empty values.
The command-line `--data-dir` option takes precedence over both. Values are not
expanded as shell commands or interpolated from other variables. Restart Studio
after editing the file. Supplying an explicit `Settings` object to `create_app`
bypasses file loading, including in tests. Missing configuration leaves all recording,
investigation and synthetic experiment functions usable. An available adapter
means configuration is present, not that credentials or model access have been
validated; provider errors appear on the corresponding request.

Select a session and time window, then use **Investiguer avec l’agent**. For example:
“Examine cette interruption, conserve les preuves et prépare un essai synthétique
pour vérifier la signature observée.” The initial window is fixed to the recorded
duration when the request starts. Each question starts independently; prior
conversation text is not automatically sent. Saved reports and related experiments
are discoverable through tools.

The provider receives the question, session name/objective/provenance, recent
annotations and report/experiment references, then the excerpts requested by its
tools. The complete raw recording, UDP bytes, arbitrary metadata and local paths
are not sent by these tools. The interface states this transfer before submission.
The server contacts the fixed OpenAI HTTPS endpoint; it does not accept arbitrary
provider URLs. Requests use `store: false`; this is not a claim of zero provider
retention. See the official [Responses state handling](https://developers.openai.com/api/docs/guides/migrate-to-responses)
and [function calling protocol](https://developers.openai.com/api/docs/guides/function-calling).

| Instrument | Result |
| --- | --- |
| Session context | Declared provenance, objective, recent notes and saved evidence references |
| Measurement window | Up to 100 samples; continuity calculated over the complete selected window |
| Reception investigation | A new immutable deterministic report with evidence references |
| Saved investigation | A bounded excerpt from a report belonging to this session |
| Synthetic experiment preparation | A persisted proposal for the existing fixed protocol |
| Experiment result | Protocol, lifecycle and measured comparison for a related experiment |

Every tool call and result is saved before the provider continues. Excerpts state
their truncation; full reports remain accessible in Studio. The interface separates
the model's text from tool traces and derives evidence links from tool results.
A language-model answer can be mistaken; the retained measurements and deterministic
reports remain the evidence. Neither an annotation nor a model response grants
additional actions. Tools cannot start acquisition, launch an experiment, issue a
vehicle command, read arbitrary files or execute code. Examine a prepared proposal
and launch it separately in the experiment panel.

One request runs at a time, bounded to six provider calls, eight tool calls
(including the initial context), 2,000 output tokens per call, 6,000 in total,
and a 90-second execution deadline. Each HTTP call has a 30-second network
timeout. Requests and responses are capped at 256/512 KiB; tool results at 48 KiB.
These limits bound work, not a monetary amount. API usage is billed by the chosen
provider/model; set any spending restriction in that provider's account.

Cancellation stops further calls and preserves completed tool results. A local
tool already executing is drained before the request becomes terminal; an already
submitted remote request may still incur usage. Repeated identical report/proposal
calls in one request reuse their first result. There are no automatic retries,
provider fallbacks or restarts. Shutdown and crash recovery preserve incomplete
traces without resuming them. Final answers, known token usage and errors survive
reload and can be exported as JSON. Opaque provider reasoning used for continuation
is held only in memory and is not displayed or stored in the trace.

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
for sessions, samples, events, investigations, experiments, agent traces and raw UDP datagrams,
and `recordings/` for imported originals.
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
| Experiment proposals per originating session | 20; protocol up to 64 KiB, result up to 4 MiB |
| Synthetic experiment | Two 6-second captures; 20-second execution deadline; 10-minute proposal expiry |
| Agent history per session | 50 requests, up to 32 trace steps of 64 KiB each; final text up to 16,000 characters |

There is no automatic deletion or total disk quota. When the session limit is
reached, retain the existing directory and start with another data directory.
For a backup, stop the server and copy the **whole** data directory, including
any SQLite auxiliary files and original recordings.

The implemented live profile is local UDP reception for declared simulation.
TCP, serial/USB acquisition, hardware commands, local-model adapters and
integrated UAV Debugger/Meridian modules are outside this version.

## Implementation and API

| Component | Responsibility |
| --- | --- |
| [`core.py`](src/argos_studio/core.py) | SQLite persistence, validation and deterministic window analysis |
| [`simulator.py`](src/argos_studio/simulator.py) | Synthetic acquisition, bounded interruptions and lifecycle |
| [`mavlink.py`](src/argos_studio/mavlink.py) | Receive-only UDP, decoding, source selection and freshness |
| [`acquisition.py`](src/argos_studio/acquisition.py) | Route session operations to the active source |
| [`investigation.py`](src/argos_studio/investigation.py) | Snapshot-based reception tools, evidence references and versioned reports |
| [`experiments.py`](src/argos_studio/experiments.py) | Consume a proposal once, execute two bounded synthetic captures and retain outcomes |
| [`comparison.py`](src/argos_studio/comparison.py) | Check comparability, link intervention evidence and compare measured receipt intervals |
| [`agent.py`](src/argos_studio/agent.py) | Bound provider/tool calls, cancellation and persistent execution traces |
| [`agent_tools.py`](src/argos_studio/agent_tools.py) | Strict session-scoped access to existing instruments and evidence |
| [`agent_provider.py`](src/argos_studio/agent_provider.py) | Opt-in OpenAI Responses adapter with server-side credentials |
| [`app.py`](src/argos_studio/app.py) | Local FastAPI endpoints, process ownership and HTTP boundaries |
| [`argos_import.py`](src/argos_studio/argos_import.py) | Optional subprocess adapter to ARGOS's native recording reader |
| [`static/`](src/argos_studio/static/) | Browser interface, charts, timeline and replay without a build step |

The interface uses the same HTTP analysis endpoints available to other local
clients. The [OpenAPI schema](http://127.0.0.1:8765/openapi.json) describes request
and response routes. Key endpoints are:

| Endpoint | Purpose |
| --- | --- |
| `GET /api/health` | Runtime limits, optional adapters and active agent request |
| `GET /api/sessions` / `POST /api/sessions` | List sessions or start synthetic/MAVLink UDP acquisition |
| `GET /api/sessions/{id}` | Measurements, events and source status; optional `start_s` / `end_s` |
| `POST /api/sessions/{id}/annotations` | Attach an observation to a session time |
| `POST /api/sessions/{id}/dropout` / `stop` | Interrupt synthetic reception or stop the active acquisition |
| `GET /api/sessions/{id}/analysis` | Evidence for a selected reception-time window |
| `GET` / `POST /api/sessions/{id}/investigations` | List reports or investigate a window with optional context |
| `GET /api/sessions/{id}/investigations/{report_id}` | Read a saved report; append `/export` to download JSON |
| `POST /api/sessions/{id}/investigations/{report_id}/experiments` | Prepare the fixed synthetic comparison protocol |
| `GET /api/experiments` | List experiments; optional originating `session_id` filter |
| `GET /api/experiments/{experiment_id}` | Inspect proposal, status, linked captures and comparison |
| `POST /api/experiments/{experiment_id}/start` / `cancel` | Start a valid proposal once, or cancel the sequence |
| `GET /api/experiments/{experiment_id}/export` | Export a terminal experiment and its result |
| `GET` / `POST /api/sessions/{id}/agent-runs` | List requests or submit a question with optional window bounds |
| `GET /api/sessions/{id}/agent-runs/{run_id}` | Inspect answer, lifecycle and tool trace; append `/export` for terminal JSON |
| `POST /api/sessions/{id}/agent-runs/{run_id}/cancel` | Cancel further provider/tool calls and preserve the trace |
| `GET /api/sessions/{id}/export` / `raw` | Export session JSON or retrieve an imported original |
| `GET /api/sessions/{id}/capture` | Export a stopped MAVLink session's raw UDP evidence |
| `POST /api/import/argos` | Validate and import a native recording |

Writes use JSON, except the import endpoint, which accepts
`application/octet-stream`. Browser writes are restricted to the same origin.
Investigation requests accept `start_s`, `end_s` and `context` (up to 2,000
characters). Omitted bounds select the whole persisted session; explicit bounds
must be finite, ordered and within its recorded duration. Domain tools run locally;
the optional agent sends their bounded outputs to its configured provider.
Experiment preparation, start and cancellation accept only `{}`. Protocols are
defined by the server; arbitrary sources, durations or executable actions are
not accepted in these requests. Starting returns HTTP 202 and reserves two
session slots. Inspect status with GET; incompatible, expired, already-consumed
or busy launches return HTTP 409 without acquiring a new source.

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
replay, investigations, report history/export, synthetic experiment completion
and cancellation, mobile layout and connection errors without touching saved sessions.
Agent tests use explicit scripted provider or HTTP doubles: they validate tool
orchestration, the Responses protocol, persistence and interface behavior without
network access, API charges or a claim about a live model's reasoning quality.
The default browser scenario checks the unconfigured state; live provider access
must be configured separately to evaluate actual model behavior.

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
