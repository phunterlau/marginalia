"""Optional Responses adapter for isolated comparison artifacts, never Brain cards."""

import json


class OpenAIComparisonProvider:
    def __init__(self, *, model, reasoning_effort):
        from openai import OpenAI

        self.client = OpenAI(max_retries=0, timeout=60)
        self.model = model
        self.reasoning_effort = reasoning_effort

    def generate(self, *, schema_name, schema, instructions, payload):
        response = self.client.responses.create(
            model=self.model,
            reasoning={"effort": self.reasoning_effort},
            instructions=instructions,
            input=json.dumps(payload, ensure_ascii=False),
            text={
                "format": {
                    "type": "json_schema",
                    "name": schema_name,
                    "schema": schema,
                    "strict": True,
                }
            },
            max_output_tokens=4096,
            store=False,
        )
        # Parse/validate outside the adapter so refusals and invalid JSON retain
        # their response IDs, raw output, status, and billed usage in the ledger.
        return {
            "response_id": response.id,
            "status": response.status,
            "output_text": response.output_text,
            "raw_output": [item.model_dump() for item in response.output],
            "usage": response.usage.model_dump() if response.usage else {},
        }
