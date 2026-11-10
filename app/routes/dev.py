"""
Developer quick-login blueprint.

Only registered when DEV_LOGIN_ENABLED=true in .env (DevelopmentConfig only).
Never available in production — ProductionConfig hardcodes DEV_LOGIN_ENABLED=False
and does not read the env var, so this blueprint cannot be accidentally enabled.

Provides:
  GET  /dev/                     — lists available dev accounts
  POST /dev/login-as/<role>      — instantly logs in as the dev account for that role
"""

from flask import Blueprint, Response, abort, flash, redirect, url_for
from flask_login import login_user

from app.extensions import db
from app.models.user import UserAccount

dev_bp = Blueprint("dev", __name__, url_prefix="/dev")

# Canonical dev account emails — must match seed_dev.py
DEV_ACCOUNTS: list[dict] = [
    {
        "role": "admin",
        "label": "Admin",
        "surname": "Testovací",
        "first_name": "Rudolf",
        "email": "dev.admin@medcover.local",
        "description": "Full system access",
    },
    {
        "role": "coordinator",
        "label": "Coordinator",
        "surname": "Smyšlená",
        "first_name": "Marie Anna",
        "email": "dev.coordinator@medcover.local",
        "description": "Create/manage master events and events",
    },
    {
        "role": "member",
        "label": "Member",
        "surname": "Vymyšlený",
        "first_name": "Jiří",
        "email": "dev.member@medcover.local",
        "description": "Join events, submit debriefings",
    },
    {
        "role": "external",
        "label": "External",
        "surname": "Zkušební",
        "first_name": "Božena",
        "email": "dev.external@medcover.local",
        "description": "Join eligible events as an external collaborator",
    },
    {
        "role": "viewer",
        "label": "Viewer",
        "surname": "Fiktivní",
        "first_name": "Ludmila",
        "email": "dev.viewer@medcover.local",
        "description": "Read-only access",
    },
    {
        "role": "debrief_manager",
        "label": "Debriefing Manager",
        "surname": "Neexistující",
        "first_name": "Oldřich",
        "email": "dev.debrief@medcover.local",
        "description": "View and manage confidential debriefing records",
    },
    {
        "role": "inactive",
        "label": "Inactive (pending activation)",
        "surname": "Nepravdivý",
        "first_name": "Bohumil",
        "email": "dev.inactive@medcover.local",
        "description": "Registered but not yet activated by admin",
    },
]


@dev_bp.post("/login-as/<role>")
def login_as(role: str) -> Response:
    account = next((a for a in DEV_ACCOUNTS if a["role"] == role), None)
    if account is None:
        abort(404)

    user = db.session.scalar(db.select(UserAccount).where(UserAccount.email == account["email"]))
    if user is None:
        flash(
            f"Dev account '{account['email']}' not found. Run scripts/seed_dev.py first.",
            "warning",
        )
        return redirect(url_for("auth.login"))

    login_user(user, force=True)  # force=True bypasses is_active check (intentional for dev)
    return redirect(url_for("main.dashboard"))
