# Configuration examples

These files are templates, not live configuration. Run setup commands from the repository root and keep edited copies in your private state directory.

| Template | Purpose |
| --- | --- |
| `job-search-config.example.json` | Application runtime, local paths, and scheduling |
| `preference-profile.example.json` | Personal ranking preferences |
| `inference-profile.example.json` | Inference providers and budgets |
| `mail-classifier.example.json` | Recruiter email classification |
| `resume-model.example.json` | Resume generation model |
| `resume-model.runpod.example.json` | Resume generation with Runpod |

See the [runtime guide](../docs/operations/job-search-runtime.md) and [inference guide](../docs/models/inference-providers.md) for setup. Extension-specific examples remain in `extension/`, and deployment-specific templates remain with `infra/` and `deploy/`.
