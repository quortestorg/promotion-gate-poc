# Setup

Scope: only the prod gate. A person starts `promote-prod`, the pipeline renders the prod
manifest, opens a PR and a CAB work item, and starts `prod-gate` on the PR branch. That job
sits on the `prod` environment until the required reviewers approve in GitHub and the
`cab-checker` app confirms the CAB item is approved by the owner and security roles. It then
prints OK. Nothing is deployed.

## 0. Plan constraint

Environment protection rules and rulesets are enforced on public repositories on every plan,
and on private repositories only on Team or Enterprise. This repository is public for that
reason and holds only sample manifests and workflow code.

## 1. Repository settings (once)

| Setting | Value | Why |
| --- | --- | --- |
| Environment `prod` > Required reviewers | owner team, DevOps team (any one approves); "Prevent self-review" on once a second account exists | human approval, recorded on the run |
| Environment `prod` > Deployment branches and tags | Selected branches: `promote/*` | the gated job runs on the PR branch |
| Environment `prod` > Allow administrators to bypass | off | admins are held too |
| Environment `prod` > Deployment protection rules | enable `cab-checker` after the app is installed | the CAB check |
| Ruleset on `main` | require PR; required status check `rendered manifest matches sources`; require deployments to succeed: `prod`; no bypass actors | merge stays blocked until the gate passed for the PR head |
| Label | `promotion` | marks promotion PRs |

```bash
REPO=owner/repo; USER_ID=$(gh api user --jq .id)
gh api -X PUT repos/$REPO/environments/prod --input - <<JSON
{"reviewers":[{"type":"User","id":$USER_ID}],"prevent_self_review":false,"can_admins_bypass":false,
 "deployment_branch_policy":{"protected_branches":false,"custom_branch_policies":true}}
JSON
gh api -X POST repos/$REPO/environments/prod/deployment-branch-policies -f name='promote/*' -f type=branch
gh label create promotion -R $REPO -c 0E8A16 -d "prod promotion PR"
```

## 2. Repository variables and secrets

| Name | Kind | Value |
| --- | --- | --- |
| `ADO_ORG_URL` | variable | `https://dev.azure.com/<org>` |
| `ADO_PROJECT` | variable | project that holds CAB items |
| `ADO_AREA_PATH` | variable | area path for CAB items (optional) |
| `ADO_WORK_ITEM_TYPE` | variable | default `User Story` |
| `ADO_PAT` | secret | PAT with Work Items read and write |

Without `ADO_ORG_URL` the workflow still opens the PR and starts the gate, but no CAB item is
created and the app reports "must reference exactly one CAB item" until one is linked by hand
(`AB#<id>` in the PR body).

## 3. cab-checker app

1. Create the GitHub App from `cab-checker/app-manifest.json` (Settings > Developer settings >
   GitHub Apps > New GitHub App). Set the webhook URL to your endpoint; on a laptop use a
   `smee.io` channel and run `npx smee-client -u <channel> -t http://localhost:8080/webhook`.
   Generate a private key, note the App ID and the webhook secret.
2. Install the app on the repository.
3. Run the service:

   ```bash
   cd cab-checker
   cp .env.example .env   # fill in
   pip install -r requirements.txt
   set -a; . ./.env; set +a; python app.py
   # or: docker build -t cab-checker . && docker run --env-file .env -p 8080:8080 cab-checker
   ```

4. On environment `prod`, enable the `cab-checker` deployment protection rule.
5. Optional: an Azure DevOps service hook (Project settings > Service hooks > Web Hooks, event
   "Work item updated") pointing at `/ado-hook` releases the gate immediately instead of at
   the next poll.

## 4. How approvers act

The CAB item description says it. Review the PR linked on the item, then add the tag
`CAB-Approved-Owner` or `CAB-Approved-Security`. The app reads the item's update history to
see who added each tag and compares against `OWNER_APPROVERS` and `SECURITY_APPROVERS`. In a
real build these become two fields with group-scoped write rules instead of tags.

## 5. Run it

```bash
gh workflow run promote-prod.yml -R owner/repo -f release_tag=v0.1.0
```

Then: the PR appears with the rendered diff; the CAB item appears in Azure DevOps; the
`prod-gate` run shows "Waiting for review" with the required reviewers and a pending
`cab-checker` rule carrying the app's status report. Approve as reviewer, add both tags in
Azure DevOps, watch the job release and print OK. The run's Deployments tab and
`GET /repos/owner/repo/actions/runs/<run_id>/approvals` show who approved.

Push another commit to the promotion branch after approving and the app clears the tags and
rejects with "Head changed".
