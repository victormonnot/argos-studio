"""Route session operations to the owned source without adding a command interface."""

from .core import Store
from .mavlink import MavlinkReceiver
from .simulator import Simulator


class Acquisition:
    def __init__(
        self, store: Store, *, period_s: float, max_duration_s: float, dropout_duration_s: float
    ):
        self.simulator = Simulator(
            store,
            period_s=period_s,
            max_duration_s=max_duration_s,
            dropout_duration_s=dropout_duration_s,
        )
        self.receiver = MavlinkReceiver(store, max_duration_s=max_duration_s)
        self.experiment_source: Simulator | None = None

    @property
    def sources(self):
        return tuple(
            source
            for source in (self.simulator, self.receiver, self.experiment_source)
            if source is not None
        )

    @property
    def active(self) -> bool:
        return any(source.active for source in self.sources)

    @property
    def session_id(self) -> str | None:
        for source in self.sources:
            if source.active:
                return source.session_id
        return None

    async def start(
        self,
        name: str,
        objective: str,
        *,
        source: str = "simulation",
        listen_port: int = 14580,
        system_id: int = 1,
        component_id: int = 1,
    ) -> dict:
        if self.active:
            raise ValueError("Une acquisition est déjà en cours.")
        if source == "simulation":
            return await self.simulator.start(name, objective)
        if source == "mavlink-udp":
            return await self.receiver.start(
                name,
                objective,
                listen_port=listen_port,
                system_id=system_id,
                component_id=component_id,
            )
        raise ValueError("Source non prise en charge.")

    def owner(self, session_id: str):
        return next(
            (source for source in self.sources if source.session_id == session_id), self.simulator
        )

    async def stop(self, session_id: str, *, status: str = "completed") -> dict:
        return await self.owner(session_id).stop(session_id, status=status)

    async def dropout(self, session_id: str) -> dict:
        return await self.simulator.dropout(session_id)

    def live_state(self, session_id: str) -> dict:
        return self.owner(session_id).live_state(session_id)
