"""
Azure DevOps Service Hook handling: decide whether an incoming build event should
be healed, and normalize its payload into the shape orchestrate() expects.

Pure and stdlib-only, so it is unit-tested without any ADO connection. The set of
pipelines to monitor is a config value (MONITORED_PIPELINES) rather than per-pipeline
YAML — one broad "Build completed / status Failed" subscription feeds this gate.
"""

_REF_PREFIX = "refs/heads/"


def parse_allowlist(raw: str) -> set:
    """Parse MONITORED_PIPELINES ('42, 58 103' or '') into a set of id/name strings.

    An empty result means 'monitor everything' — see is_monitored.
    """
    if not raw:
        return set()
    return {tok.strip() for tok in raw.replace(",", " ").split() if tok.strip()}


def is_monitored(pipeline_id, pipeline_name, allowlist: set) -> bool:
    """Is this pipeline in the monitored set? Empty allowlist = monitor all."""
    if not allowlist:
        return True
    return str(pipeline_id) in allowlist or (pipeline_name or "") in allowlist


def is_failed_build(event: dict) -> bool:
    """True only for a completed build whose result is 'failed'."""
    return (event.get("resource") or {}).get("result") == "failed"


def _strip_ref(branch: str) -> str:
    if not branch:
        return "main"
    return branch[len(_REF_PREFIX):] if branch.startswith(_REF_PREFIX) else branch


def normalize_service_hook(event: dict, org_url: str) -> dict:
    """Map an ADO 'build.complete' Service Hook payload to an orchestrate payload.

    `org_url` is supplied from config (ADO_ORG_URL) rather than trusted from the
    event body. Repo/commit/branch/definition come from the build resource.
    """
    resource = event.get("resource") or {}
    definition = resource.get("definition") or {}
    project = resource.get("project") or {}
    repo = resource.get("repository") or {}
    return {
        "org": org_url,
        "project": project.get("name") or project.get("id"),
        "repo": repo.get("id") or repo.get("name"),
        "buildId": resource.get("id"),
        "commit": resource.get("sourceVersion"),
        "branch": _strip_ref(resource.get("sourceBranch")),
        "pipelineId": definition.get("id"),
        "pipelineName": definition.get("name"),
    }
