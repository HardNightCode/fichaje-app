from flask import flash, redirect, render_template, request, url_for
from flask_login import current_user
from sqlalchemy import text

from ..auth import admin_required
from ..extensions import db
from ..logic import (
    get_or_create_schedule_settings,
    obtener_ubicaciones_usuario,
)
from ..models import (
    KioskUser,
    Location,
    QRToken,
    Registro,
    RegistroEdicion,
    RegistroJustificacion,
    Schedule,
    User,
    UserSchedule,
)
from ..routes.auth_routes import crear_qr_token_db
from datetime import datetime


def register_admin_user_routes(app):
    @app.route("/admin/usuarios", methods=["GET", "POST"])
    @admin_required
    def admin_usuarios():
        usuarios = User.query.order_by(User.username).all()
        ubicaciones = Location.query.order_by(Location.name).all()
        flexible = Location.query.filter_by(name="Flexible").first()
        flexible_location_id = flexible.id if flexible else None

        if request.method == "POST":
            for user in usuarios:
                field_name = f"locations_{user.id}[]"
                valores = request.form.getlist(field_name)

                user.locations_multi.clear()
                user.location_id = None

                for v in valores:
                    if not v or v == "borrar":
                        continue
                    try:
                        loc_id = int(v)
                    except ValueError:
                        continue

                    loc = Location.query.get(loc_id)
                    if loc and loc not in user.locations_multi:
                        user.locations_multi.append(loc)

            db.session.commit()
            flash("Ubicaciones de usuarios actualizadas.", "success")
            return redirect(url_for("admin_usuarios"))

        user_locations_map = {}
        for user in usuarios:
            if user.locations_multi:
                user_locations_map[user.id] = list(user.locations_multi)
            elif user.location is not None:
                user_locations_map[user.id] = [user.location]
            else:
                user_locations_map[user.id] = []

        return render_template(
            "admin_usuarios.html",
            usuarios=usuarios,
            ubicaciones=ubicaciones,
            flexible_location_id=flexible_location_id,
            user_locations_map=user_locations_map,
        )

    @app.route("/admin/usuarios/fichas", methods=["GET", "POST"])
    @admin_required
    def admin_usuarios_fichas():
        """
        Lista de usuarios, con enlace a su ficha de configuración.
        """
        usuarios = User.query.order_by(User.username).all()

        if request.method == "POST":
            action = request.form.get("action")
            user_id_str = request.form.get("user_id", "").strip()

            try:
                user_id = int(user_id_str)
                user = User.query.get_or_404(user_id)
            except (ValueError, TypeError):
                flash("Usuario no válido.", "error")
                return redirect(url_for("admin_usuarios_fichas"))

            if action == "delete" and user.id == current_user.id:
                flash("No puedes eliminar tu propio usuario.", "error")
                return redirect(url_for("admin_usuarios_fichas"))

            if action == "delete" and user.username == "admin":
                flash("No puedes eliminar la cuenta 'admin'.", "error")
                return redirect(url_for("admin_usuarios_fichas"))

            if action == "update_role":
                nuevo_rol = request.form.get("role", "").strip()
                if nuevo_rol not in ("admin", "empleado", "kiosko", "kiosko_admin"):
                    flash("Rol no válido.", "error")
                    return redirect(url_for("admin_usuarios_fichas"))

                user.role = nuevo_rol
                db.session.commit()
                flash(f"Rol de {user.username} actualizado a {nuevo_rol}.", "success")
                return redirect(url_for("admin_usuarios_fichas"))

            elif action == "delete":
                from ..models import Kiosk

                kiosko_vinculado = (
                    Kiosk.query
                    .filter(
                        (Kiosk.owner_id == user.id) | (Kiosk.kiosk_account_id == user.id)
                    )
                    .first()
                )
                if kiosko_vinculado:
                    flash(
                        "No puedes eliminar este usuario porque está vinculado a un kiosko "
                        "(propietario o cuenta asociada).",
                        "error",
                    )
                    return redirect(url_for("admin_usuarios_fichas"))

                if user.schedule_settings:
                    db.session.delete(user.schedule_settings)

                db.session.query(UserSchedule).filter(UserSchedule.user_id == user.id).delete(
                    synchronize_session=False
                )
                db.session.execute(
                    text("DELETE FROM user_location WHERE user_id = :user_id"),
                    {"user_id": user.id},
                )
                db.session.query(KioskUser).filter(KioskUser.user_id == user.id).delete(
                    synchronize_session=False
                )
                db.session.query(QRToken).filter(QRToken.user_id == user.id).delete(
                    synchronize_session=False
                )

                registros_subq = db.session.query(Registro.id).filter(
                    Registro.usuario_id == user.id
                ).subquery()
                db.session.query(RegistroEdicion).filter(
                    (RegistroEdicion.registro_id.in_(registros_subq))
                    | (RegistroEdicion.editor_id == user.id)
                ).delete(synchronize_session=False)
                db.session.query(RegistroJustificacion).filter(
                    RegistroJustificacion.registro_id.in_(registros_subq)
                ).delete(synchronize_session=False)
                db.session.query(Registro).filter(Registro.usuario_id == user.id).delete(
                    synchronize_session=False
                )

                db.session.delete(user)
                db.session.commit()
                flash(f"Usuario {user.username} eliminado correctamente.", "success")
                return redirect(url_for("admin_usuarios_fichas"))

            return redirect(url_for("admin_usuarios_fichas"))

        return render_template("admin_usuarios_fichas.html", usuarios=usuarios)

    @app.route("/admin/usuarios/<int:user_id>/reset_password", methods=["POST"])
    @admin_required
    def admin_reset_password(user_id):
        """
        Reinicia la contraseña de un usuario desde la administración.
        """
        user = User.query.get_or_404(user_id)

        new_password = (request.form.get("new_password") or "").strip()
        must_change = request.form.get("must_change_password") == "on"

        if not new_password:
            flash("La nueva contraseña no puede estar vacía.", "error")
            return redirect(url_for("admin_usuarios_fichas"))

        user.set_password(new_password)
        user.must_change_password = must_change
        db.session.commit()

        flash(f"Contraseña de {user.username} reiniciada correctamente.", "success")
        return redirect(url_for("admin_usuarios_fichas"))

    @app.route("/admin/usuarios/<int:user_id>/ficha", methods=["GET", "POST"])
    @admin_required
    def admin_usuario_ficha(user_id):
        """
        Ficha individual de usuario: ubicaciones, horarios y configuración.
        """
        user = User.query.get_or_404(user_id)
        horarios = Schedule.query.order_by(Schedule.name).all()
        settings = get_or_create_schedule_settings(user)

        if request.method == "POST":
            schedule_ids = request.form.getlist("schedule_ids")

            user.schedules.clear()

            for sid in schedule_ids:
                try:
                    sid_int = int(sid)
                except ValueError:
                    continue
                h = Schedule.query.get(sid_int)
                if h and h not in user.schedules:
                    user.schedules.append(h)

            enforce_value = request.form.get("enforce_schedule", "no")
            settings.enforce_schedule = (enforce_value == "si")

            margin_str = request.form.get("margin_minutes", "").strip() or "0"
            try:
                settings.margin_minutes = max(0, int(margin_str))
            except ValueError:
                settings.margin_minutes = 0

            settings.detect_schedule = (request.form.get("detect_schedule") == "on")

            db.session.commit()
            flash("Ficha de usuario actualizada correctamente.", "success")
            return redirect(url_for("admin_usuario_ficha", user_id=user.id))

        ubicaciones_usuario = obtener_ubicaciones_usuario(user)
        horarios_usuario = list(user.schedules)

        return render_template(
            "admin_usuario_ficha.html",
            usuario=user,
            ubicaciones_usuario=ubicaciones_usuario,
            horarios=horarios,
            horarios_usuario=horarios_usuario,
            settings=settings,
        )

    @app.route("/admin/usuarios/<int:user_id>/qr", methods=["GET", "POST"])
    @admin_required
    def admin_usuario_qr(user_id):
        usuario = User.query.get_or_404(user_id)

        if request.method == "POST":
            action = request.form.get("action", "create")
            if action == "create":
                domain = request.form.get("domain", "").strip() or request.host_url.rstrip("/")
                tipo = request.form.get("tipo", "always")
                fecha_hasta = request.form.get("fecha_hasta", "").strip()
                expires = None
                if tipo == "until" and fecha_hasta:
                    try:
                        dt = datetime.strptime(fecha_hasta, "%Y-%m-%d")
                        expires = datetime(dt.year, dt.month, dt.day, 23, 59, 59)
                    except ValueError:
                        flash("Fecha no válida.", "error")
                        return redirect(url_for("admin_usuario_qr", user_id=usuario.id))
                crear_qr_token_db(usuario, domain, expires)
                flash("QR generado correctamente.", "success")
                return redirect(url_for("admin_usuario_qr", user_id=usuario.id))
            elif action == "delete":
                token_id = request.form.get("token_id")
                qr = QRToken.query.filter_by(id=token_id, user_id=usuario.id).first()
                if qr:
                    db.session.delete(qr)
                    db.session.commit()
                    flash("QR eliminado.", "success")
                return redirect(url_for("admin_usuario_qr", user_id=usuario.id))

        tokens = QRToken.query.filter_by(user_id=usuario.id).order_by(QRToken.created_at.desc()).all()
        return render_template("admin_usuario_qr.html", usuario=usuario, tokens=tokens)
