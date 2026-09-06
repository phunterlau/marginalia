from types import SimpleNamespace
from unittest.mock import patch, Mock
import pytest

pytest.importorskip("openai")

from research_brain.comparison_provider import OpenAIComparisonProvider


def test_responses_adapter_disables_hidden_retries_and_storage():
    client = Mock()
    client.responses.create.return_value = SimpleNamespace(
        id="resp_test", status="completed", output_text="{}", output=[], usage=None
    )
    with patch("openai.OpenAI", return_value=client) as constructor:
        provider = OpenAIComparisonProvider(
            model="gpt-5.6-luna", reasoning_effort="medium"
        )
        result = provider.generate(
            schema_name="TestV1",
            schema={"type": "object"},
            instructions="test",
            payload={"question": "test"},
        )
    constructor.assert_called_once_with(max_retries=0, timeout=60)
    args = client.responses.create.call_args.kwargs
    assert args["store"] is False and args["max_output_tokens"] == 4096
    assert args["model"] == "gpt-5.6-luna" and args["reasoning"] == {"effort": "medium"}
    assert args["text"]["format"]["strict"] is True
    assert result["response_id"] == "resp_test"
