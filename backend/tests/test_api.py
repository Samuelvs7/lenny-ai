"""API contracts and persistence, against a real PostgreSQL.

Skipped with a clear reason when ``TEST_DATABASE_URL`` is unset — see
``conftest.py``. The model provider is always faked: these tests assert the
*application's* behaviour (validation, isolation, persistence, error shape),
not a language model's prose.
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from tests.conftest import TEST_DATABASE_URL, FailingLLM, FakeLLM, requires_db

pytestmark = requires_db


@pytest.fixture
async def app_client(monkeypatch):
    """Boot the real app against the test database with a fake model."""
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    monkeypatch.setenv("MODEL_PROVIDER", "ollama")

    from app.config import get_settings

    get_settings.cache_clear()

    import app.db.engine as engine_module

    engine_module._engine = None  # noqa: SLF001 - fresh engine for the test DB

    from app.main import create_app

    application = create_app()

    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        async with application.router.lifespan_context(application):
            state = application.state.app_state

            # Swap in a scripted model so no Ollama call is made.
            fake = FakeLLM(replies=["A grounded answer about growth [S1]."])
            state.providers.get_llm = lambda name=None: fake  # type: ignore[method-assign]
            state.agent._providers = state.providers  # noqa: SLF001
            client.fake_llm = fake  # type: ignore[attr-defined]

            await _truncate(state.engine)
            await _seed_knowledge(state.engine)
            yield client

    get_settings.cache_clear()
    engine_module._engine = None  # noqa: SLF001


async def _truncate(engine) -> None:
    """Start each test from a clean conversation store."""
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE artifacts, messages, sessions CASCADE"))


async def _seed_knowledge(engine) -> None:
    """Insert a tiny fixture corpus.

    Chunks are seeded **without embeddings** on purpose: the dense arm then
    stays empty and the tests need no Ollama, while Postgres full-text search
    still matches. That exercises the real grounding path — including the
    conjunctive `strong_lexical_match` signal — on any machine.
    """
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE episodes CASCADE"))
        episode_id = (
            await conn.execute(
                text(
                    """
                    INSERT INTO episodes (slug, guest, title, youtube_url, content_hash)
                    VALUES ('test-guest', 'Test Guest', 'A test episode',
                            'https://www.youtube.com/watch?v=test123', 'hash-1')
                    RETURNING id
                    """
                )
            )
        ).scalar_one()

        passages = [
            "Growth for early stage startups comes from a narrow segment and a "
            "repeatable loop, not from broad paid acquisition spend.",
            "Pricing strategy should follow the value a customer receives, and "
            "most teams price far too low when they first launch a product.",
            "Hiring engineers well means optimising for slope over intercept and "
            "writing the role scorecard before the first interview.",
            "Onboarding is the highest leverage growth surface because activation "
            "compounds into retention for every cohort that follows.",
        ]
        for index, content in enumerate(passages):
            await conn.execute(
                text(
                    """
                    INSERT INTO chunks (episode_id, chunk_index, content, speaker,
                                        start_seconds, word_count)
                    VALUES (:episode_id, :index, :content, 'Test Guest', :start, :words)
                    """
                ),
                {
                    "episode_id": episode_id,
                    "index": index,
                    "content": content,
                    "start": index * 60,
                    "words": len(content.split()),
                },
            )


class TestHealth:
    async def test_liveness_is_shallow_and_always_ok(self, app_client: AsyncClient):
        response = await app_client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    async def test_readiness_reports_each_component(self, app_client: AsyncClient):
        response = await app_client.get("/health/deep")
        assert response.status_code == 200
        body = response.json()
        names = {component["name"] for component in body["components"]}
        assert "database" in names
        assert "knowledge_base" in names
        assert any(name.startswith("provider:") for name in names)

    async def test_every_response_carries_a_request_id(self, app_client: AsyncClient):
        response = await app_client.get("/health")
        assert response.headers.get("X-Request-ID")

    async def test_echoes_a_caller_supplied_request_id(self, app_client: AsyncClient):
        response = await app_client.get("/health", headers={"X-Request-ID": "trace-me-123"})
        assert response.headers["X-Request-ID"] == "trace-me-123"


class TestModelsEndpoint:
    async def test_lists_providers_and_the_active_one(self, app_client: AsyncClient):
        body = (await app_client.get("/api/models")).json()
        assert body["active_provider"] == "ollama"
        assert body["agent_runner"] in {"native", "claude_agent_sdk"}
        names = {provider["name"] for provider in body["providers"]}
        assert {"ollama", "anthropic"} <= names

    async def test_unconfigured_provider_explains_itself(self, app_client: AsyncClient):
        """A missing key must be a readable reason, never a crash."""
        body = (await app_client.get("/api/models")).json()
        anthropic = next(p for p in body["providers"] if p["name"] == "anthropic")
        if not anthropic["available"]:
            assert anthropic["reason"]
            assert "ANTHROPIC_API_KEY" in anthropic["reason"]


class TestSessions:
    async def test_create_and_fetch(self, app_client: AsyncClient):
        created = await app_client.post("/api/sessions", json={})
        assert created.status_code == 201
        session_id = created.json()["id"]

        fetched = await app_client.get(f"/api/sessions/{session_id}")
        assert fetched.status_code == 200
        assert fetched.json()["messages"] == []

    async def test_unknown_session_is_404_not_an_empty_conversation(
        self, app_client: AsyncClient
    ):
        response = await app_client.get(f"/api/sessions/{uuid4()}")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"

    async def test_malformed_uuid_is_a_validation_error(self, app_client: AsyncClient):
        response = await app_client.get("/api/sessions/not-a-uuid")
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_request"

    async def test_rename_and_delete(self, app_client: AsyncClient):
        session_id = (await app_client.post("/api/sessions", json={})).json()["id"]

        assert (
            await app_client.patch(
                f"/api/sessions/{session_id}", json={"title": "Pricing research"}
            )
        ).status_code == 204
        assert (await app_client.get(f"/api/sessions/{session_id}")).json()["title"] == (
            "Pricing research"
        )

        assert (await app_client.delete(f"/api/sessions/{session_id}")).status_code == 204
        assert (await app_client.get(f"/api/sessions/{session_id}")).status_code == 404

    async def test_listing_orders_by_recent_activity(self, app_client: AsyncClient):
        first = (await app_client.post("/api/sessions", json={})).json()["id"]
        second = (await app_client.post("/api/sessions", json={})).json()["id"]

        await app_client.post(f"/api/sessions/{first}/chat", json={"message": "hello there"})

        listed = (await app_client.get("/api/sessions")).json()
        assert listed[0]["id"] == first, "the session just used must sort first"
        assert {item["id"] for item in listed} == {first, second}


class TestChatValidation:
    async def test_rejects_an_empty_message(self, app_client: AsyncClient):
        session_id = (await app_client.post("/api/sessions", json={})).json()["id"]
        response = await app_client.post(f"/api/sessions/{session_id}/chat", json={"message": ""})
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_request"

    async def test_rejects_whitespace_only(self, app_client: AsyncClient):
        session_id = (await app_client.post("/api/sessions", json={})).json()["id"]
        response = await app_client.post(
            f"/api/sessions/{session_id}/chat", json={"message": "   \n\t  "}
        )
        assert response.status_code == 400

    async def test_rejects_an_oversized_message(self, app_client: AsyncClient):
        session_id = (await app_client.post("/api/sessions", json={})).json()["id"]
        response = await app_client.post(
            f"/api/sessions/{session_id}/chat", json={"message": "x" * 5000}
        )
        assert response.status_code == 400

    async def test_chatting_to_an_unknown_session_is_404(self, app_client: AsyncClient):
        response = await app_client.post(
            f"/api/sessions/{uuid4()}/chat", json={"message": "hello"}
        )
        assert response.status_code == 404

    async def test_error_body_has_the_documented_shape(self, app_client: AsyncClient):
        response = await app_client.get(f"/api/sessions/{uuid4()}")
        error = response.json()["error"]
        assert set(error) >= {"code", "message", "retryable", "request_id"}
        assert isinstance(error["retryable"], bool)


class TestPersistenceAndIsolation:
    async def test_messages_persist_across_requests(self, app_client: AsyncClient):
        session_id = (await app_client.post("/api/sessions", json={})).json()["id"]
        await app_client.post(
            f"/api/sessions/{session_id}/chat",
            json={"message": "What did guests say about growth?"},
        )

        messages = (await app_client.get(f"/api/sessions/{session_id}")).json()["messages"]
        assert len(messages) == 2
        assert messages[0]["role"] == "user"
        assert messages[1]["role"] == "assistant"
        assert messages[1]["model_provider"] == "fake"
        assert messages[1]["skill"]

    async def test_sessions_keep_independent_context(self, app_client: AsyncClient):
        """The core session requirement: one chat never sees another's history."""
        first = (await app_client.post("/api/sessions", json={})).json()["id"]
        second = (await app_client.post("/api/sessions", json={})).json()["id"]

        await app_client.post(
            f"/api/sessions/{first}/chat", json={"message": "Tell me about pricing strategy"}
        )
        await app_client.post(
            f"/api/sessions/{second}/chat", json={"message": "Tell me about hiring engineers"}
        )

        first_messages = (await app_client.get(f"/api/sessions/{first}")).json()["messages"]
        second_messages = (await app_client.get(f"/api/sessions/{second}")).json()["messages"]

        assert len(first_messages) == 2
        assert len(second_messages) == 2
        assert "pricing" in first_messages[0]["content"]
        assert "hiring" in second_messages[0]["content"]

        # The real isolation property: neither session's *user* turns appear in
        # the other. (Assistant copy is not a valid probe — the refusal message
        # legitimately lists several topics by name.)
        first_user = [m["content"] for m in first_messages if m["role"] == "user"]
        second_user = [m["content"] for m in second_messages if m["role"] == "user"]
        assert first_user == ["Tell me about pricing strategy"]
        assert second_user == ["Tell me about hiring engineers"]

    async def test_history_from_the_same_session_reaches_the_model(
        self, app_client: AsyncClient
    ):
        """Follow-up questions need prior turns in the prompt."""
        session_id = (await app_client.post("/api/sessions", json={})).json()["id"]
        await app_client.post(
            f"/api/sessions/{session_id}/chat", json={"message": "Tell me about onboarding"}
        )
        await app_client.post(
            f"/api/sessions/{session_id}/chat", json={"message": "What about for B2B?"}
        )

        fake: FakeLLM = app_client.fake_llm  # type: ignore[attr-defined]
        last_prompt = "\n".join(message.content for message in fake.calls[-1])
        assert "onboarding" in last_prompt, "prior turn must be in the follow-up prompt"

    async def test_title_is_derived_from_the_first_message(self, app_client: AsyncClient):
        session_id = (await app_client.post("/api/sessions", json={})).json()["id"]
        await app_client.post(
            f"/api/sessions/{session_id}/chat",
            json={"message": "How should I price a B2B SaaS product?"},
        )
        title = (await app_client.get(f"/api/sessions/{session_id}")).json()["title"]
        assert title != "New chat"
        assert "price" in title.lower()

    async def test_deleting_a_session_cascades(self, app_client: AsyncClient):
        session_id = (await app_client.post("/api/sessions", json={})).json()["id"]
        await app_client.post(f"/api/sessions/{session_id}/chat", json={"message": "hello there"})
        await app_client.delete(f"/api/sessions/{session_id}")

        from app.config import get_settings
        from app.db.engine import get_engine

        async with get_engine(get_settings()).connect() as conn:
            remaining = (
                await conn.execute(
                    text("SELECT COUNT(*) FROM messages WHERE session_id = :id"),
                    {"id": session_id},
                )
            ).scalar_one()
        assert remaining == 0


class TestProviderFailureSurface:
    async def test_model_failure_becomes_a_structured_error(self, app_client: AsyncClient):
        from app.errors import ProviderUnavailableError

        session_id = (await app_client.post("/api/sessions", json={})).json()["id"]

        import app.main  # noqa: F401  (app already built)

        state = app_client._transport.app.state.app_state  # type: ignore[attr-defined]
        state.providers.get_llm = lambda name=None: FailingLLM(  # type: ignore[method-assign]
            ProviderUnavailableError("Ollama is not running")
        )

        response = await app_client.post(
            f"/api/sessions/{session_id}/chat",
            json={"message": "What about growth for early stage startups?"},
        )

        assert response.status_code == 503
        error = response.json()["error"]
        assert error["code"] == "provider_unavailable"
        assert error["retryable"] is True
        assert "request_id" in error

    async def test_error_messages_never_leak_the_connection_string(
        self, app_client: AsyncClient
    ):
        """A password in an error body would be a real incident."""
        response = await app_client.get(f"/api/sessions/{uuid4()}")
        body = response.text
        assert "postgresql" not in body.lower()
        assert os.getenv("TEST_DATABASE_URL", "no-url") not in body


class TestKnowledgeBaseEndpoint:
    async def test_reports_index_state(self, app_client: AsyncClient):
        body = (await app_client.get("/api/knowledge-base")).json()
        assert set(body) >= {
            "episodes",
            "chunks",
            "chunks_with_embeddings",
            "sponsor_segments_removed",
            "dense_index_size",
        }
        assert isinstance(body["episodes"], int)
