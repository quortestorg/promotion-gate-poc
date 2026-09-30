#!/usr/bin/env python3
"""Canonicalize a `kustomize build` render so two renders can be compared as a PROOF.

Reads a multi-document YAML render on stdin, writes a deterministic text form on stdout.
Pipe two renders through this and `diff` them: an empty diff is a byte-identity proof that
survives document reordering, key reordering and formatting churn, none of which change what
is applied to a cluster.

WHY THIS EXISTS AS A TOOL AND NOT A ONE-LINER. Collapsing per-instance copies into a shared
base is only safe if the render does not move, and that claim has to be re-proved once per
instance across the fan-out. A comparison re-typed nine times is nine chances to compare the
wrong pair, or to compare a raw render whose document ORDER changed for a reason nobody
checked. Ordering is exactly what kustomize is free to change when resources move between
roots, so a raw `diff` reports a difference that is not one, and the tempting response is to
stop trusting the diff.

WHAT IS NORMALIZED, and each line of this list is a decision:
  * Documents are sorted by (apiVersion, kind, namespace, name) -- a resource moving between
    kustomize roots changes emission order and nothing else.
  * Mapping keys are sorted recursively, so a re-serialized document compares equal.
  * Empty documents are dropped: kustomize emits a trailing `---` whose presence depends on
    the last resource's provenance.

WHAT IS DELIBERATELY NOT NORMALIZED, because normalizing it would hide a real change:
  * No value is rewritten, coerced or defaulted. An `ALLOW_DATA_LOSS` that moves from "false"
    to "true" MUST show as a diff -- that is the change this instrument exists to catch.
  * List order is preserved. Container order, env order and volume order are semantic in a
    podSpec, and a reordered env list can change which duplicate key wins.
  * Nothing is filtered out by kind or name. A tool that silently skips a resource cannot
    prove anything about it, and the resources most worth proving are the ones a filter
    would be most tempted to skip.

Exit codes: 0 on success, 1 on a parse failure (never silently emit a partial render -- an
empty or truncated normalization compares equal to another empty one, which would read as
proof when it is the absence of evidence).
"""

import sys

import yaml


def canonical(node):
    """Recursively sort mapping keys; leave sequences and scalars untouched."""
    if isinstance(node, dict):
        return {k: canonical(node[k]) for k in sorted(node, key=str)}
    if isinstance(node, list):
        return [canonical(v) for v in node]
    return node


def sort_key(doc):
    meta = doc.get("metadata") or {}
    return (
        str(doc.get("apiVersion", "")),
        str(doc.get("kind", "")),
        str(meta.get("namespace", "")),
        str(meta.get("name", "")),
    )


def main():
    raw = sys.stdin.buffer.read().decode("utf-8")
    if not raw.strip():
        print("normalize-render: refusing to normalize an EMPTY render", file=sys.stderr)
        print("  An empty normalization compares equal to another empty one, so this would", file=sys.stderr)
        print("  read as a passing proof when it is the absence of evidence.", file=sys.stderr)
        return 1
    try:
        docs = [d for d in yaml.safe_load_all(raw) if d]
    except yaml.YAMLError as exc:
        print(f"normalize-render: FAILED to parse the render: {exc}", file=sys.stderr)
        return 1
    if not docs:
        print("normalize-render: render parsed but contained NO documents", file=sys.stderr)
        return 1

    out = sys.stdout
    for doc in sorted(docs, key=sort_key):
        meta = doc.get("metadata") or {}
        # A stable, greppable header so a diff names the object that moved, not just a line number.
        out.write(
            "# ===== {}/{} {}/{} =====\n".format(
                doc.get("apiVersion", "?"),
                doc.get("kind", "?"),
                meta.get("namespace", "-"),
                meta.get("name", "?"),
            )
        )
        yaml.safe_dump(
            canonical(doc),
            out,
            default_flow_style=False,
            width=4096,          # do not line-wrap: a wrap point is formatting, not content
            allow_unicode=True,
            sort_keys=False,     # ordering already imposed by canonical(); do not re-sort lists
        )
        out.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
