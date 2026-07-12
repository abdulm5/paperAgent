import json
from copy import deepcopy
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel

StructuredModel = TypeVar("StructuredModel", bound=BaseModel)


class OpenAIStructuredOutputClient:
    """Small Responses API adapter shared by grounded generation workflows."""

    def __init__(
        self,
        api_key: str,
        model_name: str,
        base_url: str,
        timeout_seconds: float,
        client: httpx.Client | None = None,
    ) -> None:
        self.model_name = model_name
        self.client = client or httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout_seconds,
        )

    def generate(
        self,
        output_type: type[StructuredModel],
        schema_name: str,
        system_prompt: str,
        context: dict[str, Any],
    ) -> StructuredModel:
        response = self.client.post(
            "/responses",
            json={
                "model": self.model_name,
                "store": False,
                "reasoning": {"effort": "low"},
                "input": [
                    {"role": "system", "content": system_prompt},
                    {
                        "role": "user",
                        "content": "<incident_evidence>\n"
                        + json.dumps(context, sort_keys=True)
                        + "\n</incident_evidence>",
                    },
                ],
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": schema_name,
                        "strict": True,
                        "schema": self.strict_schema(output_type.model_json_schema()),
                    }
                },
            },
        )
        response.raise_for_status()
        return output_type.model_validate_json(self.output_text(response.json()))

    @staticmethod
    def output_text(payload: dict[str, Any]) -> str:
        for item in payload.get("output", []):
            if item.get("type") != "message":
                continue
            for content in item.get("content", []):
                if content.get("type") == "refusal":
                    raise ValueError(f"OpenAI refused generation: {content.get('refusal')}")
                if content.get("type") == "output_text" and content.get("text"):
                    return str(content["text"])
        raise ValueError("OpenAI response did not contain structured output text")

    @classmethod
    def strict_schema(cls, source: dict[str, Any]) -> dict[str, Any]:
        schema = deepcopy(source)

        def visit(node: object) -> None:
            if isinstance(node, dict):
                if node.get("type") == "object" and "properties" in node:
                    node["additionalProperties"] = False
                    node["required"] = list(node["properties"])
                for value in node.values():
                    visit(value)
            elif isinstance(node, list):
                for value in node:
                    visit(value)

        visit(schema)
        return schema
