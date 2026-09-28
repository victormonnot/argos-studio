"""An explicit, bounded executor for one synthetic control/perturbation protocol."""

import asyncio
import logging
from contextlib import suppress
from dataclasses import asdict, dataclass

from .acquisition import Acquisition
from .comparison import compare_experiment
from .core import Store, _number
from .simulator import Simulator

PROPOSAL_TTL_S = 600
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class SyntheticProtocol:
    """Server-owned parameters; the HTTP API does not accept executable plans."""

    kind: str = "synthetic_dropout_comparison"
    version: str = "synthetic-dropout/1"
    source: str = "simulation"
    sample_rate_hz: float = 20
    phase_duration_s: float = 6
    dropout_at_s: float = 2
    dropout_duration_s: float = 2
    gap_threshold_s: float = 0.25
    timing_tolerance_s: float = 0.25
    max_wall_duration_s: float = 20

    def __post_init__(self):
        for key, value in asdict(self).items():
            if key not in {"kind", "version", "source"}:
                if _number(value, key, minimum=0) == 0:
                    raise ValueError(f"{key} must be positive")
        if (self.kind, self.version, self.source) != (
            "synthetic_dropout_comparison",
            "synthetic-dropout/1",
            "simulation",
        ):
            raise ValueError("Unknown synthetic protocol")
        if self.gap_threshold_s != 0.25:
            raise ValueError("Protocol must use the shared continuity threshold")
        if not 5 <= self.sample_rate_hz <= 100:
            raise ValueError("Unsupported synthetic cadence")
        if self.dropout_duration_s <= self.gap_threshold_s:
            raise ValueError("Interruption must exceed the inspection threshold")
        if self.dropout_at_s < 2 / self.sample_rate_hz:
            raise ValueError("Insufficient time to observe the pre-interruption signal")
        if (
            self.dropout_at_s + self.dropout_duration_s + 2 / self.sample_rate_hz
            >= self.phase_duration_s
        ):
            raise ValueError("Insufficient time to observe resumption")
        if not 2 * self.phase_duration_s < self.max_wall_duration_s <= 30:
            raise ValueError("Protocol needs a bounded deadline longer than both captures")


class ExperimentRunner:
    def __init__(
        self,
        store: Store,
        runtime: Acquisition,
        creation_lock: asyncio.Lock,
        *,
        protocol: SyntheticProtocol | None = None,
        max_sessions: int = 100,
    ):
        self.store = store
        self.runtime = runtime
        self.creation_lock = creation_lock
        self.protocol = protocol or SyntheticProtocol()
        self.max_sessions = max_sessions
        self.source = Simulator(
            store,
            period_s=1 / self.protocol.sample_rate_hz,
            max_duration_s=self.protocol.phase_duration_s,
            dropout_duration_s=self.protocol.dropout_duration_s,
        )
        runtime.experiment_source = self.source
        self.task: asyncio.Task | None = None
        self.experiment_id: str | None = None
        self.cancel_status = "cancelled"

    @property
    def active(self) -> bool:
        return self.task is not None and not self.task.done()

    def propose(self, session_id: str, investigation_id: str) -> dict:
        report = self.store.get_investigation(session_id, investigation_id)
        if report["kind"] != "reception_quality":
            raise ValueError("Ce protocole nécessite une investigation de réception.")
        return self.store.create_experiment(
            session_id, investigation_id, asdict(self.protocol), ttl_s=PROPOSAL_TTL_S
        )

    async def start(self, experiment_id: str) -> dict:
        async with self.creation_lock:
            proposal = self.store.get_experiment(experiment_id)
            if proposal["plan"] != asdict(self.protocol):
                raise ValueError("Le protocole a changé. Préparez une nouvelle proposition.")
            if self.active or self.runtime.active:
                raise ValueError(
                    "Arrêtez l’acquisition ou l’essai en cours avant de lancer celui-ci."
                )
            claimed = self.store.claim_experiment(experiment_id, max_sessions=self.max_sessions)
            self.experiment_id = experiment_id
            self.cancel_status = "cancelled"
            # A proposal is consumed once before scheduling any source. Neither
            # HTTP retries nor recovery can schedule these captures a second time.
            self.task = asyncio.create_task(self._run(experiment_id))
            return claimed

    async def _phase(self, experiment_id: str, role: str) -> str:
        label = "Témoin" if role == "control" else "Interruption"
        session = await self.source.start(
            f"Essai synthétique · {label} · {experiment_id[:8]}",
            "Comparer la continuité de réception avec et sans suspension du générateur.",
            experiment_id=experiment_id,
            experiment_role=role,
        )
        if role == "perturbed":
            await asyncio.sleep(self.protocol.dropout_at_s)
            if not self.source.active:
                await self.source.task
                raise RuntimeError("La source s’est arrêtée avant l’intervention prévue.")
            await self.source.dropout(session["id"])
        # The generator has its own duration bound even if orchestration fails.
        await asyncio.shield(self.source.task)
        if self.store.get_session(session["id"])["status"] != "completed":
            raise RuntimeError("La capture ne s’est pas terminée normalement.")
        return session["id"]

    async def _stop_source(self):
        session_id = self.source.session_id
        if session_id is None:
            return
        if self.source.active:
            await self.source.stop(session_id, status="interrupted")
        elif self.store.get_session(session_id)["status"] == "live":
            # Handle failure between the atomic session/link insert and task
            # creation without leaving an orphan live session.
            self.store.finish_session(session_id, status="interrupted")

    async def _run(self, experiment_id: str):
        status, result, error = "failed", None, None
        try:
            async with asyncio.timeout(self.protocol.max_wall_duration_s):
                control = await self._phase(experiment_id, "control")
                perturbed = await self._phase(experiment_id, "perturbed")
                experiment = self.store.get_experiment(experiment_id)
                reference = self.store.get_investigation(
                    experiment["origin_session_id"], experiment["investigation_id"]
                )
                result = compare_experiment(
                    self.store.snapshot(control),
                    self.store.snapshot(perturbed),
                    reference,
                    experiment["plan"],
                )
                status = "completed"
        except asyncio.CancelledError:
            status = self.cancel_status
            error = "Essai arrêté ; les captures partielles restent consultables."
        except TimeoutError:
            error = "Délai maximal de l’essai dépassé ; acquisition arrêtée."
        except Exception:
            LOGGER.exception("Synthetic experiment %s failed", experiment_id)
            error = "Échec de l’essai synthétique ; examiner les captures conservées."
        finally:
            try:
                await self._stop_source()
            except Exception:
                LOGGER.exception("Synthetic experiment %s cleanup failed", experiment_id)
                status, result = "failed", None
                error = "Échec de l’arrêt de l’essai ; examiner les captures conservées."
            finally:
                self.store.finish_experiment(experiment_id, status, result=result, error=error)

    async def cancel(self, experiment_id: str, *, status: str = "cancelled") -> dict:
        async with self.creation_lock:
            experiment = self.store.get_experiment(experiment_id)
            if experiment["status"] == "proposed":
                return self.store.finish_experiment(experiment_id, "cancelled")
            if experiment["status"] != "running":
                return experiment
            if experiment_id != self.experiment_id or not self.active:
                raise ValueError("Cet essai n’est pas exécuté par ce processus.")
            self.cancel_status = status
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
            # Cancellation may occur before the coroutine enters try/finally.
            await self._stop_source()
            return self.store.finish_experiment(
                experiment_id,
                status,
                error="Essai arrêté ; les captures partielles restent consultables.",
            )

    async def close(self):
        if self.active:
            await self.cancel(self.experiment_id, status="interrupted")

    def owns(self, session_id: str) -> bool:
        if not self.active:
            return False
        experiment = self.store.get_experiment(self.experiment_id)
        return session_id in (experiment["control_session_id"], experiment["perturbed_session_id"])
