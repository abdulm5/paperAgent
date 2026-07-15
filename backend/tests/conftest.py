from collections.abc import Iterator
from uuid import UUID

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth.constants import (
    DEFAULT_ORGANIZATION_ID,
    DEFAULT_ORGANIZATION_NAME,
    DEFAULT_ORGANIZATION_SLUG,
)
from app.auth.dependencies import get_current_principal, require_ingest_context
from app.auth.permissions import permissions_for_role
from app.db.base import Base
from app.db.models import OrganizationRecord
from app.db.session import get_db
from app.domain.auth import IngestContext, Principal, Role
from app.main import app

test_engine = create_engine(
    "sqlite+pysqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(bind=test_engine, autoflush=False, expire_on_commit=False)
TEST_USER_ID = UUID("00000000-0000-0000-0000-000000000101")


@event.listens_for(test_engine, "connect")
def enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def override_get_db() -> Iterator[Session]:
    with TestingSessionLocal() as session:
        yield session


def override_current_principal() -> Principal:
    return Principal(
        user_id=TEST_USER_ID,
        organization_id=DEFAULT_ORGANIZATION_ID,
        organization_slug=DEFAULT_ORGANIZATION_SLUG,
        role=Role.ADMIN,
        permissions=permissions_for_role(Role.ADMIN),
    )


def override_ingest_context() -> IngestContext:
    return IngestContext(
        organization_id=DEFAULT_ORGANIZATION_ID,
        organization_slug=DEFAULT_ORGANIZATION_SLUG,
    )


@pytest.fixture
def db_session() -> Iterator[Session]:
    with TestingSessionLocal() as session:
        yield session


@pytest.fixture(autouse=True)
def isolated_database() -> Iterator[None]:
    Base.metadata.drop_all(test_engine)
    Base.metadata.create_all(test_engine)
    with TestingSessionLocal() as session:
        session.add(
            OrganizationRecord(
                id=DEFAULT_ORGANIZATION_ID,
                slug=DEFAULT_ORGANIZATION_SLUG,
                name=DEFAULT_ORGANIZATION_NAME,
            )
        )
        session.commit()
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_principal] = override_current_principal
    app.dependency_overrides[require_ingest_context] = override_ingest_context
    yield
    app.dependency_overrides.clear()
    Base.metadata.drop_all(test_engine)
