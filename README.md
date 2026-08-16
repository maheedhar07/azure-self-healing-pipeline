# azure-self-healing-pipeline

AI self-healing CI/CD for **Azure DevOps** pipelines, Azure-native (no n8n).

When a pipeline fails, an Azure Function gathers the failure context (logs, changed files,
source), asks an **LLM** for a minimal fix, and opens a **Pull Request** with it.
The AI never pushes to `main` — a human merge is always the gate.

The model provider is swappable via the `MODEL_PROVIDER` app setting:

| `MODEL_PROVIDER` | Backend | Default model |
|---|---|---|
| `azure_openai` | Azure OpenAI (reasoning model) | `o4-mini` |
| `claude_foundry` | Claude on Microsoft Foundry (in-tenant, MACC-eligible) | `claude-sonnet-4-6` |

Both return the same fix shape, so you can A/B them on the same failures during the pilot.

See **[DESIGN.md](DESIGN.md)** for the full architecture, cost rationale, and guardrails.

## Layout

```
function_app/
  function_app.py          Thin Azure Function HTTP entrypoint
  healer/
    core.py                Pure orchestration — stdlib only, unit-tested
    ado.py                 Azure DevOps REST adapter (lazy `requests`/`azure.identity`)
    providers.py           Model dispatch: azure_openai (o4-mini) | claude_foundry
  tests/
    test_core.py           unittest suite — no creds, no SDKs, all I/O faked
  requirements.txt
  host.json
  local.settings.json.example
pipeline/
  azure-pipelines.yml      Example build pipeline with the failure trigger job
infra/
  main.bicep               One-command infra (Function, Storage, Key Vault, App Insights)
```

## Test

The orchestration logic (failure parsing, file filtering, provider dispatch, loop-breaker,
PR payloads) is tested with fakes for every external call — no Azure/ADO/LLM credentials
and no third-party packages required:

```bash
cd function_app && python3 -m unittest discover -s tests -v
```

The design keeps `healer/core.py` stdlib-only and lazy-imports the provider SDKs, so the
suite runs anywhere Python 3 is installed. It validates the wiring, **not** a live call to
Azure DevOps or the model — that step needs your provisioned resources and credentials.

## Quick start (pilot)

1. **Provision infra** (uses your $150 demo credit; ~$0 on Consumption):
   ```bash
   az group create -n rg-self-healing -l eastus
   az deployment group create -g rg-self-healing -f infra/main.bicep \
     -p namePrefix=selfheal openAiResourceName=<your-aoai> 
   ```
2. **Deploy the function:**
   ```bash
   cd function_app && func azure functionapp publish <function-app-name>
   ```
3. **Wire the trigger:** copy the `notify_self_healer` job from
   `pipeline/azure-pipelines.yml` into the pipeline you want to protect, and set the
   `HEALER_FUNCTION_URL` variable to the deployed function URL.
4. **Set app settings / secrets** — see `function_app/local.settings.json.example`.

## Status

Scaffold / pilot skeleton. The orchestration logic is unit-tested (13 tests, all green).
The ADO REST and model calls are written against the documented APIs but exercised only
through fakes so far — a live run needs your provisioned resources, credentials, and the
`# TODO` items (managed-identity onto the ADO org, required PR reviewers, log scrubbing).
Not production-hardened.
