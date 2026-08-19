"""
access/routes.py
Blueprint: access
Prefix:    /access

Who may use what. The per-user screen is the main one — tick the functions an
account may reach and save. The matrix is the same data across every user at
once, for a quick audit.
"""

from datetime import datetime

from flask import (
    Blueprint, flash, redirect, render_template, request, url_for
)
from flask_login import current_user, login_required
from sqlalchemy import func, or_

from app import db
from models import Permission, Role, User, UserPermission
from access.catalogue import grouped_permissions
from access.guards import is_unrestricted, require_perm

access_bp = Blueprint("access", __name__, url_prefix="/access")


# ─────────────────────────────────────────────
#  USER LIST
# ─────────────────────────────────────────────

@access_bp.route("/")
@login_required
@require_perm("access", "view")
def index():
    search = request.args.get("search", "").strip()
    role_filter = request.args.get("role_id", type=int)

    query = User.query
    if search:
        query = query.filter(or_(
            User.name.ilike(f"%{search}%"),
            User.username.ilike(f"%{search}%"),
        ))
    if role_filter:
        query = query.filter(User.role_id == role_filter)

    users = query.order_by(User.name).all()

    perm_counts = dict(
        db.session.query(UserPermission.user_id, func.count(UserPermission.id))
        .group_by(UserPermission.user_id)
        .all()
    )
    total_permissions = Permission.query.count()

    return render_template(
        "access/users.html",
        users=users,
        roles=Role.query.order_by(Role.name).all(),
        perm_counts=perm_counts,
        total_permissions=total_permissions,
        search=search,
        role_filter=role_filter,
    )


# ─────────────────────────────────────────────
#  PER-USER PERMISSION TICK LIST
# ─────────────────────────────────────────────

@access_bp.route("/user/<int:user_id>", methods=["GET", "POST"])
@login_required
@require_perm("access", "admin")
def user_permissions(user_id):
    user = User.query.get_or_404(user_id)
    groups = grouped_permissions()

    if request.method == "POST":
        ticked = {int(v) for v in request.form.getlist("permission_id")}
        valid = {p.id for p in Permission.query.all()}
        ticked &= valid

        current = {up.permission_id: up for up in user.user_permissions}

        for permission_id in ticked - set(current):
            db.session.add(UserPermission(
                user_id=user.id,
                permission_id=permission_id,
                granted_by=current_user.id,
                granted_at=datetime.now(),
            ))
        for permission_id in set(current) - ticked:
            db.session.delete(current[permission_id])

        db.session.commit()

        if not ticked:
            flash(
                f"All permissions cleared for {user.name} — the account is "
                "unrestricted again and can reach every module.",
                "warning",
            )
        else:
            flash(f"Access updated for {user.name} — {len(ticked)} permission(s) granted.", "success")
        return redirect(url_for("access.user_permissions", user_id=user.id))

    granted = {up.permission_id for up in user.user_permissions}

    return render_template(
        "access/user_form.html",
        user=user,
        groups=groups,
        granted=granted,
        unrestricted=is_unrestricted(user),
        other_users=User.query.filter(User.id != user.id).order_by(User.name).all(),
    )


@access_bp.route("/user/<int:user_id>/copy", methods=["POST"])
@login_required
@require_perm("access", "admin")
def copy_permissions(user_id):
    """Copy another account's permissions onto this one, replacing what's there."""
    user = User.query.get_or_404(user_id)
    source_id = request.form.get("source_user_id", type=int)
    source = User.query.get(source_id) if source_id else None

    if source is None:
        flash("Choose an account to copy from.", "warning")
        return redirect(url_for("access.user_permissions", user_id=user.id))

    UserPermission.query.filter_by(user_id=user.id).delete()
    for up in source.user_permissions:
        db.session.add(UserPermission(
            user_id=user.id,
            permission_id=up.permission_id,
            granted_by=current_user.id,
            granted_at=datetime.now(),
        ))
    db.session.commit()

    flash(f"Copied {len(source.user_permissions)} permission(s) from {source.name}.", "success")
    return redirect(url_for("access.user_permissions", user_id=user.id))


# ─────────────────────────────────────────────
#  MATRIX  (all users × one module)
# ─────────────────────────────────────────────

@access_bp.route("/matrix")
@login_required
@require_perm("access", "view")
def matrix():
    groups = grouped_permissions()

    modules = [
        (module, label, icon, perms)
        for _group, rows in groups
        for module, label, icon, perms in rows
    ]

    selected = request.args.get("module") or (modules[0][0] if modules else None)
    current = next((m for m in modules if m[0] == selected), modules[0] if modules else None)

    users = User.query.order_by(User.name).all()
    granted = {(up.user_id, up.permission_id) for up in UserPermission.query.all()}
    unrestricted_ids = {u.id for u in users if is_unrestricted(u)}

    return render_template(
        "access/matrix.html",
        groups=groups,
        modules=modules,
        current=current,
        selected=selected,
        users=users,
        granted=granted,
        unrestricted_ids=unrestricted_ids,
        can_edit=True,
    )


@access_bp.route("/toggle", methods=["POST"])
@login_required
@require_perm("access", "admin")
def toggle():
    """Flip one permission for one user — used by the matrix switches."""
    user_id = request.form.get("user_id", type=int)
    permission_id = request.form.get("permission_id", type=int)
    redirect_to = request.form.get("next") or url_for("access.matrix")

    if not user_id or not permission_id:
        flash("Invalid request.", "danger")
        return redirect(redirect_to)

    existing = UserPermission.query.filter_by(
        user_id=user_id, permission_id=permission_id
    ).first()

    if existing:
        db.session.delete(existing)
        db.session.commit()
        flash("Permission revoked.", "info")
    else:
        db.session.add(UserPermission(
            user_id=user_id,
            permission_id=permission_id,
            granted_by=current_user.id,
            granted_at=datetime.now(),
        ))
        db.session.commit()
        flash("Permission granted.", "success")

    return redirect(redirect_to)
