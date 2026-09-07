import sys
from types import SimpleNamespace
from unittest.mock import MagicMock
import pytest

from research_brain.openai_provider import OpenAIResponsesProvider
from research_brain.openai_provider import ProviderResponseError


def test_response_output_and_retry_limits(monkeypatch):
    client = MagicMock()
    client.responses.create.return_value = SimpleNamespace(
        id="fixture-response", output_text='{"cards":[]}', usage=None)
    constructor = MagicMock(return_value=client)
    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=constructor))
    provider = OpenAIResponsesProvider(model="gpt-5.6-luna")
    provider.extract(schema_name="fixture", schema={}, instructions="test", evidence=[])
    constructor.assert_called_once_with(max_retries=0, timeout=120)
    args = client.responses.create.call_args.kwargs
    assert args["model"] == "gpt-5.6-luna"
    assert args["max_output_tokens"] == 16384
    assert args["store"] is False


def test_invalid_json_preserves_response_id_and_usage(monkeypatch):
    client = MagicMock()
    usage = SimpleNamespace(model_dump=lambda: {"total_tokens": 42})
    client.responses.create.return_value = SimpleNamespace(id="returned", output_text="invalid", usage=usage, status="completed")
    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=lambda **kw: client))
    provider = OpenAIResponsesProvider(model="gpt-5.6-luna")
    with pytest.raises(ProviderResponseError) as error:
        provider.extract(schema_name="fixture", schema={}, instructions="test", evidence=[])
    assert error.value.response_payload["usage"]["total_tokens"] == 42
    assert error.value.response_payload["response_id"] == "returned"
