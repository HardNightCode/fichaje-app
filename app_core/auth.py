from functools import wraps
from flask import flash, redirect, url_for
from flask_login import current_user, login_required

from .extensions import login_manager
from .models import User


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


def admin_required(view_func):
    @wraps(view_func)
    @login_required
    def wrapped_view(*args, **kwargs):
        if current_user.role != "admin":
            flash("No tienes permisos para acceder a esta sección.", "error")
            return redirect(url_for("index"))
        return view_func(*args, **kwargs)

    return wrapped_view


def kiosko_admin_required(view_func):
    """
    Permite acceso a:
      - admin  (ve y gestiona todos los kioskos)
      - kiosko_admin (solo los suyos)
    """
    @wraps(view_func)
    @login_required
    def wrapped_view(*args, **kwargs):
        if current_user.role not in ("admin", "kiosko_admin"):
            flash("No tienes permisos para acceder a esta sección de kioskos.", "error")
            return redirect(url_for("index"))
        return view_func(*args, **kwargs)

    return wrapped_view
