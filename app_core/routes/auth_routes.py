from flask import flash, redirect, render_template, request, url_for
from flask_login import (
    current_user,
    login_required,
    login_user,
    logout_user,
)

from ..auth import admin_required
from ..extensions import db
from ..models import User


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

    @app.route("/register", methods=["GET", "POST"])
    @admin_required
    def register():
        if request.method == "POST":
            username = request.form.get("username")
            password = request.form.get("password")
            role = request.form.get("role", "empleado")

            must_change = bool(request.form.get("must_change_password"))

            if not username or not password:
                flash("Usuario y contraseña son obligatorios", "error")
                return redirect(url_for("register"))

            if User.query.filter_by(username=username).first():
                flash("Ese nombre de usuario ya existe", "error")
                return redirect(url_for("register"))

            nuevo_usuario = User(username=username, role=role)
            nuevo_usuario.set_password(password)
            nuevo_usuario.must_change_password = must_change

            db.session.add(nuevo_usuario)
            db.session.commit()

            flash("Usuario creado correctamente.", "success")
            return redirect(url_for("register"))

        return render_template("register.html")
