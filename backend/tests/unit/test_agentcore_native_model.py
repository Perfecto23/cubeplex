from typing import Any

import pytest
import respx

from cubeplex.agentcore.native_model import _upstream, complete_sse, validate_model_body
from cubeplex.agentcore.native_service import NativeTaskError


def body(**updates: Any) -> dict[str, Any]:
    return {
        "model": "fixture",
        "input": "test",
        "stream": True,
        "store": False,
        "max_output_tokens": 2048,
        **updates,
    }


@pytest.mark.parametrize(
    "updates",
    [
        {"model": "other"},
        {"background": True},
        {"store": True},
        {"max_output_tokens": True},
        {"tools": [{"type": "web_search"}]},
        {"include": ["message.input_image.image_url"]},
        {
            "input": [
                {"role": "user", "content": [{"type": "input_file", "file_url": "https://other"}]}
            ]
        },
    ],
)
def test_native_model_scope_rejects_hosted_tools_and_other_models(updates: dict[str, Any]) -> None:
    with pytest.raises(NativeTaskError):
        validate_model_body(body(**updates), {"id": "fixture", "max_output_tokens": 2048})


def test_native_model_reasoning_include_is_retained() -> None:
    validate_model_body(
        body(
            include=["reasoning.encrypted_content"], reasoning={"effort": "low", "summary": "auto"}
        ),
        {"id": "fixture", "max_output_tokens": 2048},
    )


@pytest.mark.parametrize(
    "data",
    [
        b'data: {"type":"response.incomplete","response":{"status":"incomplete"}}\n\n',
        b'data: {"type":"response.completed","response":{"status":"completed"}}',
        b"data: {}\n\n",
        b"data: [DONE]\n\n",
    ],
)
def test_native_sse_requires_complete_terminal(data: bytes) -> None:
    with pytest.raises(NativeTaskError):
        complete_sse(data)


@pytest.mark.asyncio
async def test_real_http_proxy_transfers_complete_sse_without_secret_headers() -> None:
    secret = "fake-secret-value"
    sse = 'data: {"type":"response.completed","response":{"status":"completed"}}\n\n'
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post("https://model.invalid/v1/responses").respond(
            200, text=sse, headers={"content-type": "text/event-stream", "x-private": secret}
        )
        result = await _upstream(body(), "https://model.invalid/v1", secret)
        assert result == sse and secret not in result
        assert route.calls[0].request.headers["authorization"] == "Bearer " + secret
    with respx.mock() as mock:
        mock.post("https://model.invalid/v1/responses").respond(403, text=secret)
        with pytest.raises(NativeTaskError) as rejected:
            await _upstream(body(), "https://model.invalid/v1", secret)
        assert rejected.value.code == "native_model_rejected"
        assert secret not in str(rejected.value.detail)
