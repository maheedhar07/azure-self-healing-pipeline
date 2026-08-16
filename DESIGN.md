# Self-Healing CI/CD Pipeline — Azure-Native Design

Azure-native adaptation of the pattern shown in freeCodeCamp's *"Build a Self-Healing
CI/CD Pipeline with AI"* (GitHub Actions + n8n + OpenAI). We drop n8n (not available
in-company) and replace it with an **Azure Function App**, keep everything in the Azure
tenant via **Azure OpenAI**, and target **Azure DevOps Pipelines**.

## Core principle

> The AI **diagnoses** a failure and **proposes** a fix as a Pull Request.
> It never pushes to `main`. A human merge is always the gate.

## Architecture

```
Azure DevOps Pipeline (build/test on push)
        │  on failure
        ▼
[Trigger]  in-pipeline failed() task  ──POST──►  Azure Function (HTTP)
   (pilot)                                              │
        or                                              │  orchestrates:
[Service Hook: Build failed] ─► Storage Queue ─► Queue-triggered Function
   (scale)                                              │
                                                        ▼
   1. ADO REST: Get build Timeline   → find failed task(s)
   2. ADO REST: Get Build Log        → tail failed step's log
   3. ADO REST: Get Commit Changes   → files in the offending commit
   4. ADO REST: Get Item (Git)       → source of the changed code files
   5. Build structured prompt        (in-function code)
   6. LLM: chat completion           → minimal proposed fix
        MODEL_PROVIDER = azure_openai (o4-mini)  |  claude_foundry (Claude Sonnet 4.6)
   7. ADO Git REST: create branch → commit fix → create Pull Request
   8. Notify: Teams/Slack incoming webhook
```

## Model choice (provider-swappable)

`request_fix()` dispatches on the `MODEL_PROVIDER` app setting so you can A/B providers on
the same failures. This is a code-diagnosis-and-fix task (SWE-bench-shaped), so model quality
matters — **GPT-4o-mini is deliberately not the default.**

| `MODEL_PROVIDER` | Backend | Default | Why |
|---|---|---|---|
| `azure_openai` | Azure OpenAI | `o4-mini` | Reasoning model; strong on debugging, stays in Azure OpenAI. |
| `claude_foundry` | Claude on Microsoft Foundry | `claude-sonnet-4-6` | SWE-bench leader family; in-tenant via Foundry, MACC-credit eligible. Escalate to `claude-opus-4-8` for the hardest fixes. |

Only the selected provider's SDK is imported at runtime (lazy import), and switching is a
single app-setting change — no redeploy of code.

## Orchestrator choice

| Option | Verdict |
|---|---|
| **Function App (Consumption)** ✅ | Chosen. Code-heavy workflow, ~$0 for a demo, ports cleanly to the company subscription via Bicep. |
| Logic Apps Consumption | Viable but per-action metering + awkward for the parsing/prompt-building code. |
| Logic Apps Standard | ~$175/mo hosting base — exceeds the $150 demo credit. Rejected. |

Pilot = single HTTP-triggered Function. Graduate to **Durable Functions** (orchestrator +
activity functions) as steps grow and you want built-in retry/state/replay.

## Trigger choice

- **Pilot (one pipeline):** in-pipeline job with `condition: failed()` that POSTs the build
  context to `/api/heal`. Self-contained, no webhook infra. See `pipeline/azure-pipelines.yml`.
- **Monitor a set of pipelines:** one project-wide ADO **Service Hook** ("Build completed",
  status = Failed) → `/api/hook`. The Function normalizes the event and gates on
  `MONITORED_PIPELINES` (comma/space list of pipeline definition IDs or names; empty = all).
  Register it with `scripts/register_service_hook.sh`. No per-pipeline YAML edits — the
  monitored set is an app setting you change without redeploying.

  A "Build completed" hook filters to *one* pipeline or *all*, so the allowlist-in-Function
  is how you express "these N". (Alternative: one subscription per pipeline with
  `definitionName` set — precise, but N subscriptions to manage.)
- **Scale further (durability):** point the same Service Hook at a **Storage Queue** instead
  of the HTTP endpoint, consumed by a queue-triggered Function — adds retry + dead-letter.

## Prerequisites

**Infra (all in one resource group, deployed by `infra/main.bicep`)**
- Azure subscription + resource group
- Function App on **Consumption (Y1)** plan + its Storage account
- One model backend: **Azure OpenAI** resource + an `o4-mini` deployment, **or** Claude on
  **Microsoft Foundry** (`claude-sonnet-4-6`) — selected by `MODEL_PROVIDER`
- **Key Vault** for secrets
- **Application Insights** — the audit log of every prompt/response (non-optional)

**Auth (prefer PAT-free)**
- Function App **system-assigned managed identity**.
- Add that identity to the Azure DevOps org (Entra-backed) with: Build (read),
  Code (read/write), Pull Request (read/write).
- Acquire an Entra token for the Azure DevOps resource id
  `499b84ac-1321-427f-aa17-267ca6975798` — no PAT to rotate.
- Fallback if Entra integration isn't available yet: a **service-account PAT** in Key Vault
  (never a personal PAT).
- Azure OpenAI / Foundry: managed identity, or key in Key Vault.

## Guardrails (do these before widening scope)

1. **PR-only output.** AI never merges. Auto-assign a required reviewer on the generated PR.
2. **Narrow failure class first.** Start with lint / format / dependency-pin failures — not
   "fix any build." Scope creep is where these systems get unreliable.
3. **Loop-breaker.** Tag AI-generated PRs (branch prefix `ai-fix/`, PR label) and ensure
   their CI runs never re-trigger the healer. Prevents infinite retry loops.
4. **Secret hygiene.** Build logs routinely contain tokens/URLs. Scrub before sending to the
   model; scope what source you attach. Azure OpenAI keeps it in-tenant.
5. **Audit everything.** Log prompt + response + resulting PR to App Insights.

## Rollout

Pilot on **one low-risk repo, one narrow failure class**. Prove it over a few weeks, then:
Consumption Function → (optional) Durable Functions, and in-pipeline trigger → queue-backed
Service Hook. Redeploy the Bicep into the company subscription when approved.
