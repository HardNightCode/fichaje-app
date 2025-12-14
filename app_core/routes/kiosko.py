from flask import flash, redirect, render_template, url_for, session
from flask_login import current_user, login_required

from ..models import Kiosk


def register_kiosko_routes(app):
    @app.route("/kiosko", methods=["GET"])
    @login_required
    def kiosko_panel():
        """
        Panel de fichaje en modo kiosko.
        Solo accesible para usuarios con rol 'kiosko'.
        """
        if current_user.role != "kiosko":
            flash("Esta sección es solo para cuentas de kiosko.", "error")
            return redirect(url_for("index"))

        kiosk = (
            Kiosk.query
            .filter_by(kiosk_account_id=current_user.id)
            .first()
        )
        if not kiosk:
            flash("Esta cuenta de kiosko no está asociada a ningún kiosko.", "error")
            return redirect(url_for("index"))

        kiosk_users = list(kiosk.kiosk_users)
        last_user_id = session.get("kiosk_last_user_id")

        return render_template(
            "kiosko_panel.html",
            kiosk=kiosk,
            kiosk_users=kiosk_users,
            last_user_id=last_user_id,
        )
