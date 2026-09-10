from __future__ import annotations
import enum
from datetime import datetime
from sqlalchemy import BigInteger, Boolean, DateTime, Enum, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
class Base(DeclarativeBase): pass
class ApplicationStatus(str, enum.Enum):
    PENDING_POST_CHECK="PENDING_POST_CHECK"; TRACKING_VIEWS="TRACKING_VIEWS"; VERIFICATION_PASSED="VERIFICATION_PASSED"; ADMIN_REVIEW="ADMIN_REVIEW"; APPROVED="APPROVED"; REJECTED="REJECTED"; EXPIRED="EXPIRED"; FAILED="FAILED"
class ActivityScore(str, enum.Enum):
    GOOD="GOOD"; SUSPICIOUS="SUSPICIOUS"; HIGH_RISK="HIGH_RISK"
class User(Base):
    __tablename__="users"
    telegram_id: Mapped[int]=mapped_column(BigInteger, primary_key=True)
    username: Mapped[str|None]=mapped_column(String(255), nullable=True)
    first_name: Mapped[str|None]=mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime]=mapped_column(DateTime, default=datetime.utcnow)
    last_applied_at: Mapped[datetime|None]=mapped_column(DateTime, nullable=True)
    applications: Mapped[list["Application"]]=relationship(back_populates="user")
class Application(Base):
    __tablename__="applications"
    id: Mapped[int]=mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int]=mapped_column(BigInteger, ForeignKey("users.telegram_id"))
    channel_id: Mapped[int]=mapped_column(BigInteger); channel_username: Mapped[str]=mapped_column(String(255)); channel_title: Mapped[str]=mapped_column(String(255))
    post_id: Mapped[int]=mapped_column(BigInteger); post_url: Mapped[str]=mapped_column(String(512))
    initial_views: Mapped[int]=mapped_column(Integer, default=0); current_views: Mapped[int]=mapped_column(Integer, default=0)
    subscriber_count: Mapped[int]=mapped_column(Integer, default=0); average_views: Mapped[float]=mapped_column(Float, default=0.0)
    activity_score: Mapped[ActivityScore]=mapped_column(Enum(ActivityScore), default=ActivityScore.GOOD)
    activity_notes: Mapped[str|None]=mapped_column(Text, nullable=True); referral_link_found: Mapped[bool]=mapped_column(Boolean, default=False)
    status: Mapped[ApplicationStatus]=mapped_column(Enum(ApplicationStatus), default=ApplicationStatus.PENDING_POST_CHECK)
    submitted_at: Mapped[datetime]=mapped_column(DateTime, default=datetime.utcnow); verification_deadline: Mapped[datetime|None]=mapped_column(DateTime, nullable=True)
    admin_decision: Mapped[str|None]=mapped_column(String(32), nullable=True); rejection_reason: Mapped[str|None]=mapped_column(Text, nullable=True); approved_at: Mapped[datetime|None]=mapped_column(DateTime, nullable=True)
    notified_stages: Mapped[str]=mapped_column(String(512), default="")
    user: Mapped["User"]=relationship(back_populates="applications")
    def has_notified(self, stage:str)->bool: return stage in (self.notified_stages or "").split(",")
    def mark_notified(self, stage:str)->None:
        stages=set(filter(None,(self.notified_stages or "").split(","))); stages.add(stage); self.notified_stages=",".join(sorted(stages))
class ApprovedChannel(Base):
    __tablename__="approved_channels"
    id: Mapped[int]=mapped_column(Integer, primary_key=True, autoincrement=True)
    channel_id: Mapped[int]=mapped_column(BigInteger, unique=True); channel_username: Mapped[str]=mapped_column(String(255)); channel_title: Mapped[str]=mapped_column(String(255))
    approved_by: Mapped[int]=mapped_column(BigInteger); approved_at: Mapped[datetime]=mapped_column(DateTime, default=datetime.utcnow); status: Mapped[str]=mapped_column(String(32), default="ACTIVE")
