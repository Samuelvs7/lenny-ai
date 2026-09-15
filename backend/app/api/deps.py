"""Application state and FastAPI dependencies.

A single :class:`AppState` is built at startup and attached to the app. Routes
depend on it rather than reaching for module-level globals, which keeps the
wiring visible and lets tests substitute a state object with fakes.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncEngine

from app.agent.orchestrator import Agent
from app.config import Settings
from app.db.repositories import ArtifactRepository, SessionRepository
from app.providers.registry import ProviderRegistry
from app.retrieval.hybrid import HybridRetriever
from app.retrieval.index import DenseIndex


@dataclass(slots=True)
class AppState:
    settings: Settings
    engine: AsyncEngine
    providers: ProviderRegistry
    dense_index: DenseIndex
    retriever: HybridRetriever
    sessions: SessionRepository
    artifacts: ArtifactRepository
    agent: Agent
    #: Set when startup could not fully initialise (e.g. database unreachable).
    #: The app still serves /health so an operator can see *why* it is unwell,
    #: instead of a container that crash-loops with the reason in lost logs.
    startup_error: str | None = None


def build_state(settings: Settings, engine: AsyncEngine) -> AppState:
    providers = ProviderRegistry(settings)
    dense_index = DenseIndex()
    retriever = HybridRetriever(
        engine=engine,
        settings=settings,
        dense_index=dense_index,
        embedder=providers.get_embedder(),
    )
    sessions = SessionRepository(engine)
    artifacts = ArtifactRepository(engine)
    agent = Agent(
        settings=settings,
        sessions=sessions,
        artifacts=artifacts,
        retriever=retriever,
        providers=providers,
    )
    return AppState(
        settings=settings,
        engine=engine,
        providers=providers,
        dense_index=dense_index,
        retriever=retriever,
        sessions=sessions,
        artifacts=artifacts,
        agent=agent,
    )


def get_state(request: Request) -> AppState:
    return request.app.state.app_state
