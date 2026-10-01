"""cab-checker: a GitHub App that implements a custom deployment protection rule.

When a workflow job targets an environment that has this rule enabled, GitHub sends a
`deployment_protection_rule` webhook and holds the job. This service decides whether to
release it by reading the CAB work item in Azure DevOps that the pull request links to.

Approval conditions (all must hold):
  1. The PR body references exactly one CAB item (`AB#<id>`).
  2. The CAB item's identity fields `Custom.OwnerApprover` and `Custom.SecurityApprover` are
     both set. Azure DevOps process rules make each field writable only by its group (CAB
     owners, CAB Security), so the field value is the authorization. The app additionally
     reads the work item's update history to record who set each field and, when
     OWNER_APPROVERS / SECURITY_APPROVERS are configured, checks them against those lists.
  3. The `Custom.PRHeadSHA` field equals the commit the gated job runs on, which
     promote-prod.yml arranged to be the PR head. A newer push therefore invalidates the
     approval; in that case both approver fields are cleared and the deployment is rejected
     with a reason.
  When approved, the app also moves the CAB item to state ReadyForDeploy.

Until the conditions hold the deployment stays pending. The service re-evaluates every
POLL_SECONDS and whenever Azure DevOps calls /ado-hook (a service hook on workitem.updated),
and posts a status report on the deployment only when the message changes, since GitHub
allows at most ten reports per deployment.

Environment variables:
  GITHUB_APP_ID, GITHUB_APP_PRIVATE_KEY (PEM) or GITHUB_APP_PRIVATE_KEY_FILE, GITHUB_WEBHOOK_SECRET
  ADO_ORG_URL (https://dev.azure.com/<org>), ADO_PAT (Work Items read + write)
  OWNER_APPROVERS, SECURITY_APPROVERS (comma-separated emails, case-insensitive)
  POLL_SECONDS (default 60), PORT (default 8080), GITHUB_API (default https://api.github.com)
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field

import jwt
import requests
from flask import Flask, abort, jsonify, request

log = logging.getLogger("cab-checker")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

GITHUB_API = os.environ.get("GITHUB_API", "https://api.github.com")
APP_ID = os.environ.get("GITHUB_APP_ID", "")
WEBHOOK_SECRET = os.environ.get("GITHUB_WEBHOOK_SECRET", "").encode()
ADO_ORG_URL = os.environ.get("ADO_ORG_URL", "").rstrip("/")
ADO_PAT = os.environ.get("ADO_PAT", "")
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "60"))
F_OWNER = "Custom.OwnerApprover"
F_SECURITY = "Custom.SecurityApprover"
F_SHA = "Custom.PRHeadSHA"
APPROVED_STATE = os.environ.get("CAB_APPROVED_STATE", "ReadyForDeploy")
ENV_NAME_FILTER = os.environ.get("ENVIRONMENT_NAME", "prod")


def _approvers(var: str) -> set[str]:
    return {x.strip().lower() for x in os.environ.get(var, "").split(",") if x.strip()}


OWNER_APPROVERS = _approvers("OWNER_APPROVERS")
SECURITY_APPROVERS = _approvers("SECURITY_APPROVERS")


def _private_key() -> str:
    if os.environ.get("GITHUB_APP_PRIVATE_KEY_FILE"):
        with open(os.environ["GITHUB_APP_PRIVATE_KEY_FILE"], encoding="utf-8") as f:
            return f.read()
    return os.environ.get("GITHUB_APP_PRIVATE_KEY", "").replace("\\n", "\n")


# ---------------------------------------------------------------- GitHub App auth


def app_jwt() -> str:
    now = int(time.time())
    return jwt.encode({"iat": now - 60, "exp": now + 9 * 60, "iss": APP_ID}, _private_key(), algorithm="RS256")


_token_cache: dict[int, tuple[str, float]] = {}


def installation_token(installation_id: int) -> str:
    tok = _token_cache.get(installation_id)
    if tok and tok[1] - time.time() > 120:
        return tok[0]
    r = requests.post(
        f"{GITHUB_API}/app/installations/{installation_id}/access_tokens",
        headers={"Authorization": f"Bearer {app_jwt()}", "Accept": "application/vnd.github+json"},
        timeout=20,
    )
    r.raise_for_status()
    body = r.json()
    # expires_at is ISO-8601; tokens live one hour. Cache for 55 minutes.
    _token_cache[installation_id] = (body["token"], time.time() + 55 * 60)
    return body["token"]


def gh(installation_id: int, method: str, url: str, **kw) -> requests.Response:
    if url.startswith("/"):
        url = GITHUB_API + url
    headers = {
        "Authorization": f"Bearer {installation_token(installation_id)}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    return requests.request(method, url, headers=headers, timeout=30, **kw)


# ---------------------------------------------------------------- Azure DevOps


def ado(method: str, path: str, **kw) -> requests.Response:
    headers = {"Authorization": "Basic " + base64.b64encode((":" + ADO_PAT).encode()).decode()}
    headers.update(kw.pop("headers", {}))
    sep = "&" if "?" in path else "?"
    return requests.request(method, f"{ADO_ORG_URL}{path}{sep}api-version=7.1", headers=headers, timeout=30, **kw)


def ado_work_item(wid: int) -> dict:
    r = ado("GET", f"/_apis/wit/workitems/{wid}?fields=System.State,System.Title,{F_OWNER},{F_SECURITY},{F_SHA}")
    r.raise_for_status()
    return r.json()


def ado_field_setters(wid: int) -> dict[str, str]:
    """Return {field: email-of-last-person-who-set-it-to-a-value} from the update history."""
    r = ado("GET", f"/_apis/wit/workitems/{wid}/updates?$top=200")
    r.raise_for_status()
    setters: dict[str, str] = {}
    for upd in r.json().get("value", []):
        who = ((upd.get("revisedBy") or {}).get("uniqueName") or "").lower()
        for f in (F_OWNER, F_SECURITY):
            change = (upd.get("fields") or {}).get(f)
            if not change:
                continue
            if change.get("newValue"):
                setters[f] = who
            else:
                setters.pop(f, None)
    return setters


def identity_name(v) -> str:
    if isinstance(v, dict):
        return (v.get("uniqueName") or v.get("displayName") or "").lower()
    return str(v or "").lower()


def ado_patch(wid: int, ops: list[dict]) -> requests.Response:
    r = ado("PATCH", f"/_apis/wit/workitems/{wid}", headers={"Content-Type": "application/json-patch+json"}, data=json.dumps(ops))
    if not r.ok:
        log.warning("ADO patch %s -> %s %s", wid, r.status_code, r.text[:300])
    return r


def ado_clear_approvals(wid: int) -> None:
    ado_patch(wid, [{"op": "add", "path": f"/fields/{F_OWNER}", "value": ""}, {"op": "add", "path": f"/fields/{F_SECURITY}", "value": ""}])


# ---------------------------------------------------------------- pending deployments


@dataclass
class Pending:
    callback_url: str
    installation_id: int
    repo: str
    sha: str
    environment: str
    pr_numbers: list[int]
    created: float = field(default_factory=time.time)
    last_report: str = ""
    cab_id: int | None = None
    done: bool = False


PENDING: dict[str, Pending] = {}
LOCK = threading.Lock()


def find_pr(p: Pending) -> dict | None:
    for n in p.pr_numbers:
        r = gh(p.installation_id, "GET", f"/repos/{p.repo}/pulls/{n}")
        if r.ok:
            return r.json()
    r = gh(p.installation_id, "GET", f"/repos/{p.repo}/commits/{p.sha}/pulls")
    if r.ok and r.json():
        return r.json()[0]
    return None


def report(p: Pending, comment: str, state: str | None = None) -> None:
    body = {"environment_name": p.environment, "comment": comment[:1000]}
    if state:
        body["state"] = state
    elif comment == p.last_report:
        return  # unchanged status: do not burn one of the ten allowed reports
    r = gh(p.installation_id, "POST", p.callback_url, json=body)
    log.info("callback %s state=%s -> %s %s", p.callback_url, state, r.status_code, r.text[:200])
    if r.status_code == 422 and "not pending" in r.text.lower():
        p.done = True
    if r.ok:
        p.last_report = comment
        if state:
            p.done = True


def evaluate(p: Pending) -> None:
    pr = find_pr(p)
    if not pr:
        report(p, "cab-checker: no pull request found for this commit; cannot locate a CAB item.")
        return
    ids = sorted({int(x) for x in re.findall(r"AB#(\d+)", pr.get("body") or "")})
    if len(ids) != 1:
        report(p, f"cab-checker: PR #{pr['number']} must reference exactly one CAB item as `AB#<id>` (found {ids or 'none'}).")
        return
    p.cab_id = ids[0]
    wi = ado_work_item(p.cab_id)
    f = wi["fields"]
    recorded = (f.get(F_SHA) or "").strip().lower()
    pr_head = pr["head"]["sha"]
    owner_val = identity_name(f.get(F_OWNER))
    sec_val = identity_name(f.get(F_SECURITY))

    setters = ado_field_setters(p.cab_id)
    owner_by = setters.get(F_OWNER, owner_val) if owner_val else ""
    sec_by = setters.get(F_SECURITY, sec_val) if sec_val else ""
    # ADO rules already restrict who can write each field; the allowlists are an optional second check.
    owner_ok = bool(owner_val) and (not OWNER_APPROVERS or owner_by in OWNER_APPROVERS)
    sec_ok = bool(sec_val) and (not SECURITY_APPROVERS or sec_by in SECURITY_APPROVERS)

    def line(label, val, by, ok):
        if not val:
            return f"{label}: pending"
        return f"{label}: {'approved, ' + val + ' (set by ' + by + ')' if ok else 'set by ' + by + ', who is not an authorized approver'}"

    cab_url = ((wi.get("_links") or {}).get("html") or {}).get("href") or f"{ADO_ORG_URL}/_workitems/edit/{p.cab_id}"
    status = [
        f"CAB AB#{p.cab_id} `{f.get('System.Title','')}` (state {f.get('System.State','?')}): {cab_url}",
        line("Owner", owner_val, owner_by, owner_ok),
        line("Security", sec_val, sec_by, sec_ok),
    ]

    if f.get("System.State") in ("Rejected", "Closed"):
        status.append(f"CAB is {f.get('System.State')}; rejecting this deployment.")
        report(p, "\n".join(status), state="rejected")
        return
    if recorded and (recorded != p.sha.lower() or recorded != pr_head.lower()):
        status.append(f"Head changed: CAB recorded `{recorded[:12]}`, gated commit `{p.sha[:12]}`, PR head `{pr_head[:12]}`. Approvals cleared; re-run promote-prod.")
        if owner_val or sec_val:
            ado_clear_approvals(p.cab_id)
        report(p, "\n".join(status), state="rejected")
        return
    if not recorded:
        status.append(f"CAB item has no {F_SHA}; refusing to approve.")
        report(p, "\n".join(status))
        return

    if owner_ok and sec_ok:
        status.append(f"Head `{p.sha[:12]}` matches. Releasing.")
        report(p, "\n".join(status), state="approved")
        if f.get("System.State") != APPROVED_STATE:
            ado_patch(p.cab_id, [{"op": "add", "path": "/fields/System.State", "value": APPROVED_STATE}])
    else:
        report(p, "\n".join(status))


def evaluate_all() -> None:
    with LOCK:
        items = [p for p in PENDING.values() if not p.done]
    for p in items:
        try:
            evaluate(p)
        except Exception:  # keep the loop alive; the next tick retries
            log.exception("evaluate failed for %s", p.callback_url)
    with LOCK:
        for k in [k for k, p in PENDING.items() if p.done or time.time() - p.created > 31 * 86400]:
            PENDING.pop(k, None)


def poll_loop() -> None:
    while True:
        time.sleep(POLL_SECONDS)
        evaluate_all()


# ---------------------------------------------------------------- HTTP

app = Flask(__name__)


def verify_signature(raw: bytes, sig: str | None) -> None:
    if not WEBHOOK_SECRET:
        return
    if not sig or not sig.startswith("sha256="):
        abort(401)
    expected = "sha256=" + hmac.new(WEBHOOK_SECRET, raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        abort(401)


@app.post("/webhook")
def webhook():
    raw = request.get_data()
    verify_signature(raw, request.headers.get("X-Hub-Signature-256"))
    event = request.headers.get("X-GitHub-Event", "")
    payload = json.loads(raw or b"{}")
    if event == "ping":
        return jsonify(ok=True)
    if event != "deployment_protection_rule" or payload.get("action") != "requested":
        return jsonify(ignored=event)
    if ENV_NAME_FILTER and payload.get("environment") != ENV_NAME_FILTER:
        return jsonify(ignored=f"environment {payload.get('environment')}")
    p = Pending(
        callback_url=payload["deployment_callback_url"],
        installation_id=payload["installation"]["id"],
        repo=payload["repository"]["full_name"],
        sha=payload["deployment"]["sha"],
        environment=payload["environment"],
        pr_numbers=[x["number"] for x in payload.get("pull_requests") or []],
    )
    with LOCK:
        PENDING[p.callback_url] = p
    log.info("pending %s %s@%s prs=%s", p.environment, p.repo, p.sha[:12], p.pr_numbers)
    threading.Thread(target=lambda: evaluate_all(), daemon=True).start()
    return jsonify(accepted=True)


@app.post("/ado-hook")
def ado_hook():
    # Azure DevOps service hook (workitem.updated). Payload is not verified beyond being JSON;
    # it only triggers a re-evaluation, which reads ADO itself.
    threading.Thread(target=lambda: evaluate_all(), daemon=True).start()
    return jsonify(accepted=True)


@app.get("/pending")
def pending():
    with LOCK:
        return jsonify([
            {"repo": p.repo, "sha": p.sha, "environment": p.environment, "cab_id": p.cab_id,
             "prs": p.pr_numbers, "age_s": int(time.time() - p.created), "last_report": p.last_report}
            for p in PENDING.values() if not p.done
        ])


@app.get("/health")
def health():
    return jsonify(ok=True, app_id=bool(APP_ID), ado=bool(ADO_ORG_URL and ADO_PAT),
                   owner_approvers=len(OWNER_APPROVERS), security_approvers=len(SECURITY_APPROVERS))


if __name__ == "__main__":
    threading.Thread(target=poll_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
