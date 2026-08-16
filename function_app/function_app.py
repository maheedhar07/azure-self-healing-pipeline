"""
Self-healing CI/CD orchestrator — Azure Function (Python v2 programming model).

Thin HTTP entrypoint. All logic lives in the `healer` package:
  - healer/core.py       pure orchestration (stdlib only, unit-tested)
  - healer/ado.py        Azure DevOps REST adapter
  - healer/events.py     Service Hook payload normalization + monitored-set gate
  - healer/providers.py  model dispatch (Azure OpenAI o4-mini | Claude on Foundry)

Two triggers, both ending in core.orchestrate():
  POST /api/heal  — direct call from an in-pipeline `failed()` task (pilot / manual).
  POST /api/hook  — Azure DevOps Service Hook ("Build completed", status Failed),
                    monitoring a *set* of pipelines via MONITORED_PIPELINES.

The AI only ever opens a PR; it never pushes to the default branch, and failures on
`ai-fix/*` branches are skipped (loop-breaker).
"""

import json
import logging
import os

import azure.functions as func
import requests

from healer import core, events
from healer.ado import AdoClient
from healer.providers import make_request_fix

app = func.FunctionApp()


def _notify(text: str) -> None:
    hook = os.environ.get("NOTIFY_WEBHOOK_URL")
    if not hook:
        logging.warning("NOTIFY_WEBHOOK_URL not set; skipping notification")
        return
    requests.post(hook, json={"text": text}, timeout=10)


def _run(payload: dict) -> dict:
    ado = AdoClient(payload["org"], payload["project"], payload["repo"])
    return core.orchestrate(payload, ado, make_request_fix(), _notify)


def _json(result: dict, status: int = 200) -> func.HttpResponse:
    return func.HttpResponse(json.dumps(result), status_code=status,
                             mimetype="application/json")


@app.route(route="heal", auth_level=func.AuthLevel.FUNCTION)
def heal(req: func.HttpRequest) -> func.HttpResponse:
    """Direct trigger: the in-pipeline failed() task POSTs the build context."""
    try:
        body = req.get_json()
        _ = body["org"], body["project"], body["repo"], body["buildId"], body["commit"]
    except (ValueError, KeyError) as e:
        return func.HttpResponse(f"Bad payload: {e}", status_code=400)

    logging.info("Healing build %s on %s", body["buildId"], body["repo"])
    return _json(_run(body))


@app.route(route="hook", auth_level=func.AuthLevel.FUNCTION)
def hook(req: func.HttpRequest) -> func.HttpResponse:
    """Service Hook trigger: heal monitored pipelines' failed builds project-wide."""
    try:
        event = req.get_json()
    except ValueError as e:
        return func.HttpResponse(f"Bad payload: {e}", status_code=400)

    if not events.is_failed_build(event):
        return _json({"skipped": "not-a-failed-build"})

    org_url = os.environ.get("ADO_ORG_URL")
    if not org_url:
        logging.error("ADO_ORG_URL app setting is required for the /hook trigger")
        return _json({"error": "ADO_ORG_URL not configured"}, status=500)

    payload = events.normalize_service_hook(event, org_url)
    allowlist = events.parse_allowlist(os.environ.get("MONITORED_PIPELINES", ""))
    if not events.is_monitored(payload["pipelineId"], payload["pipelineName"], allowlist):
        logging.info("Pipeline %s (%s) not in monitored set; skipping.",
                     payload["pipelineId"], payload["pipelineName"])
        return _json({"skipped": "not-monitored", "pipeline": payload["pipelineName"]})

    logging.info("Healing build %s from pipeline %s", payload["buildId"],
                 payload["pipelineName"])
    return _json(_run(payload))
