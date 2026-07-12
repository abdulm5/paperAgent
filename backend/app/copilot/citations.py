from app.domain.proposals import ClaimKind, GroundedBriefDraft


class CitationValidationError(ValueError):
    pass


class CitationValidator:
    version = "citation-policy-v1"

    field_by_kind = {
        ClaimKind.ROOT_CAUSE: "root_cause_summary",
        ClaimKind.IMPACT: "impact_summary",
        ClaimKind.RECOMMENDATION: "recommended_action",
        ClaimKind.RISK: "risk_summary",
    }

    def validate(self, brief: GroundedBriefDraft, allowed_ids: set[str]) -> None:
        claims_by_kind = {claim.kind: claim for claim in brief.claims}
        if len(brief.claims) != len(self.field_by_kind) or len(claims_by_kind) != len(
            self.field_by_kind
        ):
            raise CitationValidationError("Brief must contain exactly one claim of each kind")
        missing_kinds = set(self.field_by_kind) - set(claims_by_kind)
        if missing_kinds:
            names = ", ".join(sorted(kind.value for kind in missing_kinds))
            raise CitationValidationError(f"Missing grounded claims: {names}")

        for kind, field_name in self.field_by_kind.items():
            claim = claims_by_kind[kind]
            if claim.text != getattr(brief, field_name):
                raise CitationValidationError(
                    f"{kind.value} claim does not match its rendered brief field"
                )
            unknown = set(claim.evidence_ids) - allowed_ids
            if unknown:
                raise CitationValidationError(
                    f"{kind.value} claim cites unknown evidence: {', '.join(sorted(unknown))}"
                )
