"""Database engine/session setup plus repository-style helpers."""
from __future__ import annotations
from contextlib import contextmanager
from datetime import datetime
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker
from .config import settings
from .models import Application, ApplicationStatus, ApprovedChannel, Base, User

connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=connect_args, future=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)
ACTIVE_STATUSES = (ApplicationStatus.PENDING_POST_CHECK, ApplicationStatus.TRACKING_VIEWS, ApplicationStatus.VERIFICATION_PASSED, ApplicationStatus.ADMIN_REVIEW)


def init_db() -> None:
    Base.metadata.create_all(engine)
    # create_all does not add columns to an existing database. Keep this small
    # migration here so existing deployments gain moderation fields automatically.
    columns = {column["name"] for column in inspect(engine).get_columns("users")}
    additions = {
        "is_blocked": "BOOLEAN NOT NULL DEFAULT FALSE",
        "blocked_at": "DATETIME",
        "blocked_by": "BIGINT",
        "block_reason": "TEXT",
    }
    with engine.begin() as connection:
        for name, definition in additions.items():
            if name not in columns:
                connection.execute(text(f"ALTER TABLE users ADD COLUMN {name} {definition}"))


@contextmanager
def session_scope():
    session: Session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_or_create_user(session: Session, telegram_id: int, username: str | None, first_name: str | None) -> User:
    user = session.get(User, telegram_id)
    if user is None:
        user = User(telegram_id=telegram_id, username=username, first_name=first_name)
        session.add(user); session.flush()
    else:
        user.username = username or user.username
        user.first_name = first_name or user.first_name
    return user


def get_user(session: Session, telegram_id: int) -> User | None:
    return session.get(User, telegram_id)


def is_user_blocked(session: Session, telegram_id: int) -> bool:
    user = session.get(User, telegram_id)
    return bool(user and user.is_blocked)


def block_user(session: Session, telegram_id: int, blocked_by: int, reason: str | None = None) -> User | None:
    user = session.get(User, telegram_id)
    if user is None:
        return None
    user.is_blocked = True
    user.blocked_at = datetime.utcnow()
    user.blocked_by = blocked_by
    user.block_reason = reason
    return user


def unblock_user(session: Session, telegram_id: int) -> User | None:
    user = session.get(User, telegram_id)
    if user is None:
        return None
    user.is_blocked = False
    user.blocked_at = None
    user.blocked_by = None
    user.block_reason = None
    return user


def get_active_application_for_channel(session: Session, channel_id: int) -> Application | None:
    return session.query(Application).filter(Application.channel_id == channel_id).filter(Application.status.in_(ACTIVE_STATUSES)).order_by(Application.submitted_at.desc()).first()

def get_active_application_for_post(session: Session, post_url: str) -> Application | None:
    return session.query(Application).filter(Application.post_url == post_url).filter(Application.status.in_(ACTIVE_STATUSES)).first()

def get_user_active_application(session: Session, user_id: int) -> Application | None:
    return session.query(Application).filter(Application.user_id == user_id).filter(Application.status.in_(ACTIVE_STATUSES)).order_by(Application.submitted_at.desc()).first()

def get_applications_in_tracking(session: Session) -> list[Application]:
    return session.query(Application).filter(Application.status == ApplicationStatus.TRACKING_VIEWS).all()

def get_applications_awaiting_admin(session: Session) -> list[Application]:
    return session.query(Application).filter(Application.status == ApplicationStatus.ADMIN_REVIEW).order_by(Application.submitted_at.asc()).all()

def get_application(session: Session, application_id: int) -> Application | None:
    return session.get(Application, application_id)

def is_channel_already_approved(session: Session, channel_id: int) -> bool:
    return session.query(ApprovedChannel).filter(ApprovedChannel.channel_id == channel_id, ApprovedChannel.status == "ACTIVE").first() is not None

def add_approved_channel(session: Session, application: Application, approved_by: int) -> ApprovedChannel:
    existing = session.query(ApprovedChannel).filter(ApprovedChannel.channel_id == application.channel_id).first()
    if existing:
        existing.status = "ACTIVE"; existing.approved_by = approved_by; existing.approved_at = datetime.utcnow(); existing.channel_username = application.channel_username; existing.channel_title = application.channel_title
        return existing
    approved = ApprovedChannel(channel_id=application.channel_id, channel_username=application.channel_username, channel_title=application.channel_title, approved_by=approved_by, approved_at=datetime.utcnow(), status="ACTIVE")
    session.add(approved)
    return approved
