"""Same-origin HTTP interface to the session store and isolated synthetic source."""

import asyncio
import fcntl
import json
import os
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Annotated, Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .acquisition import Acquisition
from .agent import AgentLimits, AgentRunner
from .agent_provider import AgentConfig, OpenAIProvider, Provider, ProviderError
from .argos_import import read_argos_recording
from .config import load_environment
from .core import MAX_EXPERIMENTS, MAX_INVESTIGATIONS, Store
from .experiments import PROPOSAL_TTL_S, ExperimentRunner, SyntheticProtocol
from .investigation import ALGORITHM_VERSION, investigate
from .mavlink import MAX_DATAGRAMS, MAX_RAW_BYTES, MAX_SAMPLES, dialect
from .observations import ObservationMonitor, investigate_observation

MAX_IMPORT_BYTES = 10 * 1024 * 1024
MAX_SESSIONS = 100
MAX_DURATION_S = 180
STATIC = Path(__file__).parent / "static"


@dataclass
class Settings:
    data_dir: Path = field(
        default_factory=lambda: Path(os.environ.get("ARGOS_STUDIO_DATA_DIR", ".data"))
    )
    argos_root: str | None = field(
        default_factory=lambda: os.environ.get("ARGOS_STUDIO_ARGOS_ROOT")
    )
    argos_python: str | None = field(
        default_factory=lambda: os.environ.get("ARGOS_STUDIO_ARGOS_PYTHON")
    )
    period_s: float = 0.05
    max_duration_s: float = MAX_DURATION_S
    dropout_duration_s: float = 2
    experiment_protocol: SyntheticProtocol = field(default_factory=SyntheticProtocol)
    agent_config: AgentConfig = field(default_factory=AgentConfig.from_env)
    agent_limits: AgentLimits = field(default_factory=AgentLimits)
    observation_interval_s: float = 1

    def import_available(self) -> bool:
        if not self.argos_root or not self.argos_python:
            return False
        root = Path(self.argos_root).expanduser()
        python = Path(self.argos_python).expanduser()
        return (
            root.is_absolute()
            and python.is_absolute()
            and (root / "argos/backends/mavlink/recording.py").is_file()
            and python.is_file()
            and os.access(python, os.X_OK)
        )


class StartSession(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    objective: str = Field(default="", max_length=2000)
    source: Literal["simulation", "mavlink-udp"] = "simulation"
    listen_port: int = Field(default=14580, ge=1024, le=65535, strict=True)
    system_id: int = Field(default=1, ge=1, le=255, strict=True)
    component_id: int = Field(default=1, ge=1, le=255, strict=True)


class Annotation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=2000)
    at_s: float = Field(ge=0, allow_inf_nan=False)


class Investigation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start_s: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    end_s: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    context: str = Field(default="", max_length=2000)


class EmptyAction(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AgentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt: str = Field(min_length=1, max_length=4000)
    start_s: float | None = Field(default=None, ge=0, allow_inf_nan=False, strict=True)
    end_s: float | None = Field(default=None, ge=0, allow_inf_nan=False, strict=True)
    observation_id: str | None = Field(default=None, min_length=1, max_length=80)


Bound = Annotated[float | None, Query(ge=0, allow_inf_nan=False)]


def create_app(
    settings: Settings | None = None, *, agent_provider: Provider | None = None
) -> FastAPI:
    if settings is None:
        load_environment()
        settings = Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Recovery must never mistake another running worker's source for a crashed source.
        with (settings.data_dir / "studio.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError(
                    "This data directory is already in use by ARGOS Studio."
                ) from exc
            store = Store(settings.data_dir / "studio.sqlite3")
            store.recover_interrupted()
            store.recover_experiments()
            store.recover_agent_runs()
            runtime = Acquisition(
                store,
                period_s=settings.period_s,
                max_duration_s=settings.max_duration_s,
                dropout_duration_s=settings.dropout_duration_s,
            )
            app.state.store = store
            app.state.runtime = runtime
            app.state.creation_lock = asyncio.Lock()
            runner = ExperimentRunner(
                store,
                runtime,
                app.state.creation_lock,
                protocol=settings.experiment_protocol,
                max_sessions=MAX_SESSIONS,
            )
            app.state.experiments = runner
            provider = agent_provider
            if provider is None and settings.agent_config.reason is None:
                provider = OpenAIProvider(settings.agent_config)
            agent = AgentRunner(
                store,
                runner,
                provider,
                unavailable_reason=None if provider else settings.agent_config.reason,
                limits=settings.agent_limits,
            )
            app.state.agent = agent
            monitor = ObservationMonitor(store, interval_s=settings.observation_interval_s)
            app.state.observations = monitor
            monitor.start()
            try:
                yield
            finally:
                await monitor.close()
                await agent.close()
                await runner.close()
                if runtime.active:
                    await runtime.stop(runtime.session_id, status="interrupted")
                fcntl.flock(lock, fcntl.LOCK_UN)

    app = FastAPI(
        title="ARGOS Studio", version="0.1.0", lifespan=lifespan, docs_url=None, redoc_url=None
    )
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "[::1]", "testserver"]
    )

    @app.middleware("http")
    async def local_boundary(request: Request, call_next):
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("origin")
            expected_origin = f"{request.url.scheme}://{request.headers.get('host')}"
            if origin and origin != expected_origin:
                return JSONResponse(
                    {"detail": "Cross-origin writes are disabled."}, status_code=403
                )
            expected_type = (
                "application/octet-stream"
                if request.url.path == "/api/import/argos"
                else "application/json"
            )
            if request.headers.get("content-type", "").split(";")[0] != expected_type:
                return JSONResponse(
                    {"detail": f"Content-Type must be {expected_type}."}, status_code=415
                )
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; "
            "form-action 'self'"
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(KeyError)
    async def missing_session(request: Request, exc: KeyError):
        return JSONResponse({"detail": "Session introuvable."}, status_code=404)

    @app.exception_handler(ValueError)
    async def invalid_operation(request: Request, exc: ValueError):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request: Request, exc: RequestValidationError):
        # Do not echo NaN/Infinity (or arbitrary submitted data) into a JSON
        # error response: those values cannot be serialized as standard JSON.
        errors = [{key: error[key] for key in ("loc", "msg", "type")} for error in exc.errors()]
        return JSONResponse({"detail": "Paramètres invalides.", "errors": errors}, status_code=422)

    def capacity() -> None:
        if len(app.state.store.list_sessions()) >= MAX_SESSIONS:
            raise HTTPException(
                409, "Limite de 100 sessions atteinte. Utilisez un autre dossier de données."
            )

    def available_acquisition() -> None:
        if app.state.experiments.active:
            raise HTTPException(409, "Terminez ou annulez l’essai synthétique en cours.")

    @app.get("/api/health")
    def health():
        available = settings.import_available()
        return {
            "status": "ok",
            "version": "0.1.0",
            "argos_import": {
                "available": available,
                "reason": (
                    "Lecteur natif ARGOS configuré."
                    if available
                    else "Configurer ARGOS_STUDIO_ARGOS_ROOT et "
                    "ARGOS_STUDIO_ARGOS_PYTHON au lancement."
                ),
            },
            "mavlink": {
                "available": not bool(dialect.MAVLINK_IGNORE_CRC),
                "listen_host": "127.0.0.1",
                "default_port": 14580,
                "read_only": True,
                "max_raw_bytes": MAX_RAW_BYTES,
                "max_datagrams": MAX_DATAGRAMS,
                "max_samples": MAX_SAMPLES,
            },
            "limits": {
                "max_duration_s": settings.max_duration_s,
                "max_import_bytes": MAX_IMPORT_BYTES,
                "max_sessions": MAX_SESSIONS,
                "max_investigations_per_session": MAX_INVESTIGATIONS,
                "max_experiments_per_session": MAX_EXPERIMENTS,
            },
            "investigation": {"algorithm_version": ALGORITHM_VERSION, "uses_llm": False},
            "agent": app.state.agent.health(),
            "observation": app.state.observations.health(),
            "experiment": {
                "active_id": app.state.experiments.experiment_id
                if app.state.experiments.active
                else None,
                "profile": asdict(settings.experiment_protocol),
                "proposal_ttl_s": PROPOSAL_TTL_S,
            },
        }

    @app.get("/api/sessions")
    def list_sessions():
        return app.state.store.list_sessions()

    @app.post("/api/sessions", status_code=201)
    async def start_session(body: StartSession):
        async with app.state.creation_lock:
            available_acquisition()
            capacity()
            return await app.state.runtime.start(**body.model_dump())

    @app.get("/api/sessions/{session_id}")
    def session_snapshot(session_id: str, start_s: Bound = None, end_s: Bound = None):
        return {
            **app.state.store.snapshot(session_id, start_s=start_s, end_s=end_s),
            "live": app.state.runtime.live_state(session_id),
        }

    @app.post("/api/sessions/{session_id}/annotations", status_code=201)
    def annotate(session_id: str, body: Annotation):
        session = app.state.store.get_session(session_id)
        if session["status"] == "live":
            live = app.state.runtime.live_state(session_id)
            if live["elapsed_s"] is None or body.at_s > live["elapsed_s"]:
                raise ValueError("Une annotation ne peut pas être placée dans le futur.")
        return app.state.store.annotate(session_id, body.text, body.at_s)

    @app.post("/api/sessions/{session_id}/stop")
    async def stop(session_id: str):
        if app.state.experiments.owns(session_id):
            await app.state.experiments.cancel(app.state.experiments.experiment_id)
            return app.state.store.get_session(session_id)
        return await app.state.runtime.stop(session_id)

    @app.post("/api/sessions/{session_id}/dropout")
    async def dropout(session_id: str):
        if app.state.experiments.owns(session_id):
            raise HTTPException(409, "L’interruption est pilotée par le protocole de cet essai.")
        return await app.state.runtime.dropout(session_id)

    @app.get("/api/sessions/{session_id}/analysis")
    def analyze(session_id: str, start_s: Bound = None, end_s: Bound = None):
        return app.state.store.analyze(session_id, start_s=start_s, end_s=end_s)

    @app.post("/api/sessions/{session_id}/agent-runs", status_code=202)
    async def start_agent_run(session_id: str, body: AgentRequest):
        try:
            return app.state.agent.start(session_id, **body.model_dump())
        except ProviderError as exc:
            raise HTTPException(503, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/sessions/{session_id}/observations")
    def list_observations(session_id: str):
        return {
            **app.state.store.list_observations(session_id),
            "monitor": app.state.observations.health(),
        }

    @app.get("/api/sessions/{session_id}/observations/{observation_id}")
    def get_observation(session_id: str, observation_id: str):
        return app.state.store.get_observation(session_id, observation_id)

    @app.post("/api/sessions/{session_id}/observations/{observation_id}/investigate")
    def inspect_observation(session_id: str, observation_id: str, body: EmptyAction):
        return investigate_observation(app.state.store, session_id, observation_id)

    @app.post("/api/sessions/{session_id}/observations/{observation_id}/dismiss")
    def dismiss_observation(session_id: str, observation_id: str, body: EmptyAction):
        return app.state.store.set_observation_disposition(session_id, observation_id, "dismissed")

    @app.post("/api/sessions/{session_id}/observations/{observation_id}/reopen")
    def reopen_observation(session_id: str, observation_id: str, body: EmptyAction):
        return app.state.store.set_observation_disposition(session_id, observation_id, "open")

    @app.get("/api/sessions/{session_id}/agent-runs")
    def list_agent_runs(session_id: str):
        return app.state.store.list_agent_runs(session_id)

    @app.get("/api/sessions/{session_id}/agent-runs/{run_id}")
    def get_agent_run(session_id: str, run_id: str):
        return app.state.store.get_agent_run(session_id, run_id)

    @app.post("/api/sessions/{session_id}/agent-runs/{run_id}/cancel")
    async def cancel_agent_run(session_id: str, run_id: str, body: EmptyAction):
        try:
            return await app.state.agent.cancel(session_id, run_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/sessions/{session_id}/agent-runs/{run_id}/export")
    def export_agent_run(session_id: str, run_id: str):
        run = app.state.store.get_agent_run(session_id, run_id)
        if run["status"] == "running":
            raise HTTPException(409, "Terminez ou annulez la demande avant d’exporter sa trace.")
        return Response(
            json.dumps(run, ensure_ascii=False, allow_nan=False),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="argos-agent-{run["id"]}.json"'},
        )

    @app.post("/api/sessions/{session_id}/investigations", status_code=201)
    def run_investigation(session_id: str, body: Investigation):
        return investigate(app.state.store, session_id, **body.model_dump())

    @app.get("/api/sessions/{session_id}/investigations")
    def list_investigations(session_id: str):
        return app.state.store.list_investigations(session_id)

    @app.get("/api/sessions/{session_id}/investigations/{investigation_id}")
    def get_investigation(session_id: str, investigation_id: str):
        return app.state.store.get_investigation(session_id, investigation_id)

    @app.get("/api/sessions/{session_id}/investigations/{investigation_id}/export")
    def export_investigation(session_id: str, investigation_id: str):
        report = app.state.store.get_investigation(session_id, investigation_id)
        filename = f"argos-investigation-{report['id']}.json"
        return Response(
            json.dumps(report, ensure_ascii=False, allow_nan=False),
            media_type="application/json",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
            },
        )

    @app.post(
        "/api/sessions/{session_id}/investigations/{investigation_id}/experiments", status_code=201
    )
    def propose_experiment(session_id: str, investigation_id: str, body: EmptyAction):
        return app.state.experiments.propose(session_id, investigation_id)

    @app.get("/api/experiments")
    def list_experiments(session_id: str | None = None):
        return app.state.store.list_experiments(session_id)

    @app.get("/api/experiments/{experiment_id}")
    def get_experiment(experiment_id: str):
        return app.state.store.get_experiment(experiment_id)

    @app.post("/api/experiments/{experiment_id}/start", status_code=202)
    async def start_experiment(experiment_id: str, body: EmptyAction):
        try:
            return await app.state.experiments.start(experiment_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/experiments/{experiment_id}/cancel")
    async def cancel_experiment(experiment_id: str, body: EmptyAction):
        return await app.state.experiments.cancel(experiment_id)

    @app.get("/api/experiments/{experiment_id}/export")
    def export_experiment(experiment_id: str):
        experiment = app.state.store.get_experiment(experiment_id)
        if experiment["status"] in {"proposed", "running"}:
            raise HTTPException(409, "Terminez ou annulez l’essai avant d’exporter son résultat.")
        filename = f"argos-experiment-{experiment['id']}.json"
        return Response(
            json.dumps(experiment, ensure_ascii=False, allow_nan=False),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.get("/api/sessions/{session_id}/export")
    def export_session(session_id: str):
        payload = {
            "schema_version": 1,
            **app.state.store.snapshot(session_id),
        }
        return Response(
            json.dumps(payload, ensure_ascii=False, allow_nan=False),
            media_type="application/json",
            headers={
                "Content-Disposition": f'attachment; filename="argos-studio-{session_id}.json"',
            },
        )

    @app.get("/api/sessions/{session_id}/raw")
    def raw_recording(session_id: str):
        session = app.state.store.get_session(session_id)
        path = settings.data_dir / "recordings" / f"{session['id']}.jsonl"
        if session["source"] != "argos-recording" or not path.is_file():
            raise HTTPException(
                404, "Cette session ne possède pas d’enregistrement ARGOS original."
            )
        return FileResponse(
            path, media_type="application/octet-stream", filename=f"{session['id']}.jsonl"
        )

    @app.get("/api/sessions/{session_id}/capture")
    def export_capture(session_id: str):
        session = app.state.store.get_session(session_id)
        if session["source"] != "mavlink-udp":
            raise HTTPException(404, "Cette session ne possède pas de capture UDP.")
        if session["status"] == "live":
            raise HTTPException(409, "Arrêtez la réception avant d’exporter la capture brute.")
        payload = {
            "schema_version": 1,
            "session": session,
            "capture": app.state.store.capture_summary(session_id),
            "datagrams": app.state.store.datagrams(session_id),
        }
        return Response(
            json.dumps(payload, ensure_ascii=False, allow_nan=False),
            media_type="application/json",
            headers={
                "Content-Disposition": f'attachment; filename="argos-capture-{session_id}.json"',
            },
        )

    @app.post("/api/import/argos", status_code=201)
    async def import_argos(request: Request, filename: str = "ARGOS recording"):
        if not settings.import_available():
            raise HTTPException(503, "Le lecteur natif ARGOS n’est pas configuré.")
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > MAX_IMPORT_BYTES:
                raise HTTPException(413, "Enregistrement trop volumineux (maximum 10 Mio).")
        async with app.state.creation_lock:
            available_acquisition()
            capacity()
            if app.state.runtime.active:
                raise HTTPException(
                    409, "Arrêtez l’acquisition avant d’importer un enregistrement."
                )
            result = await asyncio.to_thread(
                read_argos_recording,
                bytes(data),
                python_executable=settings.argos_python,
                argos_root=settings.argos_root,
            )
            name = filename.replace("\\", "/").rsplit("/", 1)[-1].strip()[:120] or "ARGOS recording"

            def persist():
                session = app.state.store.import_session(
                    name,
                    "Inspection d’un enregistrement ARGOS",
                    result["samples"],
                    metadata=result["metadata"],
                    duration_s=result["duration_s"],
                )
                recordings = settings.data_dir / "recordings"
                recordings.mkdir(exist_ok=True)
                try:
                    (recordings / f"{session['id']}.jsonl").write_bytes(data)
                except OSError:
                    app.state.store.add_event(
                        session["id"],
                        "raw_save_failed",
                        "Échec de la copie du fichier original ; conserver le fichier source.",
                        0,
                    )
                    raise HTTPException(
                        507, "Session importée, mais la copie du fichier original a échoué."
                    ) from None
                app.state.store.add_event(
                    session["id"], "imported", "Enregistrement validé par le lecteur ARGOS.", 0
                )
                return app.state.store.get_session(session["id"])

            return await asyncio.to_thread(persist)

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
