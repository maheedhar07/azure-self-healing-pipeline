"""
Self-healing CI/CD orchestrator — Azure Function (Python v2 programming model).

Thin HTTP entrypoint. All logic lives in the `healer` package:
  - healer/core.py       pure orchestration (stdlib only, unit-tested)
  - healer/ado.py        Azure DevOps REST adapter
  - healer/providers.py  model dispatch (Azure OpenAI o4-mini | Claude on Foundry)

Flow: HTTP trigger receives a failed-build payload from the pipeline, then
timeline -> failed-step logs -> changed files -> source -> model -> branch +
commit + Pull Request -> notify. The AI only ever opens a PR; it never pushes to
the default branch, and failures on `ai-fix/*` branches are skipped (loop-breaker).
"""

import json
import logging
import os

import azure.functions as func
import requests

from healer import core
from healer.ado import AdoClient
from healer.providers import make_request_fix

app = func.FunctionApp()


def _notify(text: str) -> None:
    hook = os.environ.get("NOTIFY_WEBHOOK_URL")
    if not hook:
        logging.warning("NOTIFY_WEBHOOK_URL not set; skipping notification")
        return
    requests.post(hook, json={"text": text}, timeout=10)


@app.route(route="heal", auth_level=func.AuthLevel.FUNCTION)
def heal(req: func.HttpRequest) -> func.HttpResponse:
    try:
        body = req.get_json()
        org_url = body["org"]            # https://dev.azure.com/<org>/
        project = body["project"]
        repo_id = body["repo"]           # repository id or name
        _ = body["buildId"], body["commit"]
    except (ValueError, KeyError) as e:
        return func.HttpResponse(f"Bad payload: {e}", status_code=400)

    ado = AdoClient(org_url, project, repo_id)
    request_fix = make_request_fix()

    logging.info("Healing build %s on %s", body["buildId"], repo_id)
    result = core.orchestrate(body, ado, request_fix, _notify)

    status = 200 if "skipped" not in result else 200
    return func.HttpResponse(json.dumps(result), status_code=status,
                             mimetype="application/json")
