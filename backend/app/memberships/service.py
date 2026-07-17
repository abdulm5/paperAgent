from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import (
    AuthSessionRecord,
    IdentityAuditEventRecord,
    OrganizationMembershipRecord,
    OrganizationRecord,
    UserRecord,
)
from app.domain.auth import Role
from app.domain.memberships import (
    IdentityAuditEvent,
    MembershipDetail,
    MembershipIdentity,
    MembershipProvisionInput,
    MembershipUpdateInput,
)


class OrganizationNotFoundError(LookupError):
    pass


class MembershipNotFoundError(LookupError):
    pass


class ActorAuthorityChangedError(PermissionError):
    pass


class MembershipVersionConflictError(Exception):
    def __init__(self, current_version: int) -> None:
        self.current_version = current_version
        super().__init__(f"Membership changed; current version is {current_version}")


class MembershipIdentityConflictError(Exception):
    pass


class MembershipAlreadyExistsError(Exception):
    pass


class MembershipInvariantError(Exception):
    pass


class MembershipContractError(ValueError):
    pass


class MembershipService:
    """Serialize tenant membership changes and preserve their append-only receipts."""

    def __init__(
        self,
        session: Session,
        organization_id: UUID,
        *,
        configured_issuer: str | None,
    ) -> None:
        self.session = session
        self.organization_id = organization_id
        self.configured_issuer = configured_issuer

    def list_memberships(self) -> list[MembershipDetail]:
        rows = self.session.execute(
            select(OrganizationMembershipRecord, UserRecord)
            .join(UserRecord, UserRecord.id == OrganizationMembershipRecord.user_id)
            .where(OrganizationMembershipRecord.organization_id == self.organization_id)
            .order_by(UserRecord.display_name, UserRecord.id)
        ).all()
        return [self._to_detail(membership, user) for membership, user in rows]

    def list_audit_events(self, *, limit: int = 100) -> list[IdentityAuditEvent]:
        records = self.session.scalars(
            select(IdentityAuditEventRecord)
            .where(IdentityAuditEventRecord.organization_id == self.organization_id)
            .order_by(
                IdentityAuditEventRecord.created_at.desc(),
                IdentityAuditEventRecord.id.desc(),
            )
            .limit(limit)
        ).all()
        return [self._to_event(record) for record in records]

    def provision(
        self,
        request: MembershipProvisionInput,
        *,
        actor_user_id: UUID,
    ) -> MembershipDetail:
        self._lock_organization()
        self._require_active_admin(actor_user_id)
        if self.configured_issuer is None or request.issuer != self.configured_issuer:
            raise MembershipIdentityConflictError(
                "Identity issuer does not match the configured OIDC issuer"
            )

        user = self.session.scalar(
            select(UserRecord)
            .where(
                UserRecord.issuer == request.issuer,
                UserRecord.subject == request.subject,
            )
            .with_for_update()
        )
        if user is None:
            user = UserRecord(
                issuer=request.issuer,
                subject=request.subject,
                email=request.email,
                display_name=request.display_name,
                is_active=True,
            )
            self.session.add(user)
            self._flush_identity_change()
        else:
            if not user.is_active:
                raise MembershipIdentityConflictError("The stable identity is globally inactive")
            if (
                user.email.casefold() != request.email.casefold()
                or user.display_name != request.display_name
            ):
                raise MembershipIdentityConflictError(
                    "The stable identity already exists with different profile claims"
                )

        existing = self.session.get(
            OrganizationMembershipRecord,
            (self.organization_id, user.id),
        )
        if existing is not None:
            raise MembershipAlreadyExistsError(
                "The stable identity already has a membership in this organization"
            )

        membership = OrganizationMembershipRecord(
            organization_id=self.organization_id,
            user_id=user.id,
            role=request.role.value,
            is_active=True,
            version=1,
        )
        self.session.add(membership)
        self._flush_identity_change()
        self._append_event(
            membership,
            event_type="membership.provisioned",
            actor_user_id=actor_user_id,
            payload={"role": membership.role, "is_active": membership.is_active},
        )
        self._commit_identity_change()
        return self._to_detail(membership, user)

    def update(
        self,
        target_user_id: UUID,
        request: MembershipUpdateInput,
        *,
        actor_user_id: UUID,
    ) -> MembershipDetail:
        self._lock_organization()
        self._require_active_admin(actor_user_id)
        row = self.session.execute(
            select(OrganizationMembershipRecord, UserRecord)
            .join(UserRecord, UserRecord.id == OrganizationMembershipRecord.user_id)
            .where(
                OrganizationMembershipRecord.organization_id == self.organization_id,
                OrganizationMembershipRecord.user_id == target_user_id,
            )
            # The organization row already serializes this tenant's mutations.
            # Lock only the tenant membership here: locking the joined global user
            # would let cross-tenant admins acquire actor/target users in opposite
            # order and deadlock one another.
            .with_for_update(of=OrganizationMembershipRecord)
        ).one_or_none()
        if row is None:
            raise MembershipNotFoundError
        membership, user = row
        if membership.version != request.expected_version:
            raise MembershipVersionConflictError(membership.version)

        next_role = request.role.value if request.role is not None else membership.role
        next_is_active = (
            request.is_active if request.is_active is not None else membership.is_active
        )
        if next_role == membership.role and next_is_active == membership.is_active:
            raise MembershipContractError("The membership patch does not change any values")
        if (
            membership.role == Role.ADMIN.value
            and membership.is_active
            and (next_role != Role.ADMIN.value or not next_is_active)
        ):
            active_admin_count = self.session.scalar(
                select(func.count())
                .select_from(OrganizationMembershipRecord)
                .join(UserRecord, UserRecord.id == OrganizationMembershipRecord.user_id)
                .where(
                    OrganizationMembershipRecord.organization_id == self.organization_id,
                    OrganizationMembershipRecord.role == Role.ADMIN.value,
                    OrganizationMembershipRecord.is_active.is_(True),
                    UserRecord.is_active.is_(True),
                )
            )
            if active_admin_count is None or active_admin_count <= 1:
                raise MembershipInvariantError(
                    "Every organization must retain at least one active administrator"
                )
        if target_user_id == actor_user_id and (
            next_role != Role.ADMIN.value or not next_is_active
        ):
            raise MembershipInvariantError(
                "Administrators cannot demote or deactivate their own active membership"
            )

        changed_fields: list[str] = []
        if next_role != membership.role:
            membership.role = next_role
            changed_fields.append("role")
        if next_is_active != membership.is_active:
            membership.is_active = next_is_active
            changed_fields.append("is_active")
        membership.version += 1
        now = datetime.now(UTC)
        membership.updated_at = now
        if "is_active" in changed_fields and not membership.is_active:
            self.session.execute(
                update(AuthSessionRecord)
                .where(
                    AuthSessionRecord.organization_id == self.organization_id,
                    AuthSessionRecord.user_id == membership.user_id,
                    AuthSessionRecord.revoked_at.is_(None),
                )
                .values(revoked_at=now)
            )
        self._append_event(
            membership,
            event_type="membership.updated",
            actor_user_id=actor_user_id,
            payload={
                "changed_fields": changed_fields,
                "role": membership.role,
                "is_active": membership.is_active,
            },
        )
        self._commit_identity_change()
        return self._to_detail(membership, user)

    def _lock_organization(self) -> None:
        organization_id = self.session.scalar(
            select(OrganizationRecord.id)
            .where(OrganizationRecord.id == self.organization_id)
            .with_for_update()
        )
        if organization_id is None:
            raise OrganizationNotFoundError

    def _require_active_admin(self, actor_user_id: UUID) -> None:
        actor = self.session.scalar(
            select(OrganizationMembershipRecord)
            .join(UserRecord, UserRecord.id == OrganizationMembershipRecord.user_id)
            .where(
                OrganizationMembershipRecord.organization_id == self.organization_id,
                OrganizationMembershipRecord.user_id == actor_user_id,
                OrganizationMembershipRecord.role == Role.ADMIN.value,
                OrganizationMembershipRecord.is_active.is_(True),
                UserRecord.is_active.is_(True),
            )
            .with_for_update(of=OrganizationMembershipRecord)
        )
        if actor is None:
            raise ActorAuthorityChangedError(
                "Administrator authority changed before the membership mutation committed"
            )

    def _append_event(
        self,
        membership: OrganizationMembershipRecord,
        *,
        event_type: str,
        actor_user_id: UUID,
        payload: dict[str, object],
    ) -> None:
        self.session.add(
            IdentityAuditEventRecord(
                organization_id=self.organization_id,
                target_user_id=membership.user_id,
                event_type=event_type,
                actor=f"user:{actor_user_id}",
                membership_version=membership.version,
                payload=payload,
            )
        )

    def _commit_identity_change(self) -> None:
        try:
            self.session.commit()
        except IntegrityError as error:
            self.session.rollback()
            raise MembershipIdentityConflictError(
                "The stable identity or membership changed concurrently"
            ) from error

    def _flush_identity_change(self) -> None:
        try:
            self.session.flush()
        except IntegrityError as error:
            self.session.rollback()
            raise MembershipIdentityConflictError(
                "The stable identity or membership changed concurrently"
            ) from error

    def _to_detail(
        self,
        membership: OrganizationMembershipRecord,
        user: UserRecord,
    ) -> MembershipDetail:
        return MembershipDetail(
            organization_id=membership.organization_id,
            user=MembershipIdentity(
                id=user.id,
                issuer=user.issuer,
                subject=user.subject,
                email=user.email,
                display_name=user.display_name,
                is_active=user.is_active,
            ),
            role=Role(membership.role),
            is_active=membership.is_active,
            version=membership.version,
            created_at=membership.created_at,
            updated_at=membership.updated_at,
        )

    def _to_event(self, record: IdentityAuditEventRecord) -> IdentityAuditEvent:
        return IdentityAuditEvent(
            id=record.id,
            organization_id=record.organization_id,
            target_user_id=record.target_user_id,
            event_type=record.event_type,
            actor=record.actor,
            membership_version=record.membership_version,
            payload=record.payload,
            created_at=record.created_at,
        )
