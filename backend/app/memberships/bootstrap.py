"""One-shot offline bootstrap for a hosted organization's first administrator."""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import (
    IdentityAuditEventRecord,
    OrganizationMembershipRecord,
    OrganizationRecord,
    UserRecord,
)
from app.db.session import SessionLocal
from app.domain.auth import Role
from app.domain.memberships import MembershipProvisionInput


class BootstrapAdminError(RuntimeError):
    """A sanitized refusal to create an unsafe or ambiguous first administrator."""


@dataclass(frozen=True)
class BootstrapAdminResult:
    organization_id: UUID
    user_id: UUID


def bootstrap_first_admin(
    session: Session,
    *,
    organization_slug: str,
    issuer: str,
    subject: str,
    email: str,
    display_name: str,
    configured_issuer: str | None,
    organization_name: str | None = None,
) -> BootstrapAdminResult:
    """Create exactly one audited first admin while holding the tenant lock.

    This boundary is intentionally callable only from trusted offline tooling. It
    never guesses identity from email and creates an organization only when an
    explicit name accompanies a previously unused slug.
    """

    try:
        identity = MembershipProvisionInput.model_validate(
            {
                "issuer": issuer,
                "subject": subject,
                "email": email,
                "display_name": display_name,
                "role": Role.ADMIN.value,
            }
        )
    except ValidationError:
        raise BootstrapAdminError("Bootstrap identity fields are invalid") from None
    if configured_issuer is None or identity.issuer != configured_issuer:
        raise BootstrapAdminError(
            "Bootstrap issuer must exactly match the configured OIDC issuer"
        )

    if re.fullmatch(r"[a-z0-9][a-z0-9-]{0,99}", organization_slug) is None:
        raise BootstrapAdminError("Bootstrap organization slug is invalid")
    if organization_name is not None and (
        organization_name != organization_name.strip()
        or not 1 <= len(organization_name) <= 200
        or any(ord(character) < 32 or ord(character) == 127 for character in organization_name)
    ):
        raise BootstrapAdminError("Bootstrap organization name is invalid")

    organization = session.scalar(
        select(OrganizationRecord)
        .where(OrganizationRecord.slug == organization_slug)
        .with_for_update()
    )
    if organization is None:
        if organization_name is None:
            raise BootstrapAdminError(
                "Bootstrap organization does not exist; provide its organization name"
            )
        organization = OrganizationRecord(
            slug=organization_slug,
            name=organization_name,
        )
        session.add(organization)
        try:
            session.flush()
        except IntegrityError:
            session.rollback()
            raise BootstrapAdminError(
                "Bootstrap organization changed concurrently; no admin was created"
            ) from None
    elif organization_name is not None and organization.name != organization_name:
        raise BootstrapAdminError(
            "Bootstrap organization already exists with a different name"
        )

    active_admins = session.scalar(
        select(func.count())
        .select_from(OrganizationMembershipRecord)
        .join(UserRecord, UserRecord.id == OrganizationMembershipRecord.user_id)
        .where(
            OrganizationMembershipRecord.organization_id == organization.id,
            OrganizationMembershipRecord.role == Role.ADMIN.value,
            OrganizationMembershipRecord.is_active.is_(True),
            UserRecord.is_active.is_(True),
        )
    )
    if active_admins:
        raise BootstrapAdminError(
            "Bootstrap refused because the organization already has an active admin"
        )

    user = session.scalar(
        select(UserRecord)
        .where(
            UserRecord.issuer == identity.issuer,
            UserRecord.subject == identity.subject,
        )
        .with_for_update()
    )
    if user is None:
        user = UserRecord(
            issuer=identity.issuer,
            subject=identity.subject,
            email=identity.email,
            display_name=identity.display_name,
            is_active=True,
        )
        session.add(user)
        try:
            session.flush()
        except IntegrityError:
            session.rollback()
            raise BootstrapAdminError(
                "Bootstrap identity changed concurrently; no admin was created"
            ) from None
    elif (
        not user.is_active
        or user.email.casefold() != identity.email.casefold()
        or user.display_name != identity.display_name
    ):
        raise BootstrapAdminError(
            "Bootstrap stable identity conflicts with an existing user"
        )

    if session.get(OrganizationMembershipRecord, (organization.id, user.id)) is not None:
        raise BootstrapAdminError(
            "Bootstrap stable identity already has an organization membership"
        )

    membership = OrganizationMembershipRecord(
        organization_id=organization.id,
        user_id=user.id,
        role=Role.ADMIN.value,
        is_active=True,
        version=1,
    )
    session.add(membership)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        raise BootstrapAdminError(
            "Bootstrap changed concurrently; no admin was created"
        ) from None
    session.add(
        IdentityAuditEventRecord(
            organization_id=organization.id,
            target_user_id=user.id,
            event_type="membership.provisioned",
            actor="bootstrap:offline",
            membership_version=1,
            payload={
                "role": Role.ADMIN.value,
                "is_active": True,
                "bootstrap": "offline",
            },
        )
    )
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise BootstrapAdminError(
            "Bootstrap changed concurrently; no admin was created"
        ) from None
    return BootstrapAdminResult(organization_id=organization.id, user_id=user.id)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Provision the first hosted PagerAgent organization administrator."
    )
    parser.add_argument("--organization", required=True, help="Organization slug")
    parser.add_argument(
        "--organization-name",
        help="Create the organization with this name when the slug does not exist",
    )
    parser.add_argument("--issuer", required=True, help="Exact configured OIDC issuer")
    parser.add_argument("--subject", required=True, help="Exact immutable OIDC subject")
    parser.add_argument("--email", required=True, help="Expected OIDC email claim")
    parser.add_argument("--display-name", required=True, help="Expected OIDC display name")
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    if settings.auth_mode != "oidc":
        raise SystemExit("Bootstrap requires PAGERAGENT_AUTH_MODE=oidc")
    try:
        with SessionLocal() as session:
            result = bootstrap_first_admin(
                session,
                organization_slug=arguments.organization,
                organization_name=arguments.organization_name,
                issuer=arguments.issuer,
                subject=arguments.subject,
                email=arguments.email,
                display_name=arguments.display_name,
                configured_issuer=settings.oidc_issuer,
            )
    except BootstrapAdminError as error:
        raise SystemExit(str(error)) from None
    print(
        "Bootstrapped hosted administrator "
        f"user={result.user_id} organization={result.organization_id}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
