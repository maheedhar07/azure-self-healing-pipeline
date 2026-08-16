"""
Azure DevOps REST adapter. Third-party imports (`requests`, `azure.identity`) are
lazy so this module can be imported for reference without them installed; the
pure orchestration in core.py never imports this file.
"""

import base64
import os

from . import core

ADO_RESOURCE_ID = "499b84ac-1321-427f-aa17-267ca6975798"  # Entra app id for Azure DevOps
API_VERSION = "7.1"


def ado_headers() -> dict:
    """Authorization header for Azure DevOps REST.

    Prefer the Function's managed identity (PAT-free). Fall back to a
    service-account PAT if ADO_PAT is set (inject from Key Vault, never personal).
    """
    pat = os.environ.get("ADO_PAT")
    if pat:
        token = base64.b64encode(f":{pat}".encode()).decode()
        return {"Authorization": f"Basic {token}"}
    from azure.identity import DefaultAzureCredential
    aad = DefaultAzureCredential().get_token(f"{ADO_RESOURCE_ID}/.default").token
    return {"Authorization": f"Bearer {aad}"}


class AdoClient:
    """Thin wrapper over the ADO REST endpoints the orchestrator needs."""

    def __init__(self, org_url: str, project: str, repo_id: str,
                 headers_provider=ado_headers):
        self.base = f"{org_url.rstrip('/')}/{project}/_apis"
        self.org_url = org_url.rstrip("/")
        self.project = project
        self.repo_id = repo_id
        self._headers_provider = headers_provider

    # -- internal helpers ---------------------------------------------------
    def _get(self, url, **params):
        import requests
        params.setdefault("api-version", API_VERSION)
        r = requests.get(url, headers=self._headers_provider(), params=params)
        r.raise_for_status()
        return r

    def _post(self, url, body):
        import requests
        r = requests.post(url, headers={**self._headers_provider(),
                                        "Content-Type": "application/json"},
                          params={"api-version": API_VERSION}, json=body)
        r.raise_for_status()
        return r

    # -- orchestrator interface --------------------------------------------
    def get_failed_steps(self, build_id):
        url = f"{self.base}/build/builds/{build_id}/timeline"
        return core.parse_failed_steps(self._get(url).json().get("records", []))

    def get_log_tail(self, build_id, log_id, tail_lines=120):
        url = f"{self.base}/build/builds/{build_id}/logs/{log_id}"
        return "\n".join(self._get(url).text.splitlines()[-tail_lines:])

    def get_changed_files(self, commit):
        url = f"{self.base}/git/repositories/{self.repo_id}/commits/{commit}/changes"
        return self._get(url).json().get("changes", [])

    def get_file_content(self, path, branch):
        url = f"{self.base}/git/repositories/{self.repo_id}/items"
        return self._get(
            url, path=path,
            **{"versionDescriptor.version": branch,
               "versionDescriptor.versionType": "branch",
               "includeContent": "true"},
        ).json().get("content", "")

    def open_fix_pr(self, base_branch, base_commit, new_branch, files, summary):
        push = core.build_push_payload(new_branch, base_commit, files, summary)
        self._post(f"{self.base}/git/repositories/{self.repo_id}/pushes", push)
        pr = core.build_pr_payload(new_branch, base_branch, summary, base_commit)
        # TODO: add required reviewers so the PR can't be silently ignored.
        body = self._post(
            f"{self.base}/git/repositories/{self.repo_id}/pullrequests", pr).json()
        return (f"{self.org_url}/{self.project}/_git/{self.repo_id}/pullrequest/"
                f"{body['pullRequestId']}")
