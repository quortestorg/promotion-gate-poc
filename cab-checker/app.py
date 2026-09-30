"""cab-checker: a GitHub App that implements a custom deployment protection rule.

When a workflow job targets an environment that has this rule enabled, GitHub sends a
`deployment_protection_rule` webhook and holds the job. This service decides whether to
release it by reading the CAB work item in Azure DevOps that the pull request links to.

Approval conditions (all must hold):
  1. The PR body references exactly one CAB item (`AB#<id>`).
  2. The CAB item carries tag `CAB-Approved-Owner`, last added by someone in
     OWNER_APPROVERS, and tag `CAB-Approved-Security`, last added by someone in
     SECURITY_APPROVERS. "Who added it" comes from the work item's update history, not from
     the tag value, so a tag pasted by the wrong person does not count.
  3. The `PR-HEAD-SHA:` recorded on the CAB item equals the commit the gated job runs on,
     which promote-prod.yml arranged to be the PR head. A newer push therefore invalidates the
     approval; in that case the two tags are removed from the CAB item and the deployment is
     rejected with a reason.

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
TAG_OWNER = "CAB-Approved-Owner"
TAG_SECURITY = "CAB-Approved-Security"
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
    r = ado("GET", f"/_apis/wit/workitems/{wid}?fields=System.Tags,System.Description,System.State,System.Title")
    r.raise_for_status()
    return r.json()


def ado_tag_adders(wid: int) -> dict[str, str]:
    """Return {tag: email-of-last-person-who-added-it} from the update history."""
    r = ado("GET", f"/_apis/wit/workitems/{wid}/updates?$top=200")
    r.raise_for_status()
    adders: dict[str, str] = {}
    for upd in r.json().get("value", []):
        change = (upd.get("fields") or {}).get("System.Tags")
        if not change:
            continue
        old = {t.strip() for t in (change.get("oldValue") or "").split(";") if t.strip()}
        new = {t.strip() for t in (change.get("newValue") or "").split(";") if t.strip()}
        who = ((upd.get("revisedBy") or {}).get("uniqueName") or "").lower()
        for t in new - old:
            adders[t] = who
        for t in old - new:
            adders.pop(t, None)
    return adders


def ado_remove_tags(wid: int, tags: set[str]) -> None:
    wi = ado_work_item(wid)
    current = [t.strip() for t in wi["fields"].get("System.Tags", "").split(";") if t.strip()]
    remaining = "; ".join(t for t in current if t not in tags)
    ado(
        "PATCH",
        f"/_apis/wit/workitems/{wid}",
        headers={"Content-Type": "application/json-patch+json"},
        data=json.dumps([{"op": "replace", "path": "/fields/System.Tags", "value": remaining}]),
    ).raise_for_status()


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
    tags = {t.strip() for t in f.get("System.Tags", "").split(";") if t.strip()}
    m = re.search(r"PR-HEAD-SHA:\s*([0-9a-f]{40})", re.sub(r"<[^>]+>", " ", f.get("System.Description", "")))
    recorded = m.group(1) if m else ""
    pr_head = pr["head"]["sha"]

    adders = ado_tag_adders(p.cab_id)
    up_by = adders.get(TAG_OWNER, "") if TAG_OWNER in tags else ""
    sec_by = adders.get(TAG_SECURITY, "") if TAG_SECURITY in tags else ""
    up_ok = bool(up_by) and up_by in OWNER_APPROVERS
    sec_ok = bool(sec_by) and sec_by in SECURITY_APPROVERS

    def line(label, present_by, ok):
        if not present_by:
            return f"{label}: pending"
        return f"{label}: {'approved by ' + present_by if ok else 'tag added by ' + present_by + ', who is not an authorized approver'}"

    status = [
        f"CAB AB#{p.cab_id} `{f.get('System.Title','')}` (state {f.get('System.State','?')})",
        line("Owner", up_by, up_ok),
        line("Security", sec_by, sec_ok),
    ]

    if recorded and (recorded != p.sha or recorded != pr_head):
        status.append(f"Head changed: CAB recorded `{recorded[:12]}`, gated commit `{p.sha[:12]}`, PR head `{pr_head[:12]}`. Approvals cleared; re-run promote-prod.")
        if tags & {TAG_OWNER, TAG_SECURITY}:
            ado_remove_tags(p.cab_id, {TAG_OWNER, TAG_SECURITY})
        report(p, "\n".join(status), state="rejected")
        return
    if not recorded:
        status.append("CAB item has no PR-HEAD-SHA line; refusing to approve.")
        report(p, "\n".join(status))
        return

    if up_ok and sec_ok:
        status.append(f"Head `{p.sha[:12]}` matches. Releasing.")
        report(p, "\n".join(status), state="approved")
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
