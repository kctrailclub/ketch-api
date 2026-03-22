from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import get_current_admin, get_current_user
from app.core.audit import log_action
from app.models.models import Project, User

router = APIRouter(prefix="/projects", tags=["projects"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class CreateProjectRequest(BaseModel):
    name:             str
    notes:            Optional[str] = None
    project_type:     str = "ongoing"
    end_date:         Optional[date] = None
    member_credit_pct: Optional[int] = None
    admin_only:       bool = False

class UpdateProjectRequest(BaseModel):
    name:             Optional[str] = None
    notes:            Optional[str] = None
    project_type:     Optional[str] = None
    end_date:         Optional[date] = None
    member_credit_pct: Optional[int] = None
    admin_only:       Optional[bool] = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/")
def list_projects(
    active_only: bool = False,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    q = db.query(Project)
    if active_only:
        today = date.today()
        q = q.filter(
            (Project.project_type == "ongoing") |
            ((Project.project_type == "one_time") & (
                (Project.end_date == None) | (Project.end_date >= today)
            ))
        )
        # Regular users cannot see admin-only projects when selecting for hours
        if not _user.is_admin:
            q = q.filter(Project.admin_only == 0)
    projects = q.order_by(Project.name).all()
    return [
        {
            "project_id":       p.project_id,
            "name":             p.name,
            "notes":            p.notes,
            "project_type":     p.project_type,
            "end_date":         p.end_date,
            "member_credit_pct": p.member_credit_pct,
            "admin_only":       bool(p.admin_only),
        }
        for p in projects
    ]


@router.post("/", status_code=status.HTTP_201_CREATED)
def create_project(
    payload: CreateProjectRequest,
    db: Session = Depends(get_db),
    _admin: User = Depends(get_current_admin),
):
    if payload.project_type not in ("ongoing", "one_time"):
        raise HTTPException(status_code=400, detail="project_type must be 'ongoing' or 'one_time'")
    if payload.project_type == "one_time" and not payload.end_date:
        raise HTTPException(status_code=400, detail="end_date is required for one_time projects")

    youth_pct = max(0, min(100, payload.member_credit_pct)) if payload.member_credit_pct is not None else 100

    project = Project(
        name=payload.name,
        notes=payload.notes,
        project_type=payload.project_type,
        end_date=payload.end_date,
        member_credit_pct=youth_pct,
        admin_only=int(payload.admin_only),
    )
    db.add(project)
    db.flush()
    log_action(db, user_id=_admin.user_id, action="create", entity_type="project", entity_id=project.project_id,
        details={"summary": f"Created project '{payload.name}'"})
    db.commit()
    db.refresh(project)
    return {"project_id": project.project_id, "detail": "Project created"}


@router.patch("/{project_id}")
def update_project(
    project_id: int,
    payload: UpdateProjectRequest,
    db: Session = Depends(get_db),
    _admin: User = Depends(get_current_admin),
):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    if payload.name             is not None: project.name             = payload.name
    if payload.notes            is not None: project.notes            = payload.notes
    if payload.project_type     is not None: project.project_type     = payload.project_type
    if payload.end_date         is not None: project.end_date         = payload.end_date
    if payload.member_credit_pct is not None: project.member_credit_pct = max(0, min(100, payload.member_credit_pct))
    if payload.admin_only       is not None: project.admin_only       = int(payload.admin_only)

    log_action(db, user_id=_admin.user_id, action="update", entity_type="project", entity_id=project_id,
        details={"summary": f"Updated project '{project.name}'"})
    db.commit()
    return {"detail": "Project updated"}
