#!/usr/bin/env python3
"""Correction-aware PROJECT filing contract (Hermes Projects, not email).

The contract lives at ``~/.hermes/project_filing.json`` and records the user's
standing agreements about which Hermes Project a folder/repo is filed under —
including the corrections that override naive filing. The gateway hook
``project-filing`` writes it; ``resolve_project_for_path()`` is the read path any
filing consumer should use instead of guessing from the folder name.

Resolution order for a (path, project) pair — first match wins:
  1. exclusion  ("never file X under Y")   -> refuse, even if a rule says yes
  2. rule       ("file X under Y")         -> that project
  3. no opinion -> None (caller decides)

Exclusions outrank rules so a correction can never be silently undone by a later
broad rule; rules outrank any name-based heuristic the caller might apply.

Secrets: none. This file is settings-shaped data, not credentials.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

CONTRACT_PATH = Path(os.environ.get(
    "PROJECT_FILING_CONTRACT",
    str(Path(os.path.expanduser("~")) / ".hermes" / "project_filing.json")))

_LOCK = threading.Lock()

# Where a bare folder name is looked up before giving up.
DEFAULT_SEARCH_ROOTS = ["~/Projects", "~/repos", "~/src", "~"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_path(path: str) -> str:
    """Absolute, user-expanded, no trailing separator. Mirrors projects_db._normalize_path
    so contract keys and DB keys are the same string."""
    p = os.path.abspath(os.path.expanduser(str(path or "").strip()))
    return p.rstrip("/\\") or p


def resolve_path(token: str, search_roots: Optional[List[str]] = None) -> Optional[str]:
    """Resolve a user-supplied folder token to an existing absolute path.

    Accepts an absolute/~ path directly; a bare name is searched under the roots
    (``~/Projects`` first). Returns None when nothing exists — a directive naming a
    non-existent folder is NOT silently filed somewhere plausible.
    """
    raw = str(token or "").strip().strip('"\'')
    if not raw:
        return None
    if raw.startswith(("/", "~", ".")):
        cand = normalize_path(raw)
        return cand if os.path.isdir(cand) else None
    for root in (search_roots or DEFAULT_SEARCH_ROOTS):
        cand = normalize_path(os.path.join(root, raw))
        if os.path.isdir(cand):
            return cand
    return None


def empty_contract() -> Dict[str, Any]:
    return {"version": 1, "rules": [], "exclusions": [], "audit": []}


def load_contract(path: Optional[Path] = None) -> Dict[str, Any]:
    p = Path(path) if path else CONTRACT_PATH
    try:
        with p.open(encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return empty_contract()
    except (json.JSONDecodeError, OSError):
        # A corrupt contract must not be silently overwritten: surface it and treat
        # as empty for reads. Writes refuse over an unreadable file.
        return empty_contract()
    if not isinstance(data, dict):
        return empty_contract()
    data.setdefault("version", 1)
    data.setdefault("rules", [])
    data.setdefault("exclusions", [])
    data.setdefault("audit", [])
    return data


def save_contract(data: Dict[str, Any], path: Optional[Path] = None) -> None:
    """Atomic replace (temp in the same dir + os.replace)."""
    p = Path(path) if path else CONTRACT_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".project_filing.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, p)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _owns(folder: str, target: str) -> bool:
    """Folder owns target when equal or an ancestor (same semantics as
    projects_db.project_for_path: longest/innermost folder wins)."""
    stem = folder.rstrip("/\\")
    return target == folder or target.startswith(stem + os.sep) or target.startswith(stem + "/")


def is_excluded(path: str, project: Optional[str] = None,
               data: Optional[Dict[str, Any]] = None) -> bool:
    """True if the user has said "never file <path> under <project>".

    A bare exclusion (project=None, from "never file X") excludes the path from
    EVERY project. A project-specific exclusion ("never file X under Y") only
    excludes X from Y — it must NOT block X from being filed elsewhere."""
    target = normalize_path(path)
    d = data if data is not None else load_contract()
    proj = (project or "").strip().lower()
    for ex in d.get("exclusions", []):
        if not _owns(normalize_path(ex.get("path", "")), target):
            continue
        ex_proj = str(ex.get("project") or "").strip().lower()
        if not ex_proj:
            return True  # bare exclusion: excluded from everything
        if proj and ex_proj == proj:
            return True
    return False


def rule_for_path(path: str, data: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """The most specific (longest matching folder) filing rule covering ``path``,
    unless that rule's project is excluded for the path (exclusion wins)."""
    target = normalize_path(path)
    d = data if data is not None else load_contract()
    matches = [r for r in d.get("rules", [])
               if _owns(normalize_path(r.get("path", "")), target) and r.get("project")
               and not is_excluded(target, r["project"], data=d)]
    if not matches:
        return None
    return max(matches, key=lambda r: len(normalize_path(r.get("path", ""))))


def resolve_project_for_path(path: str, data: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """Contract's answer for which project slug/name ``path`` belongs to, or None."""
    rule = rule_for_path(path, data=data)
    return rule.get("project") if rule else None


def add_rule(path: str, project: str, *, source: str = "",
             data: Optional[Dict[str, Any]] = None) -> Tuple[Dict[str, Any], str]:
    """Record/replace a filing rule. Returns (data, note) — note is None if unchanged."""
    d = data if data is not None else load_contract()
    norm = normalize_path(path)
    existing = next((r for r in d["rules"] if normalize_path(r.get("path", "")) == norm), None)
    prev = existing.get("project") if existing else None
    if prev == project:
        return d, ""
    if existing:
        existing["project"] = project
        existing["updated_at"] = _now()
        note = f"refiled {norm} -> {project} (was {prev})"
    else:
        d["rules"].append({"path": norm, "project": project,
                          "created_at": _now(), "source": source})
        note = f"file {norm} -> {project}"
    d["audit"].append({"ts": _now(), "action": "file", "path": norm,
                      "project": project, "prev": prev, "quote": source[:200]})
    return d, note


def add_exclusion(path: str, project: Optional[str], *, source: str = "",
                 data: Optional[Dict[str, Any]] = None) -> Tuple[Dict[str, Any], str]:
    d = data if data is not None else load_contract()
    norm = normalize_path(path)
    proj = (project or "").strip() or None
    already = any(_owns(normalize_path(e.get("path", "")), norm)
                  and (not proj or not e.get("project")
                       or str(e.get("project")).lower() == proj.lower())
                  for e in d["exclusions"])
    if already:
        return d, ""
    d["exclusions"].append({"path": norm, "project": proj,
                           "created_at": _now(), "source": source})
    # A new "never" also drops any rule that would have filed this path there.
    d["rules"] = [r for r in d["rules"]
                  if not (normalize_path(r.get("path", "")) == norm
                         and (not proj or str(r.get("project", "")).lower() == proj.lower()))]
    d["audit"].append({"ts": _now(), "action": "exclude", "path": norm,
                      "project": proj, "quote": source[:200]})
    return d, f"never file {norm}" + (f" under {proj}" if proj else "")


def remove_rule(path: str, *, source: str = "",
               data: Optional[Dict[str, Any]] = None) -> Tuple[Dict[str, Any], str]:
    d = data if data is not None else load_contract()
    norm = normalize_path(path)
    before = len(d["rules"])
    d["rules"] = [r for r in d["rules"] if normalize_path(r.get("path", "")) != norm]
    if len(d["rules"]) == before:
        return d, ""
    d["audit"].append({"ts": _now(), "action": "unfile", "path": norm, "quote": source[:200]})
    return d, f"unfiled {norm}"


def clear_exclusion(path: str, *, source: str = "",
                   data: Optional[Dict[str, Any]] = None) -> Tuple[Dict[str, Any], str]:
    d = data if data is not None else load_contract()
    norm = normalize_path(path)
    before = len(d["exclusions"])
    d["exclusions"] = [e for e in d["exclusions"] if normalize_path(e.get("path", "")) != norm]
    if len(d["exclusions"]) == before:
        return d, ""
    d["audit"].append({"ts": _now(), "action": "unexclude", "path": norm, "quote": source[:200]})
    return d, f"cleared exclusion on {norm}"


def summary(data: Optional[Dict[str, Any]] = None) -> Dict[str, int]:
    d = data if data is not None else load_contract()
    return {"rules": len(d.get("rules", [])),
            "exclusions": len(d.get("exclusions", [])),
            "audit": len(d.get("audit", []))}


if __name__ == "__main__":
    c = load_contract()
    print(f"contract: {CONTRACT_PATH}")
    print(json.dumps(summary(c)))
    for r in c.get("rules", []):
        print(f"  rule      {r['path']}  ->  {r['project']}")
    for e in c.get("exclusions", []):
        print(f"  exclusion {e['path']}  (project={e.get('project') or 'any'})")
