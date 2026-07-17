"""Add hosted identity administration and AWS KMS credential custody.

Revision ID: 20260718_0012
Revises: 20260717_0011
Create Date: 2026-07-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260718_0012"
down_revision: str | None = "20260717_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

json_document = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def _assert_membership_roles_are_supported() -> None:
    memberships = sa.table(
        "organization_memberships",
        sa.column("role", sa.String()),
    )
    unsupported = op.get_bind().execute(
        sa.select(memberships.c.role)
        .where(
            memberships.c.role.not_in(
                ("viewer", "responder", "incident_commander", "admin")
            )
        )
        .limit(1)
    ).first()
    if unsupported is not None:
        raise RuntimeError(
            "Cannot add the hosted identity boundary while unsupported roles exist"
        )


def _assert_downgrade_is_safe() -> None:
    connection = op.get_bind()
    for table_name, identifier in (
        ("auth_sessions", "id"),
        ("oidc_login_transactions", "id"),
        ("identity_audit_events", "id"),
    ):
        table = sa.table(table_name, sa.column(identifier))
        if connection.execute(
            sa.select(sa.literal(1)).select_from(table).limit(1)
        ).first() is not None:
            raise RuntimeError(
                "Cannot downgrade while hosted identity or administration receipts exist"
            )

    memberships = sa.table(
        "organization_memberships",
        sa.column("version", sa.Integer()),
    )
    if connection.execute(
        sa.select(sa.literal(1))
        .select_from(memberships)
        .where(memberships.c.version != 1)
        .limit(1)
    ).first() is not None:
        raise RuntimeError(
            "Cannot downgrade after membership administration changed a version"
        )

    credentials = sa.table(
        "connector_credentials",
        sa.column("cipher_scheme", sa.String()),
    )
    if connection.execute(
        sa.select(sa.literal(1))
        .select_from(credentials)
        .where(credentials.c.cipher_scheme != "local-aesgcm-v1")
        .limit(1)
    ).first() is not None:
        raise RuntimeError(
            "Cannot downgrade while AWS KMS credential envelopes exist"
        )


def upgrade() -> None:
    _assert_membership_roles_are_supported()

    with op.batch_alter_table("organization_memberships") as batch_op:
        batch_op.add_column(
            sa.Column(
                "version",
                sa.Integer(),
                server_default="1",
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            )
        )
        batch_op.create_check_constraint(
            "ck_organization_memberships_role",
            "role IN ('viewer', 'responder', 'incident_commander', 'admin')",
        )
        batch_op.create_check_constraint(
            "ck_organization_memberships_version_positive",
            "version > 0",
        )

    with op.batch_alter_table("connector_credentials") as batch_op:
        batch_op.add_column(
            sa.Column(
                "cipher_scheme",
                sa.String(length=32),
                server_default="local-aesgcm-v1",
                nullable=False,
            )
        )
        batch_op.drop_constraint(
            "ck_connector_credentials_wrapped_key_nonce_length",
            type_="check",
        )
        batch_op.drop_constraint(
            "ck_connector_credentials_wrapped_key_length",
            type_="check",
        )
        batch_op.alter_column(
            "wrapped_key_nonce",
            existing_type=sa.LargeBinary(),
            nullable=True,
        )
        batch_op.alter_column(
            "key_version",
            existing_type=sa.String(length=100),
            type_=sa.String(length=2048),
            existing_nullable=False,
        )
        batch_op.create_check_constraint(
            "ck_connector_credentials_cipher_scheme",
            "cipher_scheme IN ('local-aesgcm-v1', 'aws-kms-v1')",
        )
        batch_op.create_check_constraint(
            "ck_connector_credentials_wrap_contract",
            "(cipher_scheme = 'local-aesgcm-v1' "
            "AND length(wrapped_key_nonce) = 12 "
            "AND length(wrapped_data_key) = 48 "
            "AND length(key_version) BETWEEN 1 AND 100) OR "
            "(cipher_scheme = 'aws-kms-v1' "
            "AND wrapped_key_nonce IS NULL "
            "AND length(wrapped_data_key) BETWEEN 1 AND 6144 "
            "AND length(key_version) BETWEEN 20 AND 2048)",
        )

    op.create_table(
        "oidc_login_transactions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("state_hash", sa.String(length=64), nullable=False),
        sa.Column("browser_binding_hash", sa.String(length=64), nullable=False),
        sa.Column("nonce_hash", sa.String(length=64), nullable=False),
        sa.Column("verifier_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("verifier_nonce", sa.LargeBinary(), nullable=False),
        sa.Column("organization_slug", sa.String(length=100), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "length(state_hash) = 64 AND length(browser_binding_hash) = 64 "
            "AND length(nonce_hash) = 64",
            name="ck_oidc_login_transactions_hash_lengths",
        ),
        sa.CheckConstraint(
            "length(verifier_nonce) = 12",
            name="ck_oidc_login_transactions_nonce_length",
        ),
        sa.CheckConstraint(
            "length(verifier_ciphertext) BETWEEN 59 AND 272",
            name="ck_oidc_login_transactions_ciphertext_length",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("state_hash"),
    )
    op.create_index(
        "ix_oidc_login_transactions_expires",
        "oidc_login_transactions",
        ["expires_at"],
    )
    op.create_index(
        "ix_oidc_login_transactions_org_pending",
        "oidc_login_transactions",
        ["organization_slug", "consumed_at", "expires_at"],
    )

    op.create_table(
        "auth_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("auth_method", sa.String(length=16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "auth_method IN ('local', 'oidc')",
            name="ck_auth_sessions_method",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "user_id"],
            [
                "organization_memberships.organization_id",
                "organization_memberships.user_id",
            ],
            name="fk_auth_sessions_membership",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_auth_sessions_user_expires",
        "auth_sessions",
        ["user_id", "expires_at"],
    )
    op.create_index(
        "ix_auth_sessions_active_expires",
        "auth_sessions",
        ["revoked_at", "expires_at"],
    )
    op.create_index(
        "ix_auth_sessions_expires",
        "auth_sessions",
        ["expires_at"],
    )

    op.create_table(
        "identity_audit_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("target_user_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("actor", sa.String(length=100), nullable=False),
        sa.Column("membership_version", sa.Integer(), nullable=False),
        sa.Column("payload", json_document, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "event_type IN ('membership.provisioned', 'membership.updated')",
            name="ck_identity_audit_events_type",
        ),
        sa.CheckConstraint(
            "membership_version > 0",
            name="ck_identity_audit_events_version_positive",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "target_user_id"],
            [
                "organization_memberships.organization_id",
                "organization_memberships.user_id",
            ],
            name="fk_identity_audit_events_membership",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id",
            "target_user_id",
            "membership_version",
            name="uq_identity_audit_events_membership_version",
        ),
    )
    op.create_index(
        "ix_identity_audit_events_organization_created",
        "identity_audit_events",
        ["organization_id", "created_at"],
    )


def downgrade() -> None:
    _assert_downgrade_is_safe()

    op.drop_index(
        "ix_identity_audit_events_organization_created",
        table_name="identity_audit_events",
    )
    op.drop_table("identity_audit_events")
    op.drop_index(
        "ix_auth_sessions_expires",
        table_name="auth_sessions",
    )
    op.drop_index(
        "ix_auth_sessions_active_expires",
        table_name="auth_sessions",
    )
    op.drop_index(
        "ix_auth_sessions_user_expires",
        table_name="auth_sessions",
    )
    op.drop_table("auth_sessions")
    op.drop_index(
        "ix_oidc_login_transactions_org_pending",
        table_name="oidc_login_transactions",
    )
    op.drop_index(
        "ix_oidc_login_transactions_expires",
        table_name="oidc_login_transactions",
    )
    op.drop_table("oidc_login_transactions")

    with op.batch_alter_table("connector_credentials") as batch_op:
        batch_op.drop_constraint(
            "ck_connector_credentials_wrap_contract",
            type_="check",
        )
        batch_op.drop_constraint(
            "ck_connector_credentials_cipher_scheme",
            type_="check",
        )
        batch_op.alter_column(
            "key_version",
            existing_type=sa.String(length=2048),
            type_=sa.String(length=100),
            existing_nullable=False,
        )
        batch_op.alter_column(
            "wrapped_key_nonce",
            existing_type=sa.LargeBinary(),
            nullable=False,
        )
        batch_op.create_check_constraint(
            "ck_connector_credentials_wrapped_key_nonce_length",
            "length(wrapped_key_nonce) = 12",
        )
        batch_op.create_check_constraint(
            "ck_connector_credentials_wrapped_key_length",
            "length(wrapped_data_key) = 48",
        )
        batch_op.drop_column("cipher_scheme")

    with op.batch_alter_table("organization_memberships") as batch_op:
        batch_op.drop_constraint(
            "ck_organization_memberships_version_positive",
            type_="check",
        )
        batch_op.drop_constraint(
            "ck_organization_memberships_role",
            type_="check",
        )
        batch_op.drop_column("updated_at")
        batch_op.drop_column("version")
