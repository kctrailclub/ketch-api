from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger, Column, Date, DateTime, Enum, ForeignKey,
    Integer, JSON, Numeric, String, Text,
)
from sqlalchemy.orm import relationship

from app.core.database import Base


class Household(Base):
    __tablename__ = "households"

    household_id   = Column(Integer, primary_key=True, autoincrement=True)
    household_code = Column(String(10), nullable=False, unique=True)
    name           = Column(String(100), nullable=False)
    address        = Column(String(255), nullable=False, default="")
    primary_user_id = Column(Integer, ForeignKey("users.user_id", ondelete="SET NULL"), nullable=True)
    created        = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated        = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    members        = relationship("User", foreign_keys="User.household_id", back_populates="household")
    primary_user   = relationship("User", foreign_keys=[primary_user_id])


class User(Base):
    __tablename__ = "users"

    user_id               = Column(Integer, primary_key=True, autoincrement=True)
    firstname             = Column(String(100), nullable=False)
    lastname              = Column(String(100), nullable=False)
    email                 = Column(String(255), nullable=False, unique=True)
    phone                 = Column(String(20), nullable=False, default="")
    password_hash         = Column(String(255), nullable=False)
    household_id          = Column(Integer, ForeignKey("households.household_id", ondelete="SET NULL"), nullable=True)
    household_request_id  = Column(Integer, ForeignKey("households.household_id", ondelete="SET NULL"), nullable=True)
    is_admin              = Column(Integer, nullable=False, default=0)
    is_active             = Column(Integer, nullable=False, default=1)
    waiver                = Column(Date, nullable=True)
    youth                 = Column(Integer, nullable=False, default=0)
    invite_token          = Column(String(64), nullable=True, unique=True)
    invite_expires        = Column(DateTime, nullable=True)
    last_login            = Column(DateTime, nullable=True)
    created               = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated               = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    household             = relationship("Household", foreign_keys=[household_id], back_populates="members")
    hours                 = relationship("Hour", foreign_keys="Hour.member_id", back_populates="member")
    notifications         = relationship("Notification", back_populates="user")


class Project(Base):
    __tablename__ = "projects"

    project_id   = Column(Integer, primary_key=True, autoincrement=True)
    name         = Column(String(255), nullable=False)
    notes        = Column(Text, nullable=True)
    project_type = Column(Enum("one_time", "ongoing"), nullable=False, default="ongoing")
    end_date         = Column(Date, nullable=True)
    member_credit_pct = Column(Integer, nullable=False, default=100)
    admin_only       = Column(Integer, nullable=False, default=0)
    created          = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated          = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    hours            = relationship("Hour", back_populates="project")


class Hour(Base):
    __tablename__ = "hours"

    hour_id        = Column(Integer, primary_key=True, autoincrement=True)
    member_id      = Column(Integer, ForeignKey("users.user_id"), nullable=False)
    project_id     = Column(Integer, ForeignKey("projects.project_id"), nullable=False)
    logged_by      = Column(Integer, ForeignKey("users.user_id"), nullable=False)
    service_date   = Column(Date, nullable=False)
    credit_year    = Column(Integer, nullable=False)
    hours          = Column(Numeric(5, 2), nullable=False)
    notes          = Column(String(255), nullable=True)
    status         = Column(Enum("pending", "approved", "rejected"), nullable=False, default="pending")
    status_note    = Column(String(255), nullable=True)
    status_updated = Column(DateTime, nullable=True)
    status_by      = Column(Integer, ForeignKey("users.user_id"), nullable=True)
    created        = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated        = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    member         = relationship("User", foreign_keys=[member_id], back_populates="hours")
    project        = relationship("Project", back_populates="hours")
    logger         = relationship("User", foreign_keys=[logged_by])
    approver       = relationship("User", foreign_keys=[status_by])


class RegistrationRequest(Base):
    __tablename__ = "registration_requests"

    request_id   = Column(Integer, primary_key=True, autoincrement=True)
    firstname    = Column(String(100), nullable=False)
    lastname     = Column(String(100), nullable=False)
    email        = Column(String(255), nullable=False)
    phone        = Column(String(20), nullable=False, default="")
    status       = Column(Enum("pending", "approved", "rejected"), nullable=False, default="pending")
    reviewed_by  = Column(Integer, ForeignKey("users.user_id", ondelete="SET NULL"), nullable=True)
    reviewed_at  = Column(DateTime, nullable=True)
    created      = Column(DateTime, nullable=False, default=datetime.utcnow)


class Notification(Base):
    __tablename__ = "notifications"

    notification_id   = Column(Integer, primary_key=True, autoincrement=True)
    user_id           = Column(Integer, ForeignKey("users.user_id"), nullable=False)
    notification_type = Column(Enum("hours_pending", "household_request"), nullable=False)
    reference_id      = Column(Integer, nullable=False)
    message           = Column(String(255), nullable=False)
    is_read           = Column(Integer, nullable=False, default=0)
    created           = Column(DateTime, nullable=False, default=datetime.utcnow)

    user              = relationship("User", back_populates="notifications")


class Setting(Base):
    __tablename__ = "settings"

    key   = Column(String(100), primary_key=True)
    value = Column(Text, nullable=True)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    audit_log_id = Column(Integer, primary_key=True, autoincrement=True)
    user_id      = Column(Integer, ForeignKey("users.user_id", ondelete="SET NULL"), nullable=True)
    action       = Column(String(50), nullable=False)
    entity_type  = Column(String(50), nullable=False)
    entity_id    = Column(Integer, nullable=True)
    details      = Column(JSON, nullable=True)
    ip_address   = Column(String(45), nullable=True)
    created      = Column(DateTime, nullable=False, default=datetime.utcnow)

    user         = relationship("User", foreign_keys=[user_id])


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id         = Column(Integer, primary_key=True, autoincrement=True)
    user_id    = Column(Integer, ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False, index=True)
    token_hash = Column(String(64), nullable=False, index=True)
    expires_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class RewardEmail(Base):
    __tablename__ = "reward_emails"

    reward_email_id = Column(Integer, primary_key=True, autoincrement=True)
    household_id    = Column(Integer, ForeignKey("households.household_id", ondelete="CASCADE"), nullable=False)
    email_type      = Column(Enum("reward", "nudge"), nullable=False)
    year            = Column(Integer, nullable=False)
    sent_by         = Column(Integer, ForeignKey("users.user_id", ondelete="SET NULL"), nullable=True)
    sent_at         = Column(DateTime, nullable=False, default=datetime.utcnow)

    household       = relationship("Household", foreign_keys=[household_id])
    sender          = relationship("User", foreign_keys=[sent_by])


class RewardTag(Base):
    __tablename__ = "reward_tags"

    reward_tag_id = Column(Integer, primary_key=True, autoincrement=True)
    household_id  = Column(Integer, ForeignKey("households.household_id", ondelete="CASCADE"), nullable=False)
    year          = Column(Integer, nullable=False)
    tag_number    = Column(Integer, nullable=False)
    assigned_by   = Column(Integer, ForeignKey("users.user_id", ondelete="SET NULL"), nullable=True)
    assigned_at   = Column(DateTime, nullable=False, default=datetime.utcnow)

    household     = relationship("Household", foreign_keys=[household_id])
    assigner      = relationship("User", foreign_keys=[assigned_by])


class ResourceSponsor(Base):
    __tablename__ = "resource_sponsors"

    sponsor_id = Column(Integer, primary_key=True, autoincrement=True)
    name       = Column(String(100), nullable=False)
    logo_url   = Column(String(500), nullable=False)
    website_url = Column(String(500), nullable=False)
    sort_order = Column(Integer, nullable=False, default=0)
    is_active  = Column(Integer, nullable=False, default=1)
    created    = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated    = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class ResourceUpdate(Base):
    __tablename__ = "resource_updates"

    update_id   = Column(Integer, primary_key=True, autoincrement=True)
    title       = Column(String(200), nullable=False)
    body        = Column(Text, nullable=False)
    update_type = Column(Enum("trail", "event", "general"), nullable=False, default="general")
    link_url    = Column(String(500), nullable=True)
    expires_at  = Column(Date, nullable=True)
    is_active   = Column(Integer, nullable=False, default=1)
    sort_order  = Column(Integer, nullable=False, default=0)
    created_by  = Column(Integer, ForeignKey("users.user_id", ondelete="SET NULL"), nullable=True)
    created     = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated     = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    author      = relationship("User", foreign_keys=[created_by])


class ResourceDocument(Base):
    __tablename__ = "resource_documents"

    document_id = Column(Integer, primary_key=True, autoincrement=True)
    category    = Column(String(100), nullable=False)
    title       = Column(String(200), nullable=False)
    description = Column(String(500), nullable=True)
    url         = Column(String(500), nullable=False)
    sort_order  = Column(Integer, nullable=False, default=0)
    is_active   = Column(Integer, nullable=False, default=1)
    created     = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated     = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
