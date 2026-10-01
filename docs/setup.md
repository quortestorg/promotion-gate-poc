# Setup

Scope: only the prod gate. A person starts `promote-prod`. Its first job pauses on environment
`prod-start` until a required reviewer approves. Then the pipeline renders the prod manifest,
opens a PR and a CAB work item, and starts `prod-gate` on the PR branch. That job sits on the
`prod` environment until the `cab-checker` app confirms the CAB item is approved by the owner
and security roles. It then prints OK. If the gate is cancelled or times out, the CAB item is
set to Rejected. Nothing is deployed.

## 0. Plan constraint

Environment protection rules and rulesets are enforced on public repositories on every plan,
and on private repositories only on Team or Enterprise. This repository is public for that
reason and holds only sample manifests and workflow code.

## 1. Repository settings (once)

| Setting | Value | Why |
| --- | --- | --- |
| Environment `prod-start` > Required reviewers | owner team, DevOps team (any one approves); "Prevent self-review" on once a second account exists | human decision to start, recorded on the run, before anything is created |
| Environment `prod-start` > Deployment branches | Selected branches: `main` | start only from main |
| Environment `prod` > Deployment protection rules | `cab-checker` only, no required reviewers | the CAB check |
| Environment `prod` > Deployment branches and tags | Selected branches: `promote/*` | the gated job runs on the PR branch |
| Both environments > Allow administrators to bypass | off | admins are held too |
| Ruleset on `main` | require PR; required status check `rendered manifest matches sources`; require deployments to succeed: `prod`; no bypass actors | merge stays blocked until the gate passed for the PR head |
| Label | `promotion` | marks promotion PRs |

```bash
REPO=owner/repo; USER_ID=$(gh api user --jq .id)
# gate 1: human start approval, before anything is rendered
gh api -X PUT repos/$REPO/environments/prod-start --input - <<JSON
{"reviewers":[{"type":"User","id":$USER_ID}],"prevent_self_review":false,"can_admins_bypass":false,
 "deployment_branch_policy":{"protected_branches":false,"custom_branch_policies":true}}
JSON
gh api -X POST repos/$REPO/environments/prod-start/deployment-branch-policies -f name='main' -f type=branch
# gate 2: CAB rule only (enable the app rule after installing it, see section 3)
gh api -X PUT repos/$REPO/environments/prod --input - <<'JSON'
{"reviewers":[],"can_admins_bypass":false,"deployment_branch_policy":{"protected_branches":false,"custom_branch_policies":true}}
JSON
gh api -X POST repos/$REPO/environments/prod/deployment-branch-policies -f name='promote/*' -f type=branch
gh api -X POST repos/$REPO/environments/prod/deployment_protection_rules -F integration_id=<APP_ID>
# merge follows the gate automatically
gh api -X PATCH repos/$REPO -F allow_auto_merge=true -F delete_branch_on_merge=true
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
| `APP_ID` | variable | the cab-checker GitHub App id; the workflow mints a token from it to push, open the PR and dispatch the gate |
| `APP_PRIVATE_KEY` | secret | the app's private key (PEM) |
| (app env) `GITHUB_REPO` | app | `owner/repo` the app sends rollback-requested dispatches to |
| `PROMOTION_TOKEN` | secret | optional fallback: fine-grained PAT scoped to this repo (Contents, Pull requests, Actions RW) if the app token is not configured |

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

The CAB item description says it. Review the PR linked on the item, then set **Owner Approver**
or **Security Approver** to yourself. Azure DevOps process rules on the `CAB` type make each
field writable only by members of `CAB owners` / `CAB Security`, freeze the pipeline-written
fields (PR Head SHA, PR URL, Release Tag, AKS manifest) once set, and require both approvers
for state `ReadyForDeploy`. The app releases the gate when both fields are set and the PR head
still equals PR Head SHA, then moves the item to `ReadyForDeploy`. `OWNER_APPROVERS` and
`SECURITY_APPROVERS` are an optional second allowlist checked against the update history.

ADO setup used for the PoC (inherited process `Promotion`, project `cab-poc`): work item type
`CAB` (states New, ReadyForDeploy, Rejected, Closed), identity fields `Custom.OwnerApprover` and
`Custom.SecurityApprover`, text fields `Custom.PRHeadSHA`, `Custom.PRUrl`, `Custom.ReleaseTag`,
`Custom.AKSmanifest`, project groups `CAB owners` and `CAB Security`, rules as above.

## 5. Run it

```bash
gh workflow run promote-prod.yml -R owner/repo -f release_tag=v0.1.0
```

Then, in order:

1. The `promote-prod` run pauses on `prod-start`. A required reviewer approves it ("Review
   deployments"). `GET /repos/owner/repo/actions/runs/<run_id>/approvals` records who.
2. The run renders, opens the PR with the diff and compare links, creates the CAB item, arms
   auto-merge on the PR, starts `prod-gate` on the PR branch, and lists all three links in its
   job summary.
3. The `prod-gate` run pauses on `prod` with the `cab-checker` rule pending; the rule's status
   text carries the CAB link and "Owner: pending, Security: pending".
4. Approvers set Owner Approver and Security Approver on the CAB item. The app approves the
   rule, moves the CAB to ReadyForDeploy, the job prints OK.
5. Auto-merge fires because the `prod` deployment now succeeded for the PR head. `post-merge`
   closes the CAB item and records the merge commit in its history.

Negative paths: cancel the `prod-gate` run and `reject-cab` sets the CAB to Rejected; push a
commit to the promotion branch after approval and the app clears both approvers and rejects
with "Head changed".

## 6. Rollback

1. A member of `CAB owners` moves a Closed CAB to **RollbackRequired** (a process rule disallows
   that value for anyone else).
2. The app's poll finds it, sends `repository_dispatch` `rollback-requested` with the CAB id to
   the repo (`GITHUB_REPO` in the app env), and tags the CAB `RollbackDispatched`.
3. `rollback-prod` reads the CAB's Merge SHA, reverts that merge on `rollback/<tag>`, re-renders,
   opens the PR with the diff, creates a **Rollback** work item (same fields and approver rules,
   linked to the CAB), arms auto-merge and starts `prod-gate` on the branch. No `prod-start`
   pause: the owner's state change is the start decision.
4. Approvers set both fields on the Rollback item; the gate releases; auto-merge lands the
   revert; `post-merge` closes the Rollback item and the original CAB.

Manual start for testing: `gh workflow run rollback-prod.yml -f cab_id=<id>` (the CAB must be in
RollbackRequired).
