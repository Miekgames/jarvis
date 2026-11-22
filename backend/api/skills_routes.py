"""Skills: list, read, write, import, switch on and off."""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..container import container
from .deps import current_user

router = APIRouter()


def _library():
    library = container.get("skills")
    if library is None:
        raise HTTPException(status_code=503, detail="Skills are not available.")
    return library


def _errors(call):
    from ..skills.library import SkillError

    try:
        return call()
    except SkillError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


class NewSkill(BaseModel):
    name: str
    description: str
    instructions: str


class SkillText(BaseModel):
    text: str


class Import(BaseModel):
    text: str = ""
    url: str = ""


class Switch(BaseModel):
    enabled: bool


@router.get("/skills", tags=["skills"])
async def list_skills(user: Dict[str, Any] = Depends(current_user)):
    return {"skills": [s.as_dict() for s in _library().all()]}


@router.get("/skills/{name}", tags=["skills"])
async def get_skill(name: str, user: Dict[str, Any] = Depends(current_user)):
    skill = _library().get(name)
    if skill is None:
        raise HTTPException(status_code=404, detail=f"No skill called '{name}'.")
    return skill.as_dict(body=True)


@router.post("/skills", tags=["skills"])
async def create_skill(payload: NewSkill, user: Dict[str, Any] = Depends(current_user)):
    from ..skills.library import slug

    skill = _errors(lambda: _library().create(
        slug(payload.name), payload.description, payload.instructions, tier="mine"))
    return skill.as_dict(body=True)


@router.put("/skills/{name}", tags=["skills"])
async def update_skill(name: str, payload: SkillText,
                       user: Dict[str, Any] = Depends(current_user)):
    return _errors(lambda: _library().update(name, payload.text)).as_dict(body=True)


@router.post("/skills/import", tags=["skills"])
async def import_skill(payload: Import, user: Dict[str, Any] = Depends(current_user)):
    """Paste a SKILL.md, or give its address. Screened on the way in."""
    from ..skills.library import SkillError

    library = _library()
    try:
        if payload.url.strip():
            skill = await library.import_url(payload.url.strip())
        elif payload.text.strip():
            skill = library.import_text(payload.text)
        else:
            raise SkillError("Paste a SKILL.md or give its address.")
    except SkillError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return skill.as_dict(body=True)


@router.patch("/skills/{name}", tags=["skills"])
async def switch_skill(name: str, payload: Switch,
                       user: Dict[str, Any] = Depends(current_user)):
    return _errors(lambda: _library().set_enabled(name, payload.enabled)).as_dict()


@router.delete("/skills/{name}", tags=["skills"])
async def delete_skill(name: str, user: Dict[str, Any] = Depends(current_user)):
    removed = _errors(lambda: _library().delete(name))
    if not removed:
        raise HTTPException(status_code=404, detail=f"No skill called '{name}'.")
    return {"deleted": name}
