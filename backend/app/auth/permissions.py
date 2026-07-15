from app.domain.auth import Permission, Role

_VIEWER_PERMISSIONS = frozenset(
    {
        Permission.INCIDENTS_READ,
    }
)

_RESPONDER_PERMISSIONS = _VIEWER_PERMISSIONS | {
    Permission.INCIDENTS_TRANSITION,
    Permission.INVESTIGATIONS_RUN,
    Permission.PROPOSALS_GENERATE,
    Permission.POSTMORTEMS_GENERATE,
    Permission.POSTMORTEMS_EDIT,
}

_INCIDENT_COMMANDER_PERMISSIONS = _RESPONDER_PERMISSIONS | {
    Permission.INCIDENTS_RESOLVE,
    Permission.MITIGATIONS_DECIDE,
    Permission.POSTMORTEMS_FINALIZE,
    Permission.EVALUATIONS_RUN,
    Permission.CONNECTORS_READ,
}

ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.VIEWER: _VIEWER_PERMISSIONS,
    Role.RESPONDER: frozenset(_RESPONDER_PERMISSIONS),
    Role.INCIDENT_COMMANDER: frozenset(_INCIDENT_COMMANDER_PERMISSIONS),
    Role.ADMIN: frozenset(
        _INCIDENT_COMMANDER_PERMISSIONS
        | {
            Permission.ORGANIZATION_RESET,
            Permission.CONNECTORS_MANAGE,
            Permission.CONNECTORS_VALIDATE,
        }
    ),
}


def permissions_for_role(role: Role) -> frozenset[Permission]:
    return ROLE_PERMISSIONS[role]
