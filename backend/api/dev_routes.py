"""Developer workspace and code-intelligence routes."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from ..container import container
from .deps import current_user

router = APIRouter()


def _gate(permission: str, label: str) -> None:
    if not container.get("permissions").allowed(permission):
        raise HTTPException(
            status_code=403,
            detail=f"The '{label}' permission is off. Enable it in Settings -> Permissions.",
        )


async def _workspace(reference: str) -> Dict[str, Any]:
    try:
        return await container.get("code").resolve_workspace(reference)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


# ===========================================================================
# workspaces
# ===========================================================================
class WorkspaceRequest(BaseModel):
    path: str
    name: str = ""


@router.get("/dev", tags=["developer"])
async def dev_summary(workspace: str = "", user: Dict[str, Any] = Depends(current_user)):
    """Everything the developer panel needs, in one call."""
    _gate("dev.workspace", "Read your code")
    return await container.get("devtools").summary(workspace)


@router.get("/dev/workspaces", tags=["developer"])
async def dev_workspaces(user: Dict[str, Any] = Depends(current_user)):
    return {
        **await container.get("code").stats(),
        "permissions": {
            "read": container.get("permissions").allowed("dev.workspace"),
            "execute": container.get("permissions").allowed("dev.execute"),
            "git_write": container.get("permissions").allowed("dev.git_write"),
        },
    }


@router.post("/dev/workspaces", tags=["developer"])
async def add_workspace(payload: WorkspaceRequest,
                        user: Dict[str, Any] = Depends(current_user)):
    _gate("dev.workspace", "Read your code")
    try:
        return await container.get("code").add_workspace(payload.path, payload.name)
    except (PermissionError, NotADirectoryError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/dev/workspaces/{workspace_id}/index", tags=["developer"])
async def index_workspace(workspace_id: str, user: Dict[str, Any] = Depends(current_user)):
    _gate("dev.workspace", "Read your code")
    try:
        return await container.get("code").index(workspace_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.delete("/dev/workspaces/{workspace_id}", tags=["developer"])
async def remove_workspace(workspace_id: str, user: Dict[str, Any] = Depends(current_user)):
    return {"removed": await container.get("code").remove_workspace(workspace_id)}


@router.get("/dev/overview", tags=["developer"])
async def dev_overview(workspace: str = "", user: Dict[str, Any] = Depends(current_user)):
    _gate("dev.workspace", "Read your code")
    record = await _workspace(workspace)
    return await container.get("code").overview(record["id"])


# ===========================================================================
# git
# ===========================================================================
class CommitRequest(BaseModel):
    message: str
    paths: Optional[List[str]] = None
    workspace: str = ""


@router.get("/dev/git/status", tags=["developer"])
async def git_status(workspace: str = "", user: Dict[str, Any] = Depends(current_user)):
    _gate("dev.workspace", "Read your code")
    try:
        return await container.get("devtools").status(workspace)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.get("/dev/git/diff", tags=["developer"])
async def git_diff(workspace: str = "", path: str = "", staged: bool = False,
                   user: Dict[str, Any] = Depends(current_user)):
    _gate("dev.workspace", "Read your code")
    try:
        return await container.get("devtools").diff(workspace, path, staged)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.get("/dev/git/log", tags=["developer"])
async def git_log(workspace: str = "", limit: int = 20, path: str = "",
                  user: Dict[str, Any] = Depends(current_user)):
    _gate("dev.workspace", "Read your code")
    try:
        return {"commits": await container.get("devtools").log(workspace, limit, path)}
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/dev/git/review", tags=["developer"])
async def review_diff(workspace: str = "", staged: bool = False,
                      user: Dict[str, Any] = Depends(current_user)):
    _gate("dev.workspace", "Read your code")
    return await container.get("devtools").review_diff(workspace, staged)


@router.post("/dev/git/commit-message", tags=["developer"])
async def commit_message(workspace: str = "", staged_only: bool = True,
                         user: Dict[str, Any] = Depends(current_user)):
    _gate("dev.workspace", "Read your code")
    return await container.get("devtools").commit_message(workspace, staged_only)


@router.post("/dev/git/commit", tags=["developer"])
async def git_commit(payload: CommitRequest, user: Dict[str, Any] = Depends(current_user)):
    _gate("dev.git_write", "Write to git")
    dev = container.get("devtools")
    if payload.paths:
        staged = await dev.stage(payload.paths, payload.workspace)
        if not staged.get("ok"):
            raise HTTPException(status_code=400, detail=staged.get("error", "Staging failed."))
    result = await dev.commit(payload.message, payload.workspace)
    if not result.get("ok"):
        raise HTTPException(status_code=400,
                            detail=result.get("error") or result.get("message"))
    return result


# ===========================================================================
# tests, builds, CI
# ===========================================================================
@router.post("/dev/tests", tags=["developer"])
async def run_tests(workspace: str = "", pattern: str = "", analyse: bool = False,
                    user: Dict[str, Any] = Depends(current_user)):
    _gate("dev.execute", "Run builds and tests")
    dev = container.get("devtools")
    return await (dev.analyse_failures(workspace) if analyse
                  else dev.run_tests(workspace, pattern))


@router.post("/dev/build", tags=["developer"])
async def run_build(workspace: str = "", user: Dict[str, Any] = Depends(current_user)):
    _gate("dev.execute", "Run builds and tests")
    return await container.get("devtools").run_build(workspace)


@router.get("/dev/ci", tags=["developer"])
async def ci_status(repo: str, user: Dict[str, Any] = Depends(current_user)):
    return await container.get("devtools").ci_status(repo)


@router.get("/dev/timeline", tags=["developer"])
async def dev_timeline(workspace: str = "", hours: int = 72,
                       user: Dict[str, Any] = Depends(current_user)):
    _gate("dev.workspace", "Read your code")
    return {"events": await container.get("devtools").timeline(workspace, hours)}


class DebugRequest(BaseModel):
    problem: str
    workspace: str = ""


@router.post("/dev/debug-plan", tags=["developer"])
async def debug_plan(payload: DebugRequest, user: Dict[str, Any] = Depends(current_user)):
    _gate("dev.workspace", "Read your code")
    try:
        return await container.get("devtools").debug_plan(payload.problem, payload.workspace)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


# ===========================================================================
# code intelligence
# ===========================================================================
@router.get("/code/find", tags=["developer"])
async def code_find(name: str, mode: str = "definition", workspace: str = "",
                    user: Dict[str, Any] = Depends(current_user)):
    _gate("dev.workspace", "Read your code")
    code = container.get("code")
    record = await _workspace(workspace)
    if mode == "references":
        return {"references": await code.find_references(name, record["id"])}
    return {"symbols": await code.find_symbol(name, record["id"])}


@router.get("/code/outline", tags=["developer"])
async def code_outline(path: str, workspace: str = "",
                       user: Dict[str, Any] = Depends(current_user)):
    _gate("dev.workspace", "Read your code")
    record = await _workspace(workspace)
    try:
        return await container.get("code").outline(path, record["id"])
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


class AskRequest(BaseModel):
    question: str
    workspace: str = ""


@router.post("/code/ask", tags=["developer"])
async def code_ask(payload: AskRequest, user: Dict[str, Any] = Depends(current_user)):
    _gate("dev.workspace", "Read your code")
    record = await _workspace(payload.workspace)
    return await container.get("code").ask_about_codebase(payload.question, record["id"])


class ErrorRequest(BaseModel):
    error: str
    workspace: str = ""


@router.post("/code/explain-error", tags=["developer"])
async def code_explain_error(payload: ErrorRequest,
                             user: Dict[str, Any] = Depends(current_user)):
    _gate("dev.workspace", "Read your code")
    record = await _workspace(payload.workspace)
    return await container.get("code").explain_error(payload.error, record["id"])


class ReviewRequest(BaseModel):
    path: str
    focus: str = "bugs"
    workspace: str = ""


@router.post("/code/review", tags=["developer"])
async def code_review(payload: ReviewRequest, user: Dict[str, Any] = Depends(current_user)):
    _gate("dev.workspace", "Read your code")
    record = await _workspace(payload.workspace)
    try:
        return await container.get("code").review_file(payload.path, record["id"], payload.focus)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
