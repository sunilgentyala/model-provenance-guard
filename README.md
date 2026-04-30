# model-provenance-guard

**Cryptographic provenance verification and binary inspection for ML model artifacts in CI/CD pipelines.**

Maintained by [Sunil Gentyala](https://linkedin.com/in/sunil-gentyala) | [github.com/sunilgentyala](https://github.com/sunilgentyala)

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org/)

---

## Why This Exists

GGUF, Safetensors, and PyTorch checkpoint files downloaded from public model hubs represent an active and largely unmonitored supply chain risk in enterprise MLOps. Traditional SAST, DAST, and antivirus tooling cannot inspect binary weight formats. This repository provides a production-ready GitHub Actions workflow and a Python inspection script that enforce cryptographic hash verification, Sigstore/Cosign signature checking, pickle opcode scanning, and Safetensors header anomaly detection before any model artifact is admitted into a downstream pipeline.

This toolkit is the operational companion to the Help Net Security column "Weaponized Weights: The Impending Supply Chain Crisis in GGUF and Safetensors" by Sunil Gentyala.

---

## Repository Structure

```
model-provenance-guard/
├── .github/
│   └── workflows/
│       └── model_scan.yml          # Full CI/CD provenance gate workflow
├── scripts/
│   └── verify_weights.py           # Safetensors header inspector and anomaly detector
├── registry/
│   └── trusted_models.json         # Internal trusted model hash registry (template)
├── docs/
│   └── threat-model.md             # Format-level threat model for GGUF, ST, PT
├── requirements.txt                 # Python dependencies
├── .gitignore
└── README.md
```

---

## What the Pipeline Enforces

| Control | Formats | Tool |
|---|---|---|
| SHA-256 hash verification | All | Python hashlib + trusted_models.json |
| Cosign signature verification | All | sigstore/cosign-installer |
| Pickle opcode scanning | .pt / .pth | picklescan |
| Safetensors header inspection | .safetensors | verify_weights.py |
| GGUF magic byte validation | .gguf | verify_weights.py |
| Artifact quarantine on failure | All | Workflow failure + annotated summary |

---

## Quickstart: Deploying the Workflow

### Prerequisites

- GitHub Actions runner with Python 3.9 or later
- `cosign` available in your runner environment (installed by the workflow via `sigstore/cosign-installer`)
- A `trusted_models.json` registry populated with approved model hashes (see template in `registry/`)

### Step 1: Populate Your Trusted Registry

Edit `registry/trusted_models.json` to include the SHA-256 hash of every model artifact your organization has reviewed and approved:

```json
{
  "models": [
    {
      "name": "mistral-7b-instruct-v0.3",
      "filename": "model.safetensors",
      "sha256": "a1b2c3d4e5f6...",
      "source_url": "https://huggingface.co/mistralai/Mistral-7B-Instruct-v0.3",
      "reviewed_by": "engineer@yourorg.com",
      "reviewed_at": "2025-04-15T10:00:00Z",
      "cosign_bundle": "model.safetensors.bundle"
    }
  ]
}
```

The hash value must be computed by a human reviewer from the artifact at time of initial review, not sourced from the model card on Hugging Face. The registry itself should be protected by branch protection rules requiring at least one reviewer approval on any pull request that modifies it.

### Step 2: Add the Workflow

Copy `.github/workflows/model_scan.yml` into your repository. The workflow triggers on pull requests that modify any path containing model artifacts and also runs on a nightly schedule against your staging model cache.

Set the following repository secrets:

| Secret | Purpose |
|---|---|
| `COSIGN_PUBLIC_KEY` | PEM-encoded public key for verifying model signatures |
| `MODEL_REGISTRY_PATH` | Path to your trusted_models.json in the runner environment |

### Step 3: Run the Header Inspector Locally

```bash
pip install -r requirements.txt
python scripts/verify_weights.py --file /path/to/model.safetensors --registry registry/trusted_models.json
```

For GGUF files:

```bash
python scripts/verify_weights.py --file /path/to/model.gguf --registry registry/trusted_models.json
```

For PyTorch checkpoints, the workflow automatically runs picklescan. You can also invoke it directly:

```bash
pip install picklescan
picklescan -p /path/to/model.pt
```

---

## Threat Model

See [docs/threat-model.md](docs/threat-model.md) for the full format-level attack surface analysis covering pickle deserialization in .pt files, GGUF metadata injection, Safetensors header manipulation, and neural backdoor scenarios.

---

## Integration With Existing MLOps Stacks

The workflow is designed to sit at the model intake boundary: between the artifact download step and the step that registers the model in your internal model registry (MLflow, Weights and Biases, SageMaker Model Registry, or equivalent). Any failure in the provenance gate should block promotion. Configure your orchestration layer (Kubeflow Pipelines, Metaflow, Airflow) to treat a non-zero exit from this workflow as a hard stop, not a warning.

---

## Contributing

Pull requests are welcome for additional format inspectors (ONNX, TensorFlow SavedModel) and for integrations with specific model hub SDKs. Please open an issue first to discuss scope.

---

## License

MIT License. See [LICENSE](LICENSE).

---

## Author

**Sunil Gentyala**
Lead Cybersecurity and AI Security Consultant, HCLTech
IEEE Senior Member | Cloud Security Alliance Representative | ISACA Professional Member
Creator: ContextGuard (zero-trust MCP middleware) | GSH Framework (agentic AI threat hunting)

[linkedin.com/in/sunil-gentyala](https://linkedin.com/in/sunil-gentyala) | [github.com/sunilgentyala](https://github.com/sunilgentyala)
email: sunil.gentyala@ieee.org
