import os
from pathlib import Path

from flask import flash, redirect, render_template, request, url_for, current_app
from werkzeug.utils import secure_filename

from ..auth import admin_required
from ..extensions import db
from ..models import CompanyInfo

ALLOWED_LOGOS = {"png", "jpg", "jpeg", "gif", "webp"}


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_LOGOS


def register_company_routes(app):
    @app.route("/admin/empresa", methods=["GET", "POST"])
    @admin_required
    def admin_empresa():
        info = CompanyInfo.query.first()
        if info is None:
            info = CompanyInfo()
            db.session.add(info)
            db.session.commit()

        if request.method == "POST":
            info.nombre = request.form.get("nombre", "").strip()
            info.cif = request.form.get("cif", "").strip()
            info.direccion = request.form.get("direccion", "").strip()
            info.telefono = request.form.get("telefono", "").strip()
            info.email = request.form.get("email", "").strip()
            info.web = request.form.get("web", "").strip()
            info.descripcion = request.form.get("descripcion", "").strip()

            file = request.files.get("logo")
            if file and file.filename:
                if allowed_file(file.filename):
                    filename = secure_filename(file.filename)
                    upload_dir = Path(current_app.static_folder) / "uploads"
                    upload_dir.mkdir(parents=True, exist_ok=True)
                    target = upload_dir / filename
                    file.save(target)
                    info.logo_path = f"uploads/{filename}"
                else:
                    flash("Formato de logo no permitido. Usa png, jpg, jpeg, gif o webp.", "error")
                    return redirect(url_for("admin_empresa"))

            db.session.commit()
            flash("Datos de la empresa actualizados.", "success")
            return redirect(url_for("admin_empresa"))

        return render_template("admin_empresa.html", info=info)
