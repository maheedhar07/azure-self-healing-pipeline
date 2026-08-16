"""
Model-provider dispatch. Swap providers with the MODEL_PROVIDER env var:

  MODEL_PROVIDER=azure_openai   -> Azure OpenAI, reasoning model (default o4-mini)
  MODEL_PROVIDER=claude_foundry -> Claude on Microsoft Foundry (default Sonnet 4.6)

Both return the same fix dict, so the orchestrator is provider-agnostic. SDK
imports are lazy so unit tests never need them installed.
"""

import json
import os

from . import core


def make_request_fix(provider: str = None):
    """Return a `request_fix(context) -> dict` callable for the chosen provider."""
    provider = (provider or os.environ.get("MODEL_PROVIDER", "azure_openai")).lower()
    if provider == "azure_openai":
        return _azure_openai_fix
    if provider == "claude_foundry":
        return _claude_foundry_fix
    raise ValueError(f"Unknown MODEL_PROVIDER: {provider!r}")


def _azure_openai_fix(context: dict) -> dict:
    """Azure OpenAI path. Default deployment is a reasoning model (o4-mini).

    Temperature is intentionally omitted — reasoning models reject a non-default
    temperature, and determinism here comes from the task, not sampling.
    """
    from openai import AzureOpenAI
    from azure.identity import DefaultAzureCredential, get_bearer_token_provider

    token_provider = get_bearer_token_provider(
        DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default")
    client = AzureOpenAI(
        azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
        api_version="2024-10-21",
        azure_ad_token_provider=token_provider,
    )
    resp = client.chat.completions.create(
        model=os.environ.get("AZURE_OPENAI_DEPLOYMENT", "o4-mini"),
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": core.SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(context)},
        ],
    )
    return core.extract_fix_json(resp.choices[0].message.content)


def _claude_foundry_fix(context: dict) -> dict:
    """Claude on Microsoft Foundry path (in-tenant, MACC-credit eligible)."""
    from anthropic import AnthropicFoundry

    client = AnthropicFoundry(resource=os.environ["FOUNDRY_RESOURCE"])
    resp = client.messages.create(
        model=os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6"),
        max_tokens=8000,
        system=core.SYSTEM_PROMPT,
        messages=[{"role": "user", "content": json.dumps(context)}],
    )
    text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    return core.extract_fix_json(text)
