from flask import flash, redirect, render_template, request, url_for

from ..auth import admin_required
from ..extensions import db
from ..models import Location, User


def register_admin_location_routes(app):
    @app.route("/admin/ubicaciones", methods=["GET", "POST"])
    @admin_required
    def admin_ubicaciones():
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            lat = request.form.get("latitude", "").strip()
            lon = request.form.get("longitude", "").strip()
            radius = request.form.get("radius_meters", "").strip()

            if not name or not lat or not lon or not radius:
                flash("Todos los campos son obligatorios.", "error")
                return redirect(url_for("admin_ubicaciones"))

            if name.lower() == "flexible":
                flash("La ubicación 'Flexible' es gestionada por el sistema y no puede crearse ni modificarse desde aquí.", "error")
                return redirect(url_for("admin_ubicaciones"))

            lat = lat.replace(",", ".")
            lon = lon.replace(",", ".")
            radius = radius.replace(",", ".")

            try:
                lat_f = float(lat)
                lon_f = float(lon)
                radius_f = float(radius)
            except ValueError:
                flash("Latitud, longitud y radio deben ser numéricos.", "error")
                return redirect(url_for("admin_ubicaciones"))

            loc = Location(
                name=name,
                latitude=lat_f,
                longitude=lon_f,
                radius_meters=radius_f,
            )
            db.session.add(loc)
            db.session.commit()
            flash("Ubicación creada correctamente.", "success")
            return redirect(url_for("admin_ubicaciones"))

        ubicaciones = (
            Location.query
            .filter(Location.name != "Flexible")
            .order_by(Location.name)
            .all()
        )
        return render_template("admin_ubicaciones.html", ubicaciones=ubicaciones)

    @app.route("/admin/ubicaciones/<int:loc_id>/editar", methods=["GET", "POST"])
    @admin_required
    def editar_ubicacion(loc_id):
        loc = Location.query.get_or_404(loc_id)

        if (loc.name or "").lower() == "flexible":
            flash("La ubicación 'Flexible' es especial del sistema y no puede editarse.", "error")
            return redirect(url_for("admin_ubicaciones"))

        if request.method == "POST":
            name = request.form.get("name", "").strip()
            lat = request.form.get("latitude", "").strip()
            lon = request.form.get("longitude", "").strip()
            radius = request.form.get("radius_meters", "").strip()

            if not name or not lat or not lon or not radius:
                flash("Todos los campos son obligatorios.", "error")
                return redirect(url_for("editar_ubicacion", loc_id=loc.id))

            if name.lower() == "flexible":
                flash("La ubicación 'Flexible' es gestionada por el sistema y no puede crearse ni modificarse desde aquí.", "error")
                return redirect(url_for("admin_ubicaciones"))

            lat = lat.replace(",", ".")
            lon = lon.replace(",", ".")
            radius = radius.replace(",", ".")

            try:
                loc.latitude = float(lat)
                loc.longitude = float(lon)
                loc.radius_meters = float(radius)
            except ValueError:
                flash("Latitud, longitud y radio deben ser numéricos.", "error")
                return redirect(url_for("editar_ubicacion", loc_id=loc.id))

            loc.name = name
            db.session.commit()
            flash("Ubicación actualizada correctamente.", "success")
            return redirect(url_for("admin_ubicaciones"))

        return render_template("admin_ubicacion_editar.html", loc=loc)

    @app.route("/admin/ubicaciones/<int:loc_id>/eliminar", methods=["POST"])
    @admin_required
    def eliminar_ubicacion(loc_id):
        loc = Location.query.get_or_404(loc_id)

        if (loc.name or "").lower() == "flexible":
            flash("La ubicación 'Flexible' es especial del sistema y no puede eliminarse.", "error")
            return redirect(url_for("admin_ubicaciones"))

        usuarios_con_loc = User.query.filter_by(location_id=loc.id).first()
        if usuarios_con_loc:
            flash(
                "No se puede eliminar la ubicación porque está asignada a uno o más usuarios.",
                "error",
            )
            return redirect(url_for("admin_ubicaciones"))

        db.session.delete(loc)
        db.session.commit()
        flash("Ubicación eliminada correctamente.", "success")
        return redirect(url_for("admin_ubicaciones"))
