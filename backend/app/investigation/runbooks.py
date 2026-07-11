from pathlib import Path

import yaml
from pydantic import BaseModel

from app.investigation.text import (
    canonical_hash,
    cosine_similarity,
    token_coverage,
    tokenize,
)


class RunbookDocument(BaseModel):
    runbook_id: str
    title: str
    service: str
    failure_mode: str
    owner: str
    content: str
    sections: list[dict[str, str]]
    content_hash: str


class RankedRunbook(BaseModel):
    document: RunbookDocument
    total_score: float
    score_breakdown: dict[str, float]
    matched_sections: list[dict[str, str]]


class RunbookRetriever:
    version = "hybrid-runbook-v1"

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def retrieve(
        self,
        service: str,
        failure_mode: str,
        query: str,
    ) -> list[RankedRunbook]:
        query_tokens = tokenize(query, service, failure_mode)
        ranked: list[RankedRunbook] = []
        for document in self._load_documents():
            metadata_score = (
                (0.7 if document.service == service else 0.0)
                + (0.3 if document.failure_mode == failure_mode else 0.0)
            )
            document_tokens = tokenize(document.title, document.content)
            lexical_score = token_coverage(query_tokens, document_tokens)
            vector_score = cosine_similarity(query_tokens, document_tokens)
            breakdown = {
                "metadata": round(metadata_score, 4),
                "lexical": round(lexical_score, 4),
                "vector": round(vector_score, 4),
            }
            total_score = (
                0.5 * metadata_score + 0.3 * lexical_score + 0.2 * vector_score
            )
            ranked.append(
                RankedRunbook(
                    document=document,
                    total_score=round(total_score, 4),
                    score_breakdown=breakdown,
                    matched_sections=self._match_sections(document, query_tokens),
                )
            )
        return sorted(ranked, key=lambda match: match.total_score, reverse=True)

    def _load_documents(self) -> list[RunbookDocument]:
        return [self._parse_document(path) for path in sorted(self.directory.glob("*.md"))]

    @staticmethod
    def _parse_document(path: Path) -> RunbookDocument:
        content = path.read_text()
        frontmatter, markdown = content.split("---", 2)[1:]
        metadata = yaml.safe_load(frontmatter)
        title = next(
            line.removeprefix("# ").strip()
            for line in markdown.splitlines()
            if line.startswith("# ")
        )
        sections: list[dict[str, str]] = []
        heading: str | None = None
        lines: list[str] = []
        for line in markdown.splitlines():
            if line.startswith("## "):
                if heading:
                    sections.append({"heading": heading, "excerpt": " ".join(lines).strip()})
                heading = line.removeprefix("## ").strip()
                lines = []
            elif heading and line.strip():
                lines.append(line.strip())
        if heading:
            sections.append({"heading": heading, "excerpt": " ".join(lines).strip()})
        return RunbookDocument(
            runbook_id=str(metadata["id"]),
            title=title,
            service=str(metadata["service"]),
            failure_mode=str(metadata["failure_mode"]),
            owner=str(metadata["owner"]),
            content=markdown.strip(),
            sections=sections,
            content_hash=canonical_hash(content),
        )

    @staticmethod
    def _match_sections(
        document: RunbookDocument, query_tokens: set[str]
    ) -> list[dict[str, str]]:
        scored = sorted(
            document.sections,
            key=lambda section: token_coverage(
                query_tokens, tokenize(section["heading"], section["excerpt"])
            ),
            reverse=True,
        )
        return [
            {"heading": section["heading"], "excerpt": section["excerpt"][:400]}
            for section in scored[:2]
        ]
