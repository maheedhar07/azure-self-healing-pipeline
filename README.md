# azure-self-healing-pipeline

AI self-healing CI/CD for **Azure DevOps** pipelines, Azure-native (no n8n).

When a pipeline fails, an Azure Function gathers the failure context (logs, changed files,
source), asks **Azure OpenAI** for a minimal fix, and opens a **Pull Request** with it.
The AI never pushes to `main` — a human merge is always the gate.

See **[DESIGN.md](DESIGN.md)** for the full architecture, cost rationale, and guardrails.

## Layout

```
function_app/            Python (v2 model) Azure Function — the orchestrator
  function_app.py        HTTP trigger + the 8-step heal flow (ADO REST + Azure OpenAI)
  requirements.txt
  host.json
  local.settings.json.example
pipeline/
  azure-pipelines.yml    Example build pipeline with the failure trigger job
infra/
  main.bicep             One-command infra (Function, Storage, Key Vault, App Insights)
```

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

Scaffold / pilot skeleton. REST + Azure OpenAI calls are implemented against the documented
APIs but marked `# TODO` where org-specific auth and config plug in. Not production-hardened.
