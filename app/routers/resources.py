from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.audit import log_action
from app.core.database import get_db
from app.core.dependencies import get_current_admin, get_current_user
from app.models.models import ResourceDocument, ResourceSponsor, ResourceUpdate, User

router = APIRouter(prefix="/resources", tags=["resources"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class SponsorCreate(BaseModel):
    name:        str
    logo_url:    str
    website_url: str
    sort_order:  int = 0

class SponsorUpdate(BaseModel):
    name:        Optional[str] = None
    logo_url:    Optional[str] = None
    website_url: Optional[str] = None
    sort_order:  Optional[int] = None
    is_active:   Optional[int] = None

class UpdateCreate(BaseModel):
    title:       str
    body:        str
    update_type: str = "general"
    link_url:    Optional[str] = None
    expires_at:  Optional[date] = None
    sort_order:  int = 0

class UpdateEdit(BaseModel):
    title:       Optional[str]  = None
    body:        Optional[str]  = None
    update_type: Optional[str]  = None
    link_url:    Optional[str]  = None
    expires_at:  Optional[date] = None
    sort_order:  Optional[int]  = None
    is_active:   Optional[int]  = None

class DocumentCreate(BaseModel):
    category:    str
    title:       str
    description: Optional[str] = None
    url:         str
    sort_order:  int = 0

class DocumentUpdate(BaseModel):
    category:    Optional[str] = None
    title:       Optional[str] = None
    description: Optional[str] = None
    url:         Optional[str] = None
    sort_order:  Optional[int] = None
    is_active:   Optional[int] = None


# ---------------------------------------------------------------------------
# Sponsors
# ---------------------------------------------------------------------------

@router.get("/sponsors")
def list_sponsors(
    include_inactive: bool = False,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    q = db.query(ResourceSponsor)
    if not include_inactive:
        q = q.filter(ResourceSponsor.is_active == 1)
    sponsors = q.order_by(ResourceSponsor.sort_order, ResourceSponsor.name).all()
    return [
        {
            "sponsor_id":  s.sponsor_id,
            "name":        s.name,
            "logo_url":    s.logo_url,
            "website_url": s.website_url,
            "sort_order":  s.sort_order,
            "is_active":   s.is_active,
        }
        for s in sponsors
    ]


@router.post("/sponsors", status_code=status.HTTP_201_CREATED)
def create_sponsor(
    payload: SponsorCreate,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    sponsor = ResourceSponsor(
        name=payload.name,
        logo_url=payload.logo_url,
        website_url=payload.website_url,
        sort_order=payload.sort_order,
    )
    db.add(sponsor)
    db.flush()
    log_action(db, user_id=admin.user_id, action="create", entity_type="sponsor", entity_id=sponsor.sponsor_id,
        details={"summary": f"Added sponsor: {payload.name}"})
    db.commit()
    return {"sponsor_id": sponsor.sponsor_id, "detail": "Sponsor created"}


@router.patch("/sponsors/{sponsor_id}")
def update_sponsor(
    sponsor_id: int,
    payload: SponsorUpdate,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    sponsor = db.get(ResourceSponsor, sponsor_id)
    if not sponsor:
        raise HTTPException(status_code=404, detail="Sponsor not found")

    for field in ("name", "logo_url", "website_url", "sort_order", "is_active"):
        val = getattr(payload, field)
        if val is not None:
            setattr(sponsor, field, val)

    log_action(db, user_id=admin.user_id, action="update", entity_type="sponsor", entity_id=sponsor_id,
        details={"summary": f"Updated sponsor: {sponsor.name}"})
    db.commit()
    return {"detail": "Sponsor updated"}


@router.delete("/sponsors/{sponsor_id}")
def delete_sponsor(
    sponsor_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    sponsor = db.get(ResourceSponsor, sponsor_id)
    if not sponsor:
        raise HTTPException(status_code=404, detail="Sponsor not found")

    log_action(db, user_id=admin.user_id, action="delete", entity_type="sponsor", entity_id=sponsor_id,
        details={"summary": f"Deleted sponsor: {sponsor.name}"})
    db.delete(sponsor)
    db.commit()
    return {"detail": "Sponsor deleted"}


# ---------------------------------------------------------------------------
# Updates / Announcements
# ---------------------------------------------------------------------------

@router.get("/updates")
def list_updates(
    include_inactive: bool = False,
    include_expired: bool = False,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    q = db.query(ResourceUpdate)
    if not include_inactive:
        q = q.filter(ResourceUpdate.is_active == 1)
    if not include_expired:
        q = q.filter(
            (ResourceUpdate.expires_at == None) | (ResourceUpdate.expires_at >= date.today())
        )
    updates = q.order_by(ResourceUpdate.sort_order, ResourceUpdate.created.desc()).all()
    return [
        {
            "update_id":   u.update_id,
            "title":       u.title,
            "body":        u.body,
            "update_type": u.update_type,
            "link_url":    u.link_url,
            "expires_at":  u.expires_at,
            "is_active":   u.is_active,
            "sort_order":  u.sort_order,
            "created":     u.created,
        }
        for u in updates
    ]


@router.post("/updates", status_code=status.HTTP_201_CREATED)
def create_update(
    payload: UpdateCreate,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    if payload.update_type not in ("trail", "event", "general"):
        raise HTTPException(status_code=400, detail="update_type must be 'trail', 'event', or 'general'")

    update = ResourceUpdate(
        title=payload.title,
        body=payload.body,
        update_type=payload.update_type,
        link_url=payload.link_url,
        expires_at=payload.expires_at,
        sort_order=payload.sort_order,
        created_by=admin.user_id,
    )
    db.add(update)
    db.flush()
    log_action(db, user_id=admin.user_id, action="create", entity_type="resource_update", entity_id=update.update_id,
        details={"summary": f"Added update: {payload.title}"})
    db.commit()
    return {"update_id": update.update_id, "detail": "Update created"}


@router.patch("/updates/{update_id}")
def edit_update(
    update_id: int,
    payload: UpdateEdit,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    update = db.get(ResourceUpdate, update_id)
    if not update:
        raise HTTPException(status_code=404, detail="Update not found")

    if payload.update_type is not None and payload.update_type not in ("trail", "event", "general"):
        raise HTTPException(status_code=400, detail="update_type must be 'trail', 'event', or 'general'")

    for field in ("title", "body", "update_type", "link_url", "expires_at", "sort_order", "is_active"):
        val = getattr(payload, field)
        if val is not None:
            setattr(update, field, val)

    log_action(db, user_id=admin.user_id, action="update", entity_type="resource_update", entity_id=update_id,
        details={"summary": f"Updated: {update.title}"})
    db.commit()
    return {"detail": "Update saved"}


@router.delete("/updates/{update_id}")
def delete_update(
    update_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    update = db.get(ResourceUpdate, update_id)
    if not update:
        raise HTTPException(status_code=404, detail="Update not found")

    log_action(db, user_id=admin.user_id, action="delete", entity_type="resource_update", entity_id=update_id,
        details={"summary": f"Deleted update: {update.title}"})
    db.delete(update)
    db.commit()
    return {"detail": "Update deleted"}


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------

@router.get("/documents")
def list_documents(
    include_inactive: bool = False,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    q = db.query(ResourceDocument)
    if not include_inactive:
        q = q.filter(ResourceDocument.is_active == 1)
    docs = q.order_by(ResourceDocument.category, ResourceDocument.sort_order, ResourceDocument.title).all()
    return [
        {
            "document_id": d.document_id,
            "category":    d.category,
            "title":       d.title,
            "description": d.description,
            "url":         d.url,
            "sort_order":  d.sort_order,
            "is_active":   d.is_active,
        }
        for d in docs
    ]


@router.post("/documents", status_code=status.HTTP_201_CREATED)
def create_document(
    payload: DocumentCreate,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    doc = ResourceDocument(
        category=payload.category,
        title=payload.title,
        description=payload.description,
        url=payload.url,
        sort_order=payload.sort_order,
    )
    db.add(doc)
    db.flush()
    log_action(db, user_id=admin.user_id, action="create", entity_type="resource_document", entity_id=doc.document_id,
        details={"summary": f"Added document: {payload.title} ({payload.category})"})
    db.commit()
    return {"document_id": doc.document_id, "detail": "Document created"}


@router.patch("/documents/{document_id}")
def update_document(
    document_id: int,
    payload: DocumentUpdate,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    doc = db.get(ResourceDocument, document_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    for field in ("category", "title", "description", "url", "sort_order", "is_active"):
        val = getattr(payload, field)
        if val is not None:
            setattr(doc, field, val)

    log_action(db, user_id=admin.user_id, action="update", entity_type="resource_document", entity_id=document_id,
        details={"summary": f"Updated document: {doc.title}"})
    db.commit()
    return {"detail": "Document updated"}


@router.delete("/documents/{document_id}")
def delete_document(
    document_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    doc = db.get(ResourceDocument, document_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    log_action(db, user_id=admin.user_id, action="delete", entity_type="resource_document", entity_id=document_id,
        details={"summary": f"Deleted document: {doc.title}"})
    db.delete(doc)
    db.commit()
    return {"detail": "Document deleted"}
