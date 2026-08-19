"""
Access control — permission checks
==================================

`user_can()` is the single answer to "may this person do this?", used both by
the route decorator and by the templates to hide navigation a user cannot use.

Two deliberate exceptions:

  * Admins bypass every check, so the system can always be administered.
  * A user with no permissions ticked at all is treated as unrestricted.
    Accounts that predate access control therefore keep working exactly as
    before, and enforcement switches on for a user the moment their first
    permission is ticked. Untick everything to put an account back to
    unrestricted; to lock someone out of a module instead, give them the
    permissions they should have and leave that module unticked.
"""

from functools import wraps

from flask import flash, redirect, request, url_for
from flask_login import current_user


def is_unrestricted(user):
    """True for an account that has never had permissions configured."""
    return not user.user_permissions


def user_can(user, module, action):
    """May `user` perform `action` on `module`?"""
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    if user.is_admin:
        return True
    if is_unrestricted(user):
        return True
    return user.has_permission(module, action)


def user_can_any(user, module, actions=("view",)):
    """Used for navigation — may the user reach the module at all?"""
    return any(user_can(user, module, action) for action in actions)


def require_perm(module, action):
    """
    Route decorator — bounce a user who lacks the permission.

    Redirects with a message rather than a bare 403, so someone clicking a
    stale link lands somewhere useful.
    """
    def decorator(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            if not current_user.is_authenticated:
                return redirect(url_for("login", next=request.path))
            if not user_can(current_user, module, action):
                flash(
                    "You do not have access to that. Ask an administrator to "
                    "grant you the permission if you need it.",
                    "danger",
                )
                return redirect(url_for("dashboard"))
            return view(*args, **kwargs)
        return wrapper
    return decorator
