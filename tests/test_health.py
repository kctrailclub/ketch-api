"""GET /health exposes the running build SHA so the deploy-verify workflow can compare it to origin/main
(Deploy Sign-off Checks 1 + 2; Operating Model v2 Phase 1)."""


def test_health_reports_sha_from_railway_env(client, monkeypatch):
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "abc123def456")
    res = client.get("/health")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    assert body["sha"] == "abc123def456"
    assert body["environment"]


def test_health_sha_unknown_when_env_missing(client, monkeypatch):
    monkeypatch.delenv("RAILWAY_GIT_COMMIT_SHA", raising=False)
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json()["sha"] == "unknown"
