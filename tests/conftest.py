import os

# Set environment before importing app modules so pydantic-settings sees these
# values when it instantiates app.core.config.settings.
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SECRET_KEY"] = "x" * 64
os.environ["FRONTEND_URL"] = "http://testserver"
os.environ["ENVIRONMENT"] = "staging"
os.environ.pop("EXTRA_CORS_ORIGINS", None)
os.environ.pop("CORS_ORIGIN_REGEX", None)

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.core.security import hash_password
from app.main import app
from app.models import models  # noqa: F401 — registers ORM classes on Base.metadata


@pytest.fixture(scope="session")
def engine():
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture
def db_session(engine):
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        with engine.begin() as conn:
            for table in reversed(Base.metadata.sorted_tables):
                conn.execute(table.delete())


@pytest.fixture
def client(db_session):
    def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def test_user(db_session):
    user = models.User(
        firstname="Test",
        lastname="User",
        email="test@example.com",
        password_hash=hash_password("password123"),
        is_active=1,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture
def allowed_origin():
    from app.core.config import settings
    return settings.frontend_url
