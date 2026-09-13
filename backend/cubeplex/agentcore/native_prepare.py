"""Freeze the control-plane configuration used by one native dispatch."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select


async def prepare_native_request(
    request: dict[str, object], *, ctx: Any, app: Any
) -> dict[str, object]:
    from cubeplex.config import config
    from cubeplex.db.engine import async_session_maker
    from cubeplex.llm.resolver import parse_model_ref, resolve_model_preset
    from cubeplex.llm.snapshot import load_llm_snapshot
    from cubeplex.models.agent_config import AgentConfig
    from cubeplex.prompts.system import BASE_SYSTEM_PROMPT
    from cubeplex.sandbox.manager import get_sandbox_manager

    if request.get("attachments"):
        raise ValueError("native_attachments_not_supported")
    # Until the native tools share the command-policy middleware, refuse
    # configured policies instead of silently bypassing deny/confirm rules.
    rules = await get_sandbox_manager().resolve_command_rules(ctx.org_id)
    if rules:
        raise ValueError("native_command_policy_not_supported")
    async with async_session_maker() as session:
        snapshot = await load_llm_snapshot(session, ctx.org_id, app.state.encryption_backend)
        raw_key = request.get("model_key")
        preset = resolve_model_preset(snapshot, raw_key if isinstance(raw_key, str) else None)
        if len(preset.chain) != 1:
            raise ValueError("native_model_fallback_not_supported")
        provider_id, model_id = parse_model_ref(preset.chain[0])
        provider = snapshot.providers[provider_id]
        if provider.api != "openai-responses":
            raise ValueError("native_model_requires_responses")
        model = next(item for item in provider.models if item.id == model_id)
        agent_config = (
            await session.execute(
                select(AgentConfig).where(
                    AgentConfig.org_id == ctx.org_id,
                    AgentConfig.workspace_id == ctx.workspace_id,
                )
            )
        ).scalar_one_or_none()
        prompt = BASE_SYSTEM_PROMPT
        if agent_config and agent_config.system_prompt:
            prompt += "\n\n" + agent_config.system_prompt
        await session.commit()
    prompt += (
        "\n\nThis run executes inside an AgentCore MicroVM. Use only the tools supplied "
        "in this run. Bash, Git and Python are installed. Use /workspace for task files. "
        "Only ordinary non-hidden workspace files are saved between runs; .git, hidden "
        "files, credentials, installed packages and background processes are not saved. "
        "Use present_file for outputs the user should download. Public Git repositories "
        "can be cloned with execute. Ask the user with ask_user when their answer is "
        "required. Do not claim unavailable integrations or credentials."
    )
    return {
        **request,
        "execution_mode": "native",
        "runtime_arn": str(config.get("agentcore.native_runtime_arn", "")),
        "model_ref": preset.chain[0],
        "system_prompt": prompt,
        "model": {
            "id": model_id,
            "max_output_tokens": min(model.max_tokens, 2048),
            "context_window": model.context_window,
        },
    }
