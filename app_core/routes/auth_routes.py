from datetime import datetime
import os
from flask import flash, redirect, render_template, request, url_for, current_app, jsonify
from flask_login import (
    current_user,
    login_required,
    login_user,
    logout_user,
)
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

from ..auth import admin_required
from ..extensions import db
from ..models import User, QRToken
from ..extensions import db
from secrets import token_urlsafe


def _get_qr_serializer():
    secret = current_app.config.get("SECRET_KEY", "cambia-esta-clave-por-una-mas-segura")
    return URLSafeTimedSerializer(secret_key=secret, salt="qr-login")


def _get_portal_sso_serializer():
    secret = os.getenv("PORTAL_SSO_SECRET") or current_app.config.get(
        "SECRET_KEY", "cambia-esta-clave-por-una-mas-segura"
    )
    return URLSafeTimedSerializer(secret_key=secret, salt="portal-sso")


def generar_token_qr(username: str):
    """
    Helper para generar token de login por QR (expira a los 10 minutos).
    Se puede usar desde consola de Flask o añadir una pequeña vista de administración.
    """
    s = _get_qr_serializer()
    return s.dumps({"u": username})
def crear_qr_token_db(user: User, domain: str, expires_at=None):
    tok = token_urlsafe(32)
    qr = QRToken(user_id=user.id, token=tok, domain=domain, expires_at=expires_at)
    db.session.add(qr)
    db.session.commit()
    return qr


def register_auth_routes(app):
    @app.route("/login", methods=["GET", "POST"])
    def login():
        if current_user.is_authenticated:
            if getattr(current_user, "must_change_password", False):
                return redirect(url_for("cambiar_password_obligatorio"))

            if current_user.role == "kiosko":
                return redirect(url_for("kiosko_panel"))
            return redirect(url_for("index"))

        if request.method == "POST":
            username = (request.form.get("username") or "").strip()
            password = request.form.get("password") or ""

            user = User.query.filter_by(username=username).first()

            if user and user.check_password(password):
                login_user(user)
                flash("Sesión iniciada correctamente.", "success")

                if getattr(user, "must_change_password", False):
                    flash("Debes cambiar tu contraseña antes de continuar.", "warning")
                    return redirect(url_for("cambiar_password_obligatorio"))

                if user.role == "kiosko":
                    return redirect(url_for("kiosko_panel"))

                next_page = request.args.get("next")
                if next_page:
                    return redirect(next_page)

                return redirect(url_for("index"))
            else:
                flash("Usuario o contraseña incorrectos.", "error")

        return render_template("login.html")

    @app.route("/cambiar_password_obligatorio", methods=["GET", "POST"])
    @login_required
    def cambiar_password_obligatorio():
        if not current_user.must_change_password:
            return redirect(url_for("index"))

        if request.method == "POST":
            new_password = request.form.get("new_password", "").strip()
            confirm_password = request.form.get("confirm_password", "").strip()

            if not new_password:
                flash("La nueva contraseña no puede estar vacía.", "error")
                return redirect(url_for("cambiar_password_obligatorio"))

            if new_password != confirm_password:
                flash("Las contraseñas no coinciden.", "error")
                return redirect(url_for("cambiar_password_obligatorio"))

            current_user.set_password(new_password)
            current_user.must_change_password = False
            db.session.commit()

            flash("Contraseña actualizada correctamente.", "success")
            return redirect(url_for("index"))

        return render_template("cambiar_password_obligatorio.html")

    @app.route("/logout")
    @login_required
    def logout():
        logout_user()
        flash("Has cerrado sesión", "success")
        return redirect(url_for("login"))

    @app.route("/qr_login")
    def qr_login():
        """
        Login mediante token firmado (para QR).
        Uso: /qr_login?token=...
        El token incluye el username y expira (10 minutos).
        """
        token = request.args.get("token", "").strip()
        if not token:
            flash("Token de acceso no proporcionado.", "error")
            return redirect(url_for("login"))

        # Primero buscamos token persistente en BD (QR móvil)
        qr = QRToken.query.filter_by(token=token, revoked=False).first()
        if qr:
            if qr.expires_at and qr.expires_at < datetime.utcnow():
                flash("Token caducado. Solicita un nuevo QR.", "error")
                return redirect(url_for("login"))
            user = qr.user
        else:
            # Compatibilidad con tokens firmados efímeros
            s = _get_qr_serializer()
            try:
                data = s.loads(token, max_age=600)  # 10 minutos
            except SignatureExpired:
                flash("Token caducado. Solicita un nuevo QR.", "error")
                return redirect(url_for("login"))
            except BadSignature:
                flash("Token inválido.", "error")
                return redirect(url_for("login"))
            username = data.get("u")
            user = User.query.filter_by(username=username).first() if username else None

        if not user:
            flash("Usuario no encontrado.", "error")
            return redirect(url_for("login"))

        login_user(user)
        flash("Sesión iniciada mediante QR.", "success")

        if user.role == "kiosko":
            return redirect(url_for("kiosko_panel"))
        return redirect(url_for("index"))

    @app.route("/portal/sso")
    def portal_sso():
        """
        Login SSO desde el portal.
        Uso: /portal/sso?token=...
        Token firmado y con expiracion corta.
        """
        token = request.args.get("token", "").strip()
        if not token:
            flash("Token no proporcionado.", "error")
            return redirect(url_for("login"))

        s = _get_portal_sso_serializer()
        try:
            data = s.loads(token, max_age=120)
        except SignatureExpired:
            flash("Token caducado.", "error")
            return redirect(url_for("login"))
        except BadSignature:
            flash("Token invalido.", "error")
            return redirect(url_for("login"))

        email = data.get("email")
        domain = data.get("domain")
        host = request.host.split(":")[0]
        if domain and domain != host:
            flash("Token no valido para este dominio.", "error")
            return redirect(url_for("login"))

        user = User.query.filter_by(username=email).first() if email else None
        if not user or user.role != "admin":
            flash("Usuario no autorizado.", "error")
            return redirect(url_for("login"))

        login_user(user)
        flash("Sesion iniciada desde el portal.", "success")
        return redirect(url_for("index"))

    @app.route("/register", methods=["GET", "POST"])
    @admin_required
    def register():
        if request.method == "POST":
            username = request.form.get("username")
            password = request.form.get("password")
            role = request.form.get("role", "empleado")
            email = (request.form.get("email") or "").strip().lower()

            must_change = bool(request.form.get("must_change_password"))

            if not username or not password:
                flash("Usuario y contraseña son obligatorios", "error")
                return redirect(url_for("register"))

            if User.query.filter_by(username=username).first():
                flash("Ese nombre de usuario ya existe", "error")
                return redirect(url_for("register"))

            if email and "@" not in email:
                flash("El correo no es válido.", "error")
                return redirect(url_for("register"))

            if role in ("kiosko", "kiosko_admin") and email:
                flash("Las cuentas de kiosko no pueden tener correo asociado.", "error")
                return redirect(url_for("register"))

            if email and User.query.filter_by(email=email).first():
                flash("Ese correo ya está asociado a otro usuario.", "error")
                return redirect(url_for("register"))

            nuevo_usuario = User(username=username, role=role)
            nuevo_usuario.email = email or None
            nuevo_usuario.set_password(password)
            nuevo_usuario.must_change_password = must_change

            db.session.add(nuevo_usuario)
            db.session.commit()

            flash("Usuario creado correctamente.", "success")
            return redirect(url_for("register"))

        return render_template("register.html")
