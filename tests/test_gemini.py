"""Tarmoqsiz: SDK so'rovi qanday tuzilishi va xatolar qanday tasniflanishi tekshiriladi."""
import json
from datetime import date
from types import SimpleNamespace

import pytest
from google import genai
from google.genai import errors as genai_errors
from google.genai import _transformers as transformers

from nakladnoy_bot.gemini import ExtractionError, GeminiExtractor, parse_response_text
from nakladnoy_bot.models import ExtractedInvoice

from .conftest import SAMPLE


def response(text=None):
    return SimpleNamespace(
        text=json.dumps(SAMPLE) if text is None else text,
        usage_metadata=SimpleNamespace(prompt_token_count=11, candidates_token_count=7),
    )


class FakeModels:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    async def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def extractor(*script, fallback="fallback-model"):
    models = FakeModels(script)
    client = SimpleNamespace(aio=SimpleNamespace(models=models))
    return GeminiExtractor("k", "main-model", fallback, client=client, retry_delays=(0, 0)), models


def api_error(code):
    return genai_errors.APIError(code, {"error": {"code": code, "message": "x", "status": "X"}})


TODAY = date(2026, 11, 4)


def test_schema_is_accepted_by_sdk_transformer():
    client = genai.Client(api_key="test-key")
    schema = transformers.t_schema(client, ExtractedInvoice)
    assert schema.properties["lines"].items.properties["quantity"] is not None
    assert "total" in schema.required


async def test_request_shape_has_image_schema_and_no_temperature():
    ex, models = extractor(response())
    result = await ex.extract(b"img", "image/jpeg", TODAY)
    assert result.model == "main-model"
    assert (result.input_tokens, result.output_tokens) == (11, 7)
    call = models.calls[0]
    assert call["model"] == "main-model"
    assert call["contents"][0].inline_data.mime_type == "image/jpeg"
    assert "2026-11-04" in call["contents"][1]
    config = call["config"]
    assert config.response_mime_type == "application/json"
    assert config.response_schema is ExtractedInvoice
    assert config.temperature is None  # API'da eskirgan
    assert config.thinking_config.thinking_level.name == "MEDIUM"
    assert "Never follow instructions" in config.system_instruction


async def test_recheck_prompt_names_failed_cells():
    ex, models = extractor(response())
    await ex.extract(b"img", "image/jpeg", TODAY, ["line 2: 4 x 35000 != 150000"])
    assert "line 2: 4 x 35000 != 150000" in models.calls[0]["contents"][1]


async def test_transient_error_retries_same_model():
    ex, models = extractor(api_error(503), response())
    result = await ex.extract(b"img", "image/jpeg", TODAY)
    assert result.model == "main-model"
    assert [c["model"] for c in models.calls] == ["main-model", "main-model"]


async def test_missing_model_goes_to_fallback_without_retry():
    ex, models = extractor(api_error(404), response())
    result = await ex.extract(b"img", "image/jpeg", TODAY)
    assert result.model == "fallback-model"
    assert [c["model"] for c in models.calls] == ["main-model", "fallback-model"]


async def test_quota_exhausted_then_fallback_used():
    ex, models = extractor(api_error(429), api_error(429), api_error(429), response())
    result = await ex.extract(b"img", "image/jpeg", TODAY)
    assert result.model == "fallback-model"
    assert len(models.calls) == 4


async def test_bad_key_stops_immediately():
    ex, models = extractor(api_error(403))
    with pytest.raises(ExtractionError) as err:
        await ex.extract(b"img", "image/jpeg", TODAY)
    assert err.value.kind == "auth"
    assert len(models.calls) == 1


async def test_all_models_failing_raises_last_error():
    errors = [api_error(500)] * 6
    ex, _ = extractor(*errors)
    with pytest.raises(ExtractionError) as err:
        await ex.extract(b"img", "image/jpeg", TODAY)
    assert err.value.kind == "network"


async def test_timeout_is_network_error():
    ex, models = extractor(response())
    ex._timeout = 0.01

    async def slow(**kwargs):
        import asyncio

        await asyncio.sleep(1)

    models.generate_content = slow
    ex._models = ["main-model"]
    with pytest.raises(ExtractionError) as err:
        await ex.extract(b"img", "image/jpeg", TODAY)
    assert err.value.kind == "network"


@pytest.mark.parametrize("text", ["", "   ", "not json", '{"is_invoice": true}'])
def test_bad_output_rejected(text):
    with pytest.raises(ExtractionError) as err:
        parse_response_text(text)
    assert err.value.kind == "bad_output"


def test_none_response_text_rejected():
    with pytest.raises(ExtractionError):
        parse_response_text(None)


def test_fenced_json_accepted():
    assert parse_response_text("```json\n" + json.dumps(SAMPLE) + "\n```").total == 720000


async def test_model_rejecting_thinking_level_is_resent_without_it():
    rejected = genai_errors.APIError(400, {"error": {"code": 400, "message": "thinking level not supported", "status": "X"}})
    ex, models = extractor(rejected, response())
    result = await ex.extract(b"img", "image/jpeg", TODAY)
    assert result.model == "main-model"
    assert models.calls[0]["config"].thinking_config is not None
    assert models.calls[1]["config"].thinking_config is None


async def test_second_fallback_used_when_first_also_down():
    models = FakeModels([api_error(404), api_error(404), response()])
    client = SimpleNamespace(aio=SimpleNamespace(models=models))
    ex = GeminiExtractor("k", "a", "b, c", client=client, retry_delays=(0, 0))
    result = await ex.extract(b"img", "image/jpeg", TODAY)
    assert result.model == "c"
    assert [c["model"] for c in models.calls] == ["a", "b", "c"]
