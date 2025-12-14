from flask import flash, redirect, render_template, request, url_for
from flask_login import current_user
from werkzeug.security import generate_password_hash

from ..auth import kiosko_admin_required
from ..extensions import db
from ..models import Kiosk, KioskUser, User


def register_admin_kiosk_routes(app):
    @app.route("/admin/kioskos", methods=["GET", "POST"])
    @kiosko_admin_required
    def admin_kioskos():
        """
        Gestión de kioskos:
          - admin: ve y gestiona todos
          - kiosko_admin: solo los que tenga como owner
        """
        if current_user.role == "admin":
            kioskos = Kiosk.query.order_by(Kiosk.name).all()
        else:
            kioskos = (
                Kiosk.query
                .filter_by(owner_id=current_user.id)
                .order_by(Kiosk.name)
                .all()
            )

        cuentas_kiosko = User.query.filter_by(role="kiosko").order_by(User.username).all()

        if request.method == "POST":
            name = request.form.get("name", "").strip()
            description = request.form.get("description", "").strip()
            account_id_str = request.form.get("kiosk_account_id", "").strip()

            if not name:
                flash("El nombre del kiosko es obligatorio.", "error")
                return redirect(url_for("admin_kioskos"))

            kiosk = Kiosk(name=name, description=description)
            kiosk.owner_id = current_user.id

            if account_id_str:
                try:
                    acc_id = int(account_id_str)
                    cuenta = User.query.get(acc_id)
                    if cuenta and cuenta.role == "kiosko":
                        kiosk.kiosk_account_id = cuenta.id
                    else:
                        flash("La cuenta seleccionada no es válida como cuenta de kiosko.", "error")
                        return redirect(url_for("admin_kioskos"))
                except ValueError:
                    flash("Cuenta de kiosko no válida.", "error")
                    return redirect(url_for("admin_kioskos"))

            db.session.add(kiosk)
            db.session.commit()
            flash("Kiosko creado correctamente.", "success")
            return redirect(url_for("admin_kioskos"))

        return render_template(
            "admin_kioskos.html",
            kioskos=kioskos,
            cuentas_kiosko=cuentas_kiosko,
        )

    @app.route("/admin/kioskos/<int:kiosk_id>", methods=["GET", "POST"])
    @kiosko_admin_required
    def admin_kiosko_detalle(kiosk_id):
        kiosk = Kiosk.query.get_or_404(kiosk_id)

        if current_user.role == "kiosko_admin" and kiosk.owner_id != current_user.id:
            flash("No tienes permisos para administrar este kiosko.", "error")
            return redirect(url_for("admin_kioskos"))

        usuarios = User.query.filter_by(role="empleado").order_by(User.username).all()
        kiosk_users_map = {ku.user_id: ku for ku in kiosk.kiosk_users}
        cuentas_kiosko = User.query.filter_by(role="kiosko").order_by(User.username).all()
        admins_kiosko = (
            User.query
            .filter(User.role.in_(["admin", "kiosko_admin"]))
            .order_by(User.username)
            .all()
        )

        if request.method == "POST":
            if current_user.role == "admin":
                owner_id_str = request.form.get("owner_id", "").strip()
                if owner_id_str:
                    try:
                        owner_id = int(owner_id_str)
                        owner = User.query.get(owner_id)
                        if owner and owner.role in ("admin", "kiosko_admin"):
                            kiosk.owner_id = owner.id
                        else:
                            flash("El propietario seleccionado no es válido.", "error")
                            return redirect(url_for("admin_kiosko_detalle", kiosk_id=kiosk.id))
                    except ValueError:
                        flash("Propietario seleccionado no válido.", "error")
                        return redirect(url_for("admin_kiosko_detalle", kiosk_id=kiosk.id))

            account_id_str = request.form.get("kiosk_account_id", "").strip()
            if account_id_str:
                try:
                    acc_id = int(account_id_str)
                    cuenta = User.query.get(acc_id)
                    if cuenta and cuenta.role == "kiosko":
                        kiosk.kiosk_account_id = cuenta.id
                    else:
                        flash("La cuenta de kiosko seleccionada no es válida.", "error")
                        return redirect(url_for("admin_kiosko_detalle", kiosk_id=kiosk.id))
                except ValueError:
                    flash("Cuenta de kiosko no válida.", "error")
                    return redirect(url_for("admin_kiosko_detalle", kiosk_id=kiosk.id))
            else:
                kiosk.kiosk_account_id = None

            for u in usuarios:
                enabled = request.form.get(f"user_{u.id}_enabled") == "on"
                pin = (request.form.get(f"user_{u.id}_pin") or "").strip()
                close_flag = request.form.get(f"user_{u.id}_close_session") == "on"

                ku = kiosk_users_map.get(u.id)

                if enabled:
                    if ku is None:
                        if not (pin and pin.isdigit() and len(pin) == 4):
                            flash(f"El usuario {u.username} debe tener un PIN de 4 dígitos.", "error")
                            return redirect(url_for("admin_kiosko_detalle", kiosk_id=kiosk.id))
                        ku = KioskUser(
                            kiosk_id=kiosk.id,
                            user_id=u.id,
                            pin_hash=generate_password_hash(pin),
                            close_session_after_punch=close_flag,
                        )
                        db.session.add(ku)
                    else:
                        if pin:
                            if not (pin.isdigit() and len(pin) == 4):
                                flash(f"El usuario {u.username} debe tener un PIN de 4 dígitos.", "error")
                                return redirect(url_for("admin_kiosko_detalle", kiosk_id=kiosk.id))
                            ku.pin_hash = generate_password_hash(pin)
                        ku.close_session_after_punch = close_flag
                else:
                    if ku is not None:
                        db.session.delete(ku)

            db.session.commit()
            flash("Configuración del kiosko actualizada correctamente.", "success")
            return redirect(url_for("admin_kiosko_detalle", kiosk_id=kiosk.id))

        return render_template(
            "admin_kiosko_detalle.html",
            kiosk=kiosk,
            usuarios=usuarios,
            kiosk_users_map=kiosk_users_map,
            cuentas_kiosko=cuentas_kiosko,
            admins_kiosko=admins_kiosko,
        )
