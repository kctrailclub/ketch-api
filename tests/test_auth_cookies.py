from app.models.models import RefreshToken
from app.routers.auth import REFRESH_COOKIE_NAME, REFRESH_COOKIE_PATH


LOGIN_BODY = {"email": "test@example.com", "password": "password123"}


def _login(client):
    return client.post("/auth/login", json=LOGIN_BODY)


class TestLoginIssuesCookie:
    def test_response_body_has_only_access_token(self, client, test_user):
        res = _login(client)
        assert res.status_code == 200
        body = res.json()
        assert "access_token" in body
        assert body.get("token_type") == "bearer"
        assert "refresh_token" not in body

    def test_response_sets_kctc_rt_cookie(self, client, test_user):
        res = _login(client)
        assert REFRESH_COOKIE_NAME in res.cookies
        assert res.cookies[REFRESH_COOKIE_NAME]

    def test_cookie_has_required_attributes(self, client, test_user):
        res = _login(client)
        set_cookie = res.headers["set-cookie"]
        lc = set_cookie.lower()
        assert "httponly" in lc
        assert "secure" in lc
        assert "samesite=none" in lc
        assert f"path={REFRESH_COOKIE_PATH}".lower() in lc
        assert "max-age=" in lc

    def test_cookie_value_resolves_to_db_row(self, client, test_user, db_session):
        res = _login(client)
        cookie_value = res.cookies[REFRESH_COOKIE_NAME]
        from app.core.security import hash_token
        row = db_session.query(RefreshToken).filter(
            RefreshToken.token_hash == hash_token(cookie_value)
        ).first()
        assert row is not None
        assert row.user_id == test_user.user_id


class TestRefreshConsumesCookie:
    def test_with_valid_cookie_returns_new_access_and_rotated_cookie(
        self, client, test_user, allowed_origin
    ):
        login_res = _login(client)
        original_cookie = login_res.cookies[REFRESH_COOKIE_NAME]

        refresh_res = client.post(
            "/auth/refresh",
            cookies={REFRESH_COOKIE_NAME: original_cookie},
            headers={"Origin": allowed_origin},
        )

        assert refresh_res.status_code == 200
        body = refresh_res.json()
        assert "access_token" in body
        assert "refresh_token" not in body

        rotated = refresh_res.cookies[REFRESH_COOKIE_NAME]
        assert rotated != original_cookie

    def test_without_cookie_returns_401(self, client, allowed_origin):
        res = client.post("/auth/refresh", headers={"Origin": allowed_origin})
        assert res.status_code == 401

    def test_disallowed_origin_returns_401(self, client, test_user):
        login_res = _login(client)
        cookie = login_res.cookies[REFRESH_COOKIE_NAME]
        res = client.post(
            "/auth/refresh",
            cookies={REFRESH_COOKIE_NAME: cookie},
            headers={"Origin": "https://evil.example.com"},
        )
        assert res.status_code == 401

    def test_missing_origin_returns_401(self, client, test_user):
        login_res = _login(client)
        cookie = login_res.cookies[REFRESH_COOKIE_NAME]
        res = client.post("/auth/refresh", cookies={REFRESH_COOKIE_NAME: cookie})
        assert res.status_code == 401

    def test_old_token_is_invalidated_after_rotation(
        self, client, test_user, db_session, allowed_origin
    ):
        login_res = _login(client)
        original_cookie = login_res.cookies[REFRESH_COOKIE_NAME]

        client.post(
            "/auth/refresh",
            cookies={REFRESH_COOKIE_NAME: original_cookie},
            headers={"Origin": allowed_origin},
        )

        replay = client.post(
            "/auth/refresh",
            cookies={REFRESH_COOKIE_NAME: original_cookie},
            headers={"Origin": allowed_origin},
        )
        assert replay.status_code == 401


class TestLogoutClearsCookie:
    def test_logout_deletes_db_row_and_clears_cookie(self, client, test_user, db_session):
        login_res = _login(client)
        cookie = login_res.cookies[REFRESH_COOKIE_NAME]
        assert db_session.query(RefreshToken).count() == 1

        res = client.post(
            "/auth/logout",
            cookies={REFRESH_COOKIE_NAME: cookie},
        )

        assert res.status_code == 200
        assert db_session.query(RefreshToken).count() == 0

        set_cookie = res.headers["set-cookie"]
        lc = set_cookie.lower()
        assert f"{REFRESH_COOKIE_NAME}=".lower() in lc
        assert f"path={REFRESH_COOKIE_PATH}".lower() in lc
        assert "max-age=0" in lc or 'expires="thu, 01 jan 1970' in lc

    def test_logout_without_cookie_returns_200_and_clears_cookie(self, client):
        res = client.post("/auth/logout")
        assert res.status_code == 200
        set_cookie = res.headers.get("set-cookie", "")
        assert REFRESH_COOKIE_NAME in set_cookie
