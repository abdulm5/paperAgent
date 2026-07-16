from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, Field


class Role(StrEnum):
    VIEWER = "viewer"
    RESPONDER = "responder"
    INCIDENT_COMMANDER = "incident_commander"
    ADMIN = "admin"


class Permission(StrEnum):
    INCIDENTS_READ = "incidents.read"
    INCIDENTS_TRANSITION = "incidents.transition"
    INCIDENTS_RESOLVE = "incidents.resolve"
    INVESTIGATIONS_RUN = "investigations.run"
    PROPOSALS_GENERATE = "proposals.generate"
    MITIGATIONS_DECIDE = "mitigations.decide"
    POSTMORTEMS_GENERATE = "postmortems.generate"
    POSTMORTEMS_EDIT = "postmortems.edit"
    POSTMORTEMS_FINALIZE = "postmortems.finalize"
    EVALUATIONS_RUN = "evaluations.run"
    ORGANIZATION_RESET = "organization.reset"
    CONNECTORS_READ = "connectors.read"
    CONNECTORS_MANAGE = "connectors.manage"
    CONNECTORS_VALIDATE = "connectors.validate"
    COLLABORATION_PREPARE = "collaboration.prepare"
    COLLABORATION_DECIDE = "collaboration.decide"


class Principal(BaseModel):
    user_id: UUID
    organization_id: UUID
    organization_slug: str
    role: Role
    permissions: frozenset[Permission]

    @property
    def actor(self) -> str:
        """Stable audit identity; display names can change independently."""
        return f"user:{self.user_id}"


class IngestContext(BaseModel):
    organization_id: UUID
    organization_slug: str

    @property
    def actor(self) -> str:
        return f"ingest:{self.organization_slug}"


class UserSummary(BaseModel):
    id: UUID
    email: str
    display_name: str


class OrganizationSummary(BaseModel):
    id: UUID
    slug: str
    name: str


class ActiveOrganization(OrganizationSummary):
    role: Role


class MembershipSummary(BaseModel):
    organization: OrganizationSummary
    role: Role


class SessionResponse(BaseModel):
    user: UserSummary
    active_organization: ActiveOrganization
    memberships: list[MembershipSummary]
    permissions: list[Permission]
    csrf_token: str


class SessionTokenResponse(BaseModel):
    session: SessionResponse
    access_token: str


class DevPersona(BaseModel):
    slug: str
    email: str
    display_name: str
    role: Role


class DevPersonasResponse(BaseModel):
    personas: list[DevPersona]


class DevSessionRequest(BaseModel):
    persona: str = Field(min_length=1, max_length=100)
    organization_slug: str = Field(min_length=1, max_length=100)


class SwitchOrganizationRequest(BaseModel):
    organization_id: UUID


class OidcExchangeRequest(BaseModel):
    organization_id: UUID
