from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentrt.agent_server.bash_router import bash_router


def test_bash_search_rejects_limit_above_100():
    app = FastAPI()
    app.state.bash_event_service = object()
    app.include_router(bash_router, prefix="/api")

    response = TestClient(app).get(
        "/api/bash/bash_events/search", params={"limit": 101}
    )

    assert response.status_code == 422
