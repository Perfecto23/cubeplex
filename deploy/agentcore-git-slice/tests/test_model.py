from __future__ import annotations

import httpx
import pytest

from cubeplex_git_slice.client import BrokerConfig
from cubeplex_git_slice.model import LambdaTransport


class FakeBroker:
    config = BrokerConfig("arn", "us-west-2", "task", "work", "cap")

    async def model(self, body):
        assert body["model"] == "fixture-model"
        return {
            "status": 200,
            "headers": {"content-type": "text/event-stream"},
            "body": "data: ok\n\n",
        }


@pytest.mark.asyncio
async def test_lambda_transport_returns_bounded_sse_response() -> None:
    transport = LambdaTransport(FakeBroker())
    request = httpx.Request(
        "POST",
        "https://lambda-broker.invalid/v1/responses",
        json={"model": "fixture-model", "stream": True},
    )
    response = await transport.handle_async_request(request)
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/event-stream"
    assert response.content == b"data: ok\n\n"


@pytest.mark.parametrize(
    "method,url",
    [
        ("GET", "https://lambda-broker.invalid/v1/responses"),
        ("POST", "https://external.invalid/v1/responses"),
        ("POST", "https://lambda-broker.invalid/v1/files"),
    ],
)
async def test_lambda_transport_rejects_unrelated_api_calls(method, url) -> None:
    response = await LambdaTransport(FakeBroker()).handle_async_request(
        httpx.Request(method, url, json={"unexpected": "must_not_reach_broker"})
    )
    assert response.status_code == 400
