# Prod promotion gate: proof of concept

Proves a CAB-gated promotion to a `prod` environment using GitHub-native pieces (Environments,
deployment protection rules, rulesets) plus one small GitHub App that reads the CAB record in
Azure DevOps. Nothing is deployed anywhere; the gated job prints OK when the gate passes.

```text
promote-prod (manual)  ->  PR with rendered manifest + CAB work item  ->  prod-gate on the PR branch
                                                                           |
                                              environment `prod` holds the job until:
                                                required reviewers approve (GitHub)
                                                cab-checker sees both CAB approvals + same head (ADO)
                                                                           |
                                                                        prints OK
```

| Path | Role |
| --- | --- |
| `environments/base`, `environments/prod` | a small sample app (Deployment, Service, ConfigMap) with a prod overlay |
| `rendered/prod.yaml` | normalized `kustomize build` of the prod overlay: the artifact the CAB reviews |
| `tools/ci/render-prod.sh`, `tools/ci/normalize-render.py` | the only writer of `rendered/prod.yaml`; `--check` is the PR status check |
| `.github/workflows/promote-prod.yml` | manual start: set release tag, render, open PR, create CAB item, start the gate |
| `.github/workflows/prod-gate.yml` | the gated job on environment `prod`; prints OK when released |
| `.github/workflows/render-check.yml` | required check: rendered file matches sources |
| `cab-checker/` | GitHub App implementing the custom deployment protection rule |
| `docs/setup.md` | one-time setup and how to run |
