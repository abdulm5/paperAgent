from pydantic import BaseModel

from app.domain.proposals import ClaimKind, MitigationProposalDetail, ProposalDecision


class ProposalMetrics(BaseModel):
    claim_coverage: float
    citation_validity: float
    expected_action: float
    approval_gate: float
    recovery_verified: float

    @property
    def passed(self) -> bool:
        return all(value == 1.0 for value in self.model_dump().values())


def evaluate_proposal(
    proposal: MitigationProposalDetail,
    allowed_evidence_ids: set[str],
) -> ProposalMetrics:
    kinds = {claim.kind for claim in proposal.claims}
    cited_ids = {
        evidence_id for claim in proposal.claims for evidence_id in claim.evidence_ids
    }
    has_approval = any(
        decision.decision is ProposalDecision.APPROVE for decision in proposal.decisions
    )
    execution_is_gated = proposal.execution is None or has_approval
    return ProposalMetrics(
        claim_coverage=float(kinds == set(ClaimKind)),
        citation_validity=float(bool(cited_ids) and cited_ids <= allowed_evidence_ids),
        expected_action=float(
            proposal.action.action_type == "rollback_service"
            and proposal.action.target_service == "checkout-api"
            and proposal.action.target_release == "stable-v1"
        ),
        approval_gate=float(execution_is_gated),
        recovery_verified=float(
            proposal.execution is not None and proposal.execution.recovery_verified
        ),
    )
