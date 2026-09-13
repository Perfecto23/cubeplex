"""Atomic receipt + native Redis stream append for MicroVM callbacks."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from cubeloop.agent.types import AgentEvent, AgentToolResult, ToolExecutionEndEvent
from pydantic import TypeAdapter, ValidationError
from redis.asyncio import Redis

from cubeplex.agentcore.native_service import NativeTaskError, load_native_dispatch, payload_hash
from cubeplex.agents.stream import StreamConverter
from cubeplex.db.engine import async_session_maker
from cubeplex.streams.run_events import _active_run_key, _run_events_key, _run_meta_key

_EVENTS_LUA = """
local token = ARGV[1]
local prior = redis.call('HGET', KEYS[4], token)
if prior then
  local result = cjson.decode(prior)
  if result.hash ~= ARGV[2] then
    return cjson.encode({code='native_event_conflict'})
  end
  result.duplicate = true
  return cjson.encode(result)
end
if redis.call('HEXISTS', KEYS[4], 'terminal') == 1 then
  return cjson.encode({code='native_task_terminal'})
end
if ARGV[9] ~= '1' then return cjson.encode({code='native_task_stopped'}) end
local current = redis.call('HGET', KEYS[2], 'status')
if not current then return cjson.encode({code='native_run_missing'}) end
if redis.call('GET', KEYS[3]) ~= ARGV[6] then
  return cjson.encode({code='native_active_run_conflict'})
end
if ARGV[10] ~= '' and redis.call('HGET', KEYS[2], 'claim_token') ~= ARGV[10] then
  return cjson.encode({code='native_claim_conflict'})
end
if token == 'terminal' then
  if current ~= 'running' and current ~= 'paused_hitl' then
    return cjson.encode({code='native_terminal_conflict'})
  end
else
  if current ~= 'running' then return cjson.encode({code='native_task_terminal'}) end
  local last = tonumber(redis.call('HGET', KEYS[4], 'last_seq') or '0')
  if tonumber(token) ~= last + 1 then return cjson.encode({code='native_event_gap'}) end
end
local ids = {}
for _,payload in ipairs(cjson.decode(ARGV[3])) do
  local eid = redis.call('XADD', KEYS[1], 'MAXLEN', '~', ARGV[5], '*', 'payload', cjson.encode(payload))
  ids[#ids+1] = eid
  redis.call('HSET', KEYS[2], 'last_event_id', eid)
  redis.call('HSETNX', KEYS[2], 'first_event_id', eid)
end
redis.call('HSET', KEYS[2], 'last_event_at', ARGV[7])
if token == 'terminal' then
  redis.call('HSET', KEYS[2], 'status', ARGV[8])
  if ARGV[8] ~= 'paused_hitl' and redis.call('GET', KEYS[3]) == ARGV[6] then
    redis.call('DEL', KEYS[3])
  end
else
  redis.call('HSET', KEYS[4], 'last_seq', token)
end
local result = {hash=ARGV[2],seq=token,event_ids=ids,duplicate=false,applied=true}
if token == 'terminal' then result.status=ARGV[8] end
redis.call('HSET', KEYS[4], token, cjson.encode(result))
for _,key in ipairs({KEYS[1],KEYS[2],KEYS[4]}) do redis.call('EXPIRE', key, ARGV[4]) end
if redis.call('GET', KEYS[3]) == ARGV[6] then redis.call('EXPIRE', KEYS[3], ARGV[4]) end
return cjson.encode(result)
"""


async def _emit(
    redis: Redis,
    *,
    prefix: str,
    dispatch_id: str,
    run_id: str,
    conversation_id: str,
    token: str,
    fingerprint: str,
    events: list[dict[str, Any]],
    ttl_seconds: int,
    maxlen: int,
    status: str = "",
    allow_write: bool = True,
    claim_token: str | None = None,
) -> dict[str, Any]:
    response = await redis.eval(  # type: ignore[misc]
        _EVENTS_LUA,
        4,
        _run_events_key(prefix, run_id),
        _run_meta_key(prefix, run_id),
        _active_run_key(prefix, conversation_id),
        f"{prefix}:native_events:{dispatch_id}",
        token,
        fingerprint,
        json.dumps(events),
        str(ttl_seconds),
        str(maxlen),
        run_id,
        datetime.now(UTC).isoformat(),
        status,
        "1" if allow_write else "0",
        claim_token or "",
    )
    result: dict[str, Any] = json.loads(response)
    if "code" in result:
        raise NativeTaskError(result["code"])
    # Lua cjson encodes an empty table as {}, whereas the wire contract is list.
    if result.get("event_ids") == {}:
        result["event_ids"] = []
    result.pop("hash", None)
    return result


async def emit_native_terminal(
    redis: Redis,
    *,
    prefix: str,
    dispatch_id: str,
    run_id: str,
    conversation_id: str,
    status: str,
    events: list[dict[str, Any]],
    ttl_seconds: int,
    maxlen: int,
    claim_token: str | None = None,
) -> dict[str, Any]:
    if status not in {"completed", "cancelled", "errored", "paused_hitl"}:
        raise ValueError("native_terminal_invalid")
    try:
        return await _emit(
            redis,
            prefix=prefix,
            dispatch_id=dispatch_id,
            run_id=run_id,
            conversation_id=conversation_id,
            token="terminal",
            fingerprint=payload_hash(
                {
                    "status": status,
                    "events": [
                        {key: value for key, value in event.items() if key != "timestamp"}
                        for event in events
                    ],
                }
            ),
            events=events,
            ttl_seconds=ttl_seconds,
            maxlen=maxlen,
            status=status,
            claim_token=claim_token,
        )
    except NativeTaskError as exc:
        raise ValueError(exc.code) from exc


async def receive_event(
    redis: Redis,
    *,
    prefix: str,
    dispatch_id: UUID,
    seq: int,
    event: dict[str, Any],
    ttl_seconds: int,
    maxlen: int,
) -> dict[str, Any]:
    if type(seq) is not int or seq < 1 or len(json.dumps(event).encode()) > 262144:
        raise NativeTaskError("native_event_invalid", 422)
    try:
        typed: AgentEvent = TypeAdapter(AgentEvent).validate_python(event)
    except ValidationError as exc:
        raise NativeTaskError("native_event_invalid", 422) from exc
    if isinstance(typed, ToolExecutionEndEvent) and isinstance(typed.result, dict):
        if "content" not in typed.result or set(typed.result) - {
            "content",
            "details",
            "is_error",
            "terminate",
        }:
            raise NativeTaskError("native_tool_result_invalid", 422)
        try:
            tool_result = AgentToolResult.model_validate(typed.result)
        except ValidationError as exc:
            raise NativeTaskError("native_tool_result_invalid", 422) from exc
        # CubeLoop's wire field is StructuredValue, so its TypeAdapter leaves
        # this as dict. Restore the official wrapper before StreamConverter
        # extracts JSON tool text, artifacts and presented-file cards.
        typed = typed.model_copy(update={"result": tool_result})
    from cubeplex.streams.run_manager import cubeloop_dict_to_agent_event

    async with async_session_maker() as session, session.begin():
        dispatch = await load_native_dispatch(session, dispatch_id, for_update=True)
        events: list[dict[str, Any]] = []
        # All tools are directly registered in v1; no deferred converter state
        # spans requests. Completion belongs exclusively to /finish.
        for converted in StreamConverter().convert_agent_event(typed):
            native = cubeloop_dict_to_agent_event(converted, datetime.now(UTC).isoformat())
            if native is not None and native.type != "done":
                events.append(native.model_dump(mode="json"))
        result = await _emit(
            redis,
            prefix=prefix,
            dispatch_id=str(dispatch_id),
            run_id=dispatch.run_id,
            conversation_id=dispatch.conversation_id,
            token=str(seq),
            fingerprint=payload_hash(event),
            events=events,
            ttl_seconds=ttl_seconds,
            maxlen=maxlen,
            allow_write=dispatch.status == "claimed" and not dispatch.stop_requested,
            claim_token=dispatch.request.get("claim_token")
            if dispatch.operation == "respond"
            else None,
        )
        result["seq"] = seq
        return result
