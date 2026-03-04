from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger, Column, Date, DateTime, Enum, ForeignKey,
    Integer, Numeric, String, Text,
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
    end_date     = Column(Date, nullable=True)
    created      = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated      = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    hours        = relationship("Hour", back_populates="project")


class Hour(Base):
    __tablename__ = "hours"

    hour_id        = Column(Integer, primary_key=True, autoincrement=True)
    member_id      = Column(Integer, ForeignKey("users.user_id"), nullable=False)
    project_id     = Column(Integer, ForeignKey("projects.project_id"), nullable=False)
    logged_by      = Column(Integer, ForeignKey("users.user_id"), nullable=False)
    service_date   = Column(Date, nullable=False)
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
