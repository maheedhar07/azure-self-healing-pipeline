"""
Self-healing CI/CD orchestrator — Azure Function (Python v2 programming model).

Flow (see DESIGN.md):
  HTTP trigger receives a failed-build payload from the Azure DevOps pipeline, then:
    1. get build timeline      -> find the failed task(s)
    2. get the failed step log -> tail it
    3. get commit changes      -> files in the offending commit
    4. get file content        -> source of changed code files
    5. build a structured prompt
    6. call Azure OpenAI        -> minimal proposed fix (per-file new content)
    7. create branch -> commit fix -> open Pull Request
    8. notify the team (Teams/Slack incoming webhook)

The AI only ever opens a PR. It never pushes to the default branch.
Auth: prefer the Function's managed identity for both Azure DevOps and Azure OpenAI.
      A service-account PAT (from Key Vault) is the fallback for Azure DevOps.
"""

import base64
import json
import logging
import os

import azure.functions as func
import requests
from azure.identity import DefaultAzureCredential
from openai import AzureOpenAI

app = func.FunctionApp()

# Azure DevOps resource id for Entra token acquisition (constant, org-independent).
ADO_RESOURCE_ID = "499b84ac-1321-427f-aa17-267ca6975798"
API_VERSION = "7.1"
AI_BRANCH_PREFIX = "ai-fix/"  # loop-breaker: CI must ignore branches under this prefix
CODE_EXTENSIONS = (".js", ".ts", ".py", ".json", ".yml", ".yaml", ".sh", ".java",
                   ".go", ".rb", ".cs", ".tf", ".bicep")

_credential = DefaultAzureCredential()


# --------------------------------------------------------------------------- auth
def ado_headers() -> dict:
    """Authorization header for Azure DevOps REST.

    Prefer the managed identity (PAT-free). Fall back to a service-account PAT if
    ADO_PAT is set (inject it from Key Vault via app settings, never a personal PAT).
    """
    pat = os.environ.get("ADO_PAT")
    if pat:
        token = base64.b64encode(f":{pat}".encode()).decode()
        return {"Authorization": f"Basic {token}"}
    aad_token = _credential.get_token(f"{ADO_RESOURCE_ID}/.default").token
    return {"Authorization": f"Bearer {aad_token}"}


def openai_client() -> AzureOpenAI:
    return AzureOpenAI(
        azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
        api_version="2024-10-21",
        # Uses managed identity; swap for api_key=... only if you must.
        azure_ad_token_provider=lambda: _credential.get_token(
            "https://cognitiveservices.azure.com/.default"
        ).token,
    )


# ------------------------------------------------------------------- ADO REST calls
def _org_base(org_url: str, project: str) -> str:
    # org_url comes in as e.g. https://dev.azure.com/mycompany/
    return f"{org_url.rstrip('/')}/{project}/_apis"


def get_failed_steps(org_url: str, project: str, build_id: str) -> list[dict]:
    """Step 1: build timeline -> records that failed."""
    url = f"{_org_base(org_url, project)}/build/builds/{build_id}/timeline"
    r = requests.get(url, headers=ado_headers(), params={"api-version": API_VERSION})
    r.raise_for_status()
    records = r.json().get("records", [])
    return [rec for rec in records
            if rec.get("result") == "failed" and rec.get("type") == "Task"]


def get_log_tail(org_url: str, project: str, build_id: str, log_id: int,
                 tail_lines: int = 120) -> str:
    """Step 2: fetch a step's log and keep the tail (where the error usually is)."""
    url = f"{_org_base(org_url, project)}/build/builds/{build_id}/logs/{log_id}"
    r = requests.get(url, headers=ado_headers(), params={"api-version": API_VERSION})
    r.raise_for_status()
    return "\n".join(r.text.splitlines()[-tail_lines:])


def get_changed_files(org_url: str, project: str, repo_id: str, commit: str) -> list[str]:
    """Step 3: files touched by the offending commit (code files only, excluding deletes)."""
    url = f"{_org_base(org_url, project)}/git/repositories/{repo_id}/commits/{commit}/changes"
    r = requests.get(url, headers=ado_headers(), params={"api-version": API_VERSION})
    r.raise_for_status()
    out = []
    for change in r.json().get("changes", []):
        if change.get("changeType") == "delete":
            continue
        path = change.get("item", {}).get("path", "")
        if path.endswith(CODE_EXTENSIONS):
            out.append(path)
    return out


def get_file_content(org_url: str, project: str, repo_id: str, path: str,
                     branch: str) -> str:
    """Step 4: current source of a file on the failing branch."""
    url = f"{_org_base(org_url, project)}/git/repositories/{repo_id}/items"
    r = requests.get(url, headers=ado_headers(), params={
        "path": path,
        "versionDescriptor.version": branch,
        "versionDescriptor.versionType": "branch",
        "includeContent": "true",
        "api-version": API_VERSION,
    })
    r.raise_for_status()
    return r.json().get("content", "")


# ---------------------------------------------------------------------- AI + remediation
SYSTEM_PROMPT = (
    "You are a senior engineer diagnosing an Azure DevOps CI/CD pipeline failure. "
    "You are given the failed step logs and the source of the files changed in the "
    "offending commit. Identify the smallest change that fixes the failure. "
    "Respond ONLY as JSON: "
    '{"summary": "<one line>", "files": [{"path": "<repo path>", "content": "<full new file content>"}]}. '
    "Do not modify files unrelated to the failure. If you cannot determine a safe fix, "
    'return {"summary": "no confident fix", "files": []}.'
)


def request_fix(context: dict) -> dict:
    """Steps 5-6: build the prompt and ask Azure OpenAI for a minimal fix."""
    client = openai_client()
    resp = client.chat.completions.create(
        model=os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4o-mini"),
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(context)},
        ],
    )
    return json.loads(resp.choices[0].message.content)


def open_fix_pr(org_url: str, project: str, repo_id: str, base_branch: str,
                commit: str, fix: dict) -> str | None:
    """Step 7: create branch -> push commit with the fix -> open a PR. Returns PR URL."""
    if not fix.get("files"):
        return None
    base = _org_base(org_url, project)
    new_branch = f"{AI_BRANCH_PREFIX}{commit[:8]}"

    # Create the branch off the failing commit via a push with the changed files.
    changes = [{
        "changeType": "edit",
        "item": {"path": f["path"]},
        "newContent": {"content": f["content"], "contentType": "rawtext"},
    } for f in fix["files"]]

    push = requests.post(
        f"{base}/git/repositories/{repo_id}/pushes",
        headers={**ado_headers(), "Content-Type": "application/json"},
        params={"api-version": API_VERSION},
        json={
            "refUpdates": [{"name": f"refs/heads/{new_branch}", "oldObjectId": commit}],
            "commits": [{"comment": f"AI fix: {fix['summary']}", "changes": changes}],
        },
    )
    push.raise_for_status()

    pr = requests.post(
        f"{base}/git/repositories/{repo_id}/pullrequests",
        headers={**ado_headers(), "Content-Type": "application/json"},
        params={"api-version": API_VERSION},
        json={
            "sourceRefName": f"refs/heads/{new_branch}",
            "targetRefName": f"refs/heads/{base_branch}",
            "title": f"[AI self-heal] {fix['summary']}",
            "description": (
                f"Automated fix proposed for a pipeline failure on commit `{commit[:8]}`.\n\n"
                f"**Summary:** {fix['summary']}\n\n"
                "> Generated by the self-healing pipeline. Review before merging."
            ),
            # TODO: add required reviewers so the PR can't be ignored.
        },
    )
    pr.raise_for_status()
    body = pr.json()
    return (f"{org_url.rstrip('/')}/{project}/_git/{repo_id}/pullrequest/"
            f"{body['pullRequestId']}")


def notify(text: str) -> None:
    """Step 8: post to a Teams/Slack incoming webhook."""
    hook = os.environ.get("NOTIFY_WEBHOOK_URL")
    if not hook:
        logging.warning("NOTIFY_WEBHOOK_URL not set; skipping notification")
        return
    requests.post(hook, json={"text": text}, timeout=10)


# ------------------------------------------------------------------------- entrypoint
@app.route(route="heal", auth_level=func.AuthLevel.FUNCTION)
def heal(req: func.HttpRequest) -> func.HttpResponse:
    try:
        body = req.get_json()
        org_url = body["org"]           # https://dev.azure.com/<org>/
        project = body["project"]
        build_id = str(body["buildId"])
        repo_id = body["repo"]          # repository id or name
        commit = body["commit"]
        branch = body.get("branch", "main")
    except (ValueError, KeyError) as e:
        return func.HttpResponse(f"Bad payload: {e}", status_code=400)

    # Loop-breaker: never heal a failure that came from an AI branch.
    if branch.startswith(AI_BRANCH_PREFIX) or f"refs/heads/{AI_BRANCH_PREFIX}" in branch:
        logging.info("Failure is on an AI branch; skipping to avoid a heal loop.")
        return func.HttpResponse("Skipped (AI branch).", status_code=200)

    logging.info("Healing build %s on %s@%s", build_id, repo_id, commit[:8])

    failed = get_failed_steps(org_url, project, build_id)
    logs = "\n\n".join(
        f"### {s.get('name')}\n{get_log_tail(org_url, project, build_id, s['log']['id'])}"
        for s in failed if s.get("log")
    )
    changed = get_changed_files(org_url, project, repo_id, commit)
    sources = {p: get_file_content(org_url, project, repo_id, p, branch) for p in changed}

    context = {
        "buildId": build_id,
        "failedSteps": [s.get("name") for s in failed],
        "logs": logs,
        "changedFiles": sources,
    }

    fix = request_fix(context)
    pr_url = open_fix_pr(org_url, project, repo_id, branch, commit, fix)

    if pr_url:
        notify(f"🔧 Self-healing PR opened for build {build_id}: {fix['summary']}\n{pr_url}")
        return func.HttpResponse(json.dumps({"pr": pr_url, "summary": fix["summary"]}),
                                 mimetype="application/json")

    notify(f"⚠️ Build {build_id} failed; AI had no confident fix. Manual review needed.")
    return func.HttpResponse(json.dumps({"pr": None, "summary": fix.get("summary")}),
                             mimetype="application/json")
