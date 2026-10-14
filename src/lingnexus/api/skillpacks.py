"""Read-only view of the scanned skill packages, for the owner's settings.

Skills are external *knowledge*: they are discovered from folders and grant no
authority — every action a skill describes still goes through the ordinary
tools, hooks and sandbox. This endpoint only reports what exists.
"""

from fastapi import APIRouter, Depends, Request

from lingnexus.api.security import require_owner

router = APIRouter(dependencies=[Depends(require_owner)])


@router.get("/skillpacks")
def read_skillpacks(request: Request):
    library = request.app.state.services.skillpacks
    library.refresh()
    return {
        "roots": [
            {"source": source, "path": str(path), "exists": path.is_dir()}
            for source, path in library.roots()
        ],
        "skills": [
            {
                "name": skill.name,
                "description": skill.description,
                "source": skill.source,
            }
            for skill in library.index()
        ],
    }
