from app.domain.evaluations import CausalKind, CauseCandidate
from app.domain.proposals import ActionEnvelope

AUTOMATION_CONFIDENCE_FLOOR = 0.75


def derive_action(cause: CauseCandidate) -> ActionEnvelope:
    if cause.score < AUTOMATION_CONFIDENCE_FLOOR:
        return ActionEnvelope(
            action_type="escalate_only",
            target_release=None,
            expected_faulty_commit=None,
            automation_allowed=False,
        )
    if cause.kind is CausalKind.CODE_CHANGE:
        return ActionEnvelope(expected_faulty_commit=cause.reference)
    if cause.kind is CausalKind.CONFIGURATION_CHANGE:
        return ActionEnvelope(
            action_type="disable_feature_flag",
            target_release=None,
            expected_faulty_commit=None,
            feature_flag=cause.reference,
            automation_allowed=True,
        )
    return ActionEnvelope(
        action_type="escalate_only",
        target_release=None,
        expected_faulty_commit=None,
        automation_allowed=False,
    )


def required_runbook(action: ActionEnvelope) -> str:
    return {
        "rollback_service": "checkout-api-rollback",
        "disable_feature_flag": "checkout-feature-flag-rollback",
        "escalate_only": "payment-provider-degradation",
    }[action.action_type]
