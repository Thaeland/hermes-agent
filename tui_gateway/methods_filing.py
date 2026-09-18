"""AI-assisted filing RPC surface: the correction-aware Projects filing contract.

Exposes the user's filing policy (rules / exclusions / audit), the hook that
maintains it, and a deterministic suggestion pass over unassigned sessions.
This is the contract the Hermes Android "AI-assisted filing" pane consumes;
an install without the filing library answers ``available: false`` everywhere
instead of erroring, so the client degrades the entry rather than failing.

Decision ladder (roadmap Phase 2, tiers 1-2 only — no LLM here):
  1. deterministic: the session's cwd is owned by an existing project folder
     or covered by a contract rule;
  2. exclusions veto every suggestion for the path.
"""

from __future__ import annotations

import os

from .method_ctx import HandlerRegistry, bind_module

_registry = HandlerRegistry()
method = _registry.method

_E_FILING = 5071
_E_FILING_ARG = 5072

# Sessions considered for suggestions: recent, message-bearing, non-cron.
_SUGGEST_LIMIT = 200


def _filing_available() -> bool:
    from tui_gateway import filing_bridge
    return filing_bridge.load_contract_lib() is not None


def _contract_or_err(rid):
    """(contract_dict, None) or (None, error response)."""
    from tui_gateway import filing_bridge
    if not _filing_available():
        return None, _err(rid, _E_FILING, "filing contract library not installed")
    return filing_bridge.read_contract(), None


@_registry.method("filing.status")
@_registry.profile_scoped
def _(rid, params: dict) -> dict:
    from tui_gateway import filing_bridge
    available = _filing_available()
    contract = filing_bridge.read_contract() if available else {}
    return _ok(rid, {
        "available": available,
        "hook_installed": filing_bridge.hook_installed(),
        "contract_path": str(filing_bridge.contract_path()),
        "rules": len(contract.get("rules", [])),
        "exclusions": len(contract.get("exclusions", [])),
        "audit_entries": len(contract.get("audit", [])),
    })


@_registry.method("filing.rules")
@_registry.profile_scoped
def _(rid, params: dict) -> dict:
    contract, err = _contract_or_err(rid)
    if err:
        return err
    return _ok(rid, {
        "rules": contract.get("rules", []),
        "exclusions": contract.get("exclusions", []),
        "audit": contract.get("audit", [])[-50:],
    })


def _suggestion_input(db):
    """Recent message-bearing sessions with a cwd, newest first."""
    rows = db.list_sessions_rich(
        limit=_SUGGEST_LIMIT, offset=0, order_by_last_active=True,
        min_message_count=1, include_children=False,
        exclude_sources=["cron", "kanban"], include_archived=False,
        compact_rows=True)
    return [r for r in rows if (r.get("cwd") or "").strip()]


@_registry.method("filing.suggest")
@_registry.profile_scoped
def _(rid, params: dict) -> dict:
    from tui_gateway import filing_bridge
    contract, err = _contract_or_err(rid)
    if err:
        return err
    pfc = filing_bridge.load_contract_lib()
    if pfc is None:
        return _err(rid, _E_FILING, "filing contract library not installed")
    try:
        from hermes_cli import projects_db as pdb
    except Exception:
        return _err(rid, _E_FILING, "projects database unavailable")
    suggestions = []
    with pdb.connect_closing() as conn, _profile_db(params) as db:
        if db is None:
            return _db_unavailable_error(rid, code=_E_FILING)
        for row in _suggestion_input(db):
            cwd = row["cwd"]
            if pfc.is_excluded(cwd, data=contract):
                continue
            current = pdb.project_for_path(conn, cwd)
            rule = pfc.rule_for_path(cwd, data=contract)
            if rule:
                proj = _project_by_name(pdb, conn, rule["project"])
                # A rule that agrees with the cwd-derived filing is not a
                # suggestion — the session is already filed deterministically.
                if proj is not None and (current is None or proj.id != current.id):
                    suggestions.append(_suggestion(row, proj, "contract_rule", 1.0))
                continue
            # A project-scoped exclusion ("never file X under Y") must veto the
            # cwd_match for Y specifically; is_excluded(cwd) alone only trips on
            # bare exclusions, so check the owning project by name too.
            if current is not None and not pfc.is_excluded(
                    cwd, project=current.name, data=contract):
                suggestions.append(_suggestion(row, current, "cwd_match", 0.95))
    return _ok(rid, {"suggestions": suggestions})


@_registry.method("filing.apply")
@_registry.profile_scoped
def _(rid, params: dict) -> dict:
    from tui_gateway import filing_bridge
    pfc = filing_bridge.load_contract_lib()
    if pfc is None:
        return _err(rid, _E_FILING, "filing contract library not installed")
    path = str(params.get("path") or "").strip()
    project = str(params.get("project") or "").strip()
    if not path or not project:
        return _err(rid, _E_FILING_ARG, "path and project are required")
    # The contract is durable policy the live hook acts on: refuse paths that
    # aren't absolute rather than recording a rule nothing can honour.
    if not os.path.isabs(os.path.expanduser(path)):
        return _err(rid, _E_FILING_ARG, "path must be absolute")
    source = str(params.get("source") or "rpc:apply")
    # Mirror into projects.db FIRST. If the mirror fails (e.g. the folder is
    # owned by a different project), the contract rule is never recorded —
    # the alternative order left durable policy contradicting projects.db
    # with no rollback.
    mirror = _mirror_to_projects_db(path, project)
    if mirror.startswith("projects.db apply failed"):
        return _err(rid, _E_FILING, f"rule not recorded: {mirror}")
    with pfc._LOCK:
        data = pfc.load_contract(filing_bridge.contract_path())
        data, note = pfc.add_rule(path, project, source=source, data=data)
        pfc.save_contract(data, filing_bridge.contract_path())
    return _ok(rid, {"applied": True, "note": note, "db": mirror})


@_registry.method("filing.reject")
@_registry.profile_scoped
def _(rid, params: dict) -> dict:
    from tui_gateway import filing_bridge
    pfc = filing_bridge.load_contract_lib()
    if pfc is None:
        return _err(rid, _E_FILING, "filing contract library not installed")
    path = str(params.get("path") or "").strip()
    project = str(params.get("project") or "").strip() or None
    if not path:
        return _err(rid, _E_FILING_ARG, "path is required")
    if not os.path.isabs(os.path.expanduser(path)):
        return _err(rid, _E_FILING_ARG, "path must be absolute")
    source = str(params.get("source") or "rpc:reject")
    with pfc._LOCK:
        data = pfc.load_contract(filing_bridge.contract_path())
        data, note = pfc.add_exclusion(path, project, source=source, data=data)
        if note:
            pfc.save_contract(data, filing_bridge.contract_path())
    # Retroactive unfile runs even when the exclusion was already recorded:
    # the DB may still hold the wrong filing from before the correction.
    retro = _retroactive_unfile(path, project)
    return _ok(rid, {"recorded": bool(note), "note": note or "already excluded",
                    "db": retro})


# ---------------------------------------------------------------- helpers


def _project_by_name(pdb, conn, want: str):
    """Match by display name or derived slug (normalize_slug validates, never derives)."""
    want = (want or "").strip()
    if not want:
        return None
    try:
        want_slug = pdb._slugify(want)
    except Exception:
        want_slug = want.lower()
    for p in pdb.list_projects(conn, include_archived=True):
        if p.name.lower() == want.lower() or p.slug.lower() == want_slug:
            return p
    return None


def _suggestion(row: dict, project, reason: str, confidence: float) -> dict:
    return {
        "session_id": row.get("id"),
        "title": row.get("title") or row.get("preview") or "",
        "cwd": row.get("cwd"),
        "project_id": project.id,
        "project_name": project.name,
        "reason": reason,
        "confidence": confidence,
    }


def _mirror_to_projects_db(path: str, project: str) -> str:
    """Create-or-add-folder for an applied rule (same shape as the hook).

    Runs BEFORE the contract write; a failure here aborts the apply, so the
    returned failure prefix is a control signal, not just prose."""
    try:
        from hermes_cli import projects_db as pdb
        with pdb.connect_closing() as conn:
            proj = _project_by_name(pdb, conn, project)
            if proj is None:
                pid = pdb.create_project(conn, name=project, folders=[path])
                return f"created project '{project}' ({pid}) with {path}"
            pdb.add_folder(conn, proj.id, path)
            return f"filed {path} under '{proj.name}'"
    except Exception as exc:
        return f"projects.db apply failed: {exc}"


def _retroactive_unfile(path: str, project) -> str:
    hook = None
    try:
        from tui_gateway import filing_bridge
        hook = filing_bridge.load_filing_hook_lib()
    except Exception:
        pass
    if hook is not None and hasattr(hook, "_unfile_from_project_db"):
        try:
            return hook._unfile_from_project_db(path, project) or ""
        except Exception as exc:
            return f"retroactive unfile failed: {exc}"
    if not project:
        return ""
    try:
        from hermes_cli import projects_db as pdb
        with pdb.connect_closing() as conn:
            proj = _project_by_name(pdb, conn, project)
            if proj is None:
                return ""
            # Only an EXACT folder mapping is removable here. When the rejected
            # path merely sits inside a project folder, dropping that folder
            # would un-file every other session under it — that surgical
            # per-path unfile is the hook's job, not this fallback's.
            for f in proj.folders:
                if os.path.normpath(f.path) == os.path.normpath(path):
                    pdb.remove_folder(conn, proj.id, f.path)
                    return f"removed {path} from '{proj.name}'"
            return f"{path} is inside a folder of '{proj.name}'; hook required to unfile"
    except Exception as exc:
        return f"retroactive unfile failed: {exc}"


def register(server) -> None:
    """Publish this module's helpers + handlers onto ``server`` (rebound to its globals)."""
    bind_module(globals(), server, skip=("_",))
