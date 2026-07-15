from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.constants import (
    DEFAULT_ORGANIZATION_ID,
    DEFAULT_ORGANIZATION_NAME,
    DEFAULT_ORGANIZATION_SLUG,
    DEV_IDENTITY_ISSUER,
    SANDBOX_ORGANIZATION_ID,
    SANDBOX_ORGANIZATION_NAME,
    SANDBOX_ORGANIZATION_SLUG,
)
from app.auth.oidc import OidcIdentity
from app.auth.permissions import permissions_for_role
from app.db.models import OrganizationMembershipRecord, OrganizationRecord, UserRecord
from app.domain.auth import (
    ActiveOrganization,
    DevPersona,
    MembershipSummary,
    OrganizationSummary,
    Principal,
    Role,
    SessionResponse,
    UserSummary,
)


class PrincipalNotFoundError(LookupError):
    pass


class OrganizationNotFoundError(LookupError):
    pass


class PersonaNotFoundError(LookupError):
    pass


class IdentityNotProvisionedError(LookupError):
    pass


@dataclass(frozen=True)
class DevPersonaDefinition:
    slug: str
    email: str
    display_name: str
    role: Role

    def response(self) -> DevPersona:
        return DevPersona(
            slug=self.slug,
            email=self.email,
            display_name=self.display_name,
            role=self.role,
        )


DEV_PERSONAS: tuple[DevPersonaDefinition, ...] = (
    DevPersonaDefinition(
        slug="viewer",
        email="viewer@pageragent.local",
        display_name="Vera Viewer",
        role=Role.VIEWER,
    ),
    DevPersonaDefinition(
        slug="responder",
        email="responder@pageragent.local",
        display_name="Riley Responder",
        role=Role.RESPONDER,
    ),
    DevPersonaDefinition(
        slug="incident-commander",
        email="commander@pageragent.local",
        display_name="Casey Commander",
        role=Role.INCIDENT_COMMANDER,
    ),
    DevPersonaDefinition(
        slug="admin",
        email="admin@pageragent.local",
        display_name="Avery Admin",
        role=Role.ADMIN,
    ),
)


class AuthService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def load_principal(self, user_id: UUID, organization_id: UUID) -> Principal:
        row = self.session.execute(
            select(UserRecord, OrganizationRecord, OrganizationMembershipRecord)
            .join(
                OrganizationMembershipRecord,
                OrganizationMembershipRecord.user_id == UserRecord.id,
            )
            .join(
                OrganizationRecord,
                OrganizationRecord.id == OrganizationMembershipRecord.organization_id,
            )
            .where(
                UserRecord.id == user_id,
                UserRecord.is_active.is_(True),
                OrganizationRecord.id == organization_id,
                OrganizationMembershipRecord.is_active.is_(True),
            )
        ).one_or_none()
        if row is None:
            raise PrincipalNotFoundError("User has no active membership in this organization")
        _, organization, membership = row
        try:
            role = Role(membership.role)
        except ValueError as error:
            raise PrincipalNotFoundError("Membership has an unsupported role") from error
        return Principal(
            user_id=user_id,
            organization_id=organization.id,
            organization_slug=organization.slug,
            role=role,
            permissions=permissions_for_role(role),
        )

    def build_session(self, principal: Principal, csrf_token: str) -> SessionResponse:
        user = self.session.scalar(
            select(UserRecord).where(
                UserRecord.id == principal.user_id,
                UserRecord.is_active.is_(True),
            )
        )
        if user is None:
            raise PrincipalNotFoundError("User is inactive or unavailable")

        rows = self.session.execute(
            select(OrganizationMembershipRecord, OrganizationRecord)
            .join(
                OrganizationRecord,
                OrganizationRecord.id == OrganizationMembershipRecord.organization_id,
            )
            .where(
                OrganizationMembershipRecord.user_id == principal.user_id,
                OrganizationMembershipRecord.is_active.is_(True),
            )
            .order_by(OrganizationRecord.slug)
        ).all()
        memberships = [
            MembershipSummary(
                organization=OrganizationSummary(
                    id=organization.id,
                    slug=organization.slug,
                    name=organization.name,
                ),
                role=Role(membership.role),
            )
            for membership, organization in rows
        ]
        active = next(
            (
                membership
                for membership in memberships
                if membership.organization.id == principal.organization_id
            ),
            None,
        )
        if active is None:
            raise PrincipalNotFoundError("Active membership is unavailable")

        return SessionResponse(
            user=UserSummary(
                id=user.id,
                email=user.email,
                display_name=user.display_name,
            ),
            active_organization=ActiveOrganization(
                id=active.organization.id,
                slug=active.organization.slug,
                name=active.organization.name,
                role=active.role,
            ),
            memberships=memberships,
            permissions=sorted(principal.permissions, key=str),
            csrf_token=csrf_token,
        )

    def create_dev_principal(self, persona_slug: str, organization_slug: str) -> Principal:
        persona = next((item for item in DEV_PERSONAS if item.slug == persona_slug), None)
        if persona is None:
            raise PersonaNotFoundError("Unknown development persona")
        dev_organizations = (
            (
                DEFAULT_ORGANIZATION_ID,
                DEFAULT_ORGANIZATION_SLUG,
                DEFAULT_ORGANIZATION_NAME,
            ),
            (
                SANDBOX_ORGANIZATION_ID,
                SANDBOX_ORGANIZATION_SLUG,
                SANDBOX_ORGANIZATION_NAME,
            ),
        )
        organizations_by_slug = {
            organization.slug: organization
            for organization in self.session.scalars(
                select(OrganizationRecord).where(
                    OrganizationRecord.slug.in_([item[1] for item in dev_organizations])
                )
            )
        }
        for organization_id, slug, name in dev_organizations:
            if slug not in organizations_by_slug:
                organization_record = OrganizationRecord(
                    id=organization_id,
                    slug=slug,
                    name=name,
                )
                self.session.add(organization_record)
                organizations_by_slug[slug] = organization_record
        self.session.flush()
        organization = organizations_by_slug.get(organization_slug)
        if organization is None:
            organization = self.session.scalar(
                select(OrganizationRecord).where(OrganizationRecord.slug == organization_slug)
            )
        if organization is None:
            raise OrganizationNotFoundError("Organization not found")

        user = self.session.scalar(
            select(UserRecord).where(
                UserRecord.issuer == DEV_IDENTITY_ISSUER,
                UserRecord.subject == persona.slug,
            )
        )
        now = datetime.now(UTC)
        if user is None:
            user = UserRecord(
                issuer=DEV_IDENTITY_ISSUER,
                subject=persona.slug,
                email=persona.email,
                display_name=persona.display_name,
                is_active=True,
                last_login_at=now,
            )
            self.session.add(user)
            self.session.flush()
        else:
            user.email = persona.email
            user.display_name = persona.display_name
            user.is_active = True
            user.last_login_at = now

        membership_organizations = set(organizations_by_slug.values()) | {organization}
        for membership_organization in membership_organizations:
            membership = self.session.get(
                OrganizationMembershipRecord,
                (membership_organization.id, user.id),
            )
            if membership is None:
                membership = OrganizationMembershipRecord(
                    organization_id=membership_organization.id,
                    user_id=user.id,
                    role=persona.role.value,
                    is_active=True,
                )
                self.session.add(membership)
            else:
                membership.role = persona.role.value
                membership.is_active = True
        self.session.commit()
        return self.load_principal(user.id, organization.id)

    def load_oidc_principal(
        self,
        identity: OidcIdentity,
        organization_id: UUID,
    ) -> Principal:
        user = self.session.scalar(
            select(UserRecord).where(
                UserRecord.issuer == identity.issuer,
                UserRecord.subject == identity.subject,
                UserRecord.is_active.is_(True),
            )
        )
        if user is None:
            raise IdentityNotProvisionedError("OIDC identity has not been provisioned")
        try:
            principal = self.load_principal(user.id, organization_id)
        except PrincipalNotFoundError as error:
            raise IdentityNotProvisionedError(
                "OIDC identity has no active membership in this organization"
            ) from error
        user.email = identity.email
        user.display_name = identity.display_name
        user.last_login_at = datetime.now(UTC)
        self.session.commit()
        return principal
