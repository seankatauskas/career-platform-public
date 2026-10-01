# GitHub Actions

The canonical `seankatauskas/career-platform` checkout includes active workflows in
`.github/workflows/`: offline/browser checks, AWS checks, and manually dispatched
Terraform/release workflows. AWS execution additionally requires the reviewed
`AWS_WORKFLOW_REF`, production environment settings and scoped OIDC roles described
in [AWS deployment](operations/aws-deployment.md). Merely committing these workflows
does not provision infrastructure or deploy a release.

The public-export tool keeps the credential-free `public-checks.yml` active and
moves deployment and publishing workflows into `.github/workflow-examples/`.
OpenWiki examples require separate configuration and remain inactive.

The private repository publishes daily source snapshots to
[`career-platform-public`](https://github.com/seankatauskas/career-platform-public).
See [public publishing](operations/public-publishing.md) for the export rules,
10 PM Chicago schedule, and recovery procedure.

Run the same application checks locally:

```bash
uv run --with cryptography --with pypdf python scripts/check-system.py --browser
```

Personal runtime files are excluded from Git. The private-file guard examines staged
bytes, and release packaging examines committed bytes, before publication.
