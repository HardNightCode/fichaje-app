from datetime import datetime, time

from flask import flash, redirect, render_template, request, url_for

from ..auth import admin_required
from ..extensions import db
from ..models import Schedule, ScheduleDay, UserSchedule


def register_admin_schedule_routes(app):
    @app.route("/admin/horarios", methods=["GET", "POST"])
    @admin_required
    def admin_horarios():
        """
        Página para crear y listar horarios.
        - Puede crear:
            * Horario simple (mismas horas todos los días).
            * Horario por días (cada día con su inicio/fin/descanso).
        """
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            use_per_day = bool(request.form.get("use_per_day"))

            if not name:
                flash("El nombre del horario es obligatorio.", "error")
                return redirect(url_for("admin_horarios"))

            start_time_val = None
            end_time_val = None
            break_type = "none"
            break_start = None
            break_end = None
            break_minutes = None
            break_optional = False
            break_paid = False

            if not use_per_day:
                start_time_str = request.form.get("start_time", "").strip()
                end_time_str = request.form.get("end_time", "").strip()
                break_type = request.form.get("break_type", "none")

                break_start_str = request.form.get("break_start", "").strip()
                break_end_str = request.form.get("break_end", "").strip()
                break_minutes_str = request.form.get("break_minutes", "").strip()
                break_optional = bool(request.form.get("break_optional"))
                break_paid = bool(request.form.get("break_paid"))
                break_unpaid = bool(request.form.get("break_unpaid"))
                break_optional = bool(request.form.get("break_optional"))
                break_paid = bool(request.form.get("break_paid"))
                break_unpaid = bool(request.form.get("break_unpaid"))

                if not start_time_str or not end_time_str:
                    flash("Inicio y fin de jornada son obligatorios en modo simple.", "error")
                    return redirect(url_for("admin_horarios"))

                try:
                    start_time_val = datetime.strptime(start_time_str, "%H:%M").time()
                    end_time_val = datetime.strptime(end_time_str, "%H:%M").time()
                except ValueError:
                    flash("Las horas de inicio y fin deben tener formato HH:MM.", "error")
                    return redirect(url_for("admin_horarios"))

                if break_type == "fixed":
                    if not break_start_str or not break_end_str:
                        flash("Para descanso fijo debes indicar inicio y fin de descanso.", "error")
                        return redirect(url_for("admin_horarios"))
                    try:
                        break_start = datetime.strptime(break_start_str, "%H:%M").time()
                        break_end = datetime.strptime(break_end_str, "%H:%M").time()
                    except ValueError:
                        flash("Las horas de descanso deben tener formato HH:MM.", "error")
                        return redirect(url_for("admin_horarios"))
                elif break_type == "flexible":
                    if not break_minutes_str:
                        flash("Para descanso flexible debes indicar los minutos de descanso.", "error")
                        return redirect(url_for("admin_horarios"))
                    try:
                        break_minutes = int(break_minutes_str)
                    except ValueError:
                        flash("Los minutos de descanso deben ser numéricos.", "error")
                        return redirect(url_for("admin_horarios"))

                if break_type == "none":
                    break_optional = False
                    break_paid = False
                else:
                    if break_paid and break_unpaid:
                        break_paid = True
                    elif not break_paid and not break_unpaid:
                        break_paid = False

            if use_per_day:
                start_time_val = time(0, 0)
                end_time_val = time(23, 59)
                break_type = "none"
                break_start = None
                break_end = None
                break_minutes = None
                break_optional = False
                break_paid = False

            horario = Schedule(
                name=name,
                start_time=start_time_val,
                end_time=end_time_val,
                break_type=break_type,
                break_start=break_start,
                break_end=break_end,
                break_minutes=break_minutes,
                break_optional=break_optional,
                break_paid=break_paid,
                use_per_day=use_per_day,
            )
            db.session.add(horario)
            db.session.flush()

            if use_per_day:
                dias = [
                    ("mon", 0),
                    ("tue", 1),
                    ("wed", 2),
                    ("thu", 3),
                    ("fri", 4),
                    ("sat", 5),
                    ("sun", 6),
                ]

                tiene_algun_dia = False

                for prefix, dow in dias:
                    s_str = request.form.get(f"{prefix}_start", "").strip()
                    e_str = request.form.get(f"{prefix}_end", "").strip()
                    if not s_str or not e_str:
                        continue

                    try:
                        s_time = datetime.strptime(s_str, "%H:%M").time()
                        e_time = datetime.strptime(e_str, "%H:%M").time()
                    except ValueError:
                        flash(f"Hora inválida en el día {prefix.upper()} (formato HH:MM).", "error")
                        db.session.rollback()
                        return redirect(url_for("admin_horarios"))

                    b_type = request.form.get(f"{prefix}_break_type", "none")
                    bs = be = None
                    bmin = None
                    b_optional = bool(request.form.get(f"{prefix}_break_optional"))
                    b_paid = bool(request.form.get(f"{prefix}_break_paid"))
                    b_unpaid = bool(request.form.get(f"{prefix}_break_unpaid"))

                    if b_type == "fixed":
                        bs_str = request.form.get(f"{prefix}_break_start", "").strip()
                        be_str = request.form.get(f"{prefix}_break_end", "").strip()
                        if not bs_str or not be_str:
                            flash("Para descanso fijo debes indicar inicio y fin de descanso en cada día.", "error")
                            db.session.rollback()
                            return redirect(url_for("admin_horarios"))
                        try:
                            bs = datetime.strptime(bs_str, "%H:%M").time()
                            be = datetime.strptime(be_str, "%H:%M").time()
                        except ValueError:
                            flash("Las horas de descanso diario deben tener formato HH:MM.", "error")
                            db.session.rollback()
                            return redirect(url_for("admin_horarios"))
                    elif b_type == "flexible":
                        bmin_str = request.form.get(f"{prefix}_break_minutes", "").strip()
                        if not bmin_str:
                            flash("Para descanso flexible debes indicar los minutos de descanso en cada día.", "error")
                            db.session.rollback()
                            return redirect(url_for("admin_horarios"))
                        try:
                            bmin = int(bmin_str)
                        except ValueError:
                            flash("Los minutos de descanso diario deben ser numéricos.", "error")
                            db.session.rollback()
                            return redirect(url_for("admin_horarios"))

                    if b_type == "none":
                        b_optional = False
                        b_paid = False
                    else:
                        if b_paid and b_unpaid:
                            b_paid = True
                        elif not b_paid and not b_unpaid:
                            b_paid = False

                    dia_obj = ScheduleDay(
                        schedule_id=horario.id,
                        day_of_week=dow,
                        start_time=s_time,
                        end_time=e_time,
                        break_type=b_type,
                        break_start=bs,
                        break_end=be,
                        break_minutes=bmin,
                        break_optional=b_optional,
                        break_paid=b_paid,
                    )
                    db.session.add(dia_obj)
                    tiene_algun_dia = True

                if not tiene_algun_dia:
                    flash("En modo por días, al menos un día debe tener horario.", "error")
                    db.session.rollback()
                    return redirect(url_for("admin_horarios"))

            try:
                db.session.commit()
                flash("Horario creado correctamente.", "success")
            except Exception:
                db.session.rollback()
                flash("Se ha producido un error al crear el horario.", "error")

            return redirect(url_for("admin_horarios"))

        horarios = Schedule.query.order_by(Schedule.name).all()
        return render_template("admin_horarios.html", horarios=horarios)

    @app.route("/admin/horarios/<int:schedule_id>/eliminar", methods=["POST"])
    @admin_required
    def eliminar_horario(schedule_id):
        """
        Elimina un horario, siempre que no esté asignado a ningún usuario.
        """
        horario = Schedule.query.get_or_404(schedule_id)

        try:
            asignados = horario.users.count()
        except Exception:
            asignados = len(horario.users or [])

        if asignados > 0:
            flash(
                "No se puede eliminar el horario porque está asignado a uno o más usuarios.",
                "error",
            )
            return redirect(url_for("admin_horarios"))

        UserSchedule.query.filter_by(schedule_id=schedule_id).delete(
            synchronize_session=False
        )

        db.session.delete(horario)
        db.session.commit()
        flash("Horario eliminado correctamente.", "success")
        return redirect(url_for("admin_horarios"))

    @app.route("/admin/horarios/<int:schedule_id>/editar", methods=["GET", "POST"])
    @admin_required
    def editar_horario(schedule_id):
        horario = Schedule.query.get_or_404(schedule_id)

        if request.method == "POST":
            name = request.form.get("name", "").strip()
            use_per_day = bool(request.form.get("use_per_day"))

            if not name:
                flash("El nombre del horario es obligatorio.", "error")
                return redirect(url_for("editar_horario", schedule_id=horario.id))

            horario.name = name
            horario.use_per_day = use_per_day

            if not use_per_day:
                start_time_str = request.form.get("start_time", "").strip()
                end_time_str = request.form.get("end_time", "").strip()
                break_type = request.form.get("break_type", "none")

                break_start_str = request.form.get("break_start", "").strip()
                break_end_str = request.form.get("break_end", "").strip()
                break_minutes_str = request.form.get("break_minutes", "").strip()

                if not start_time_str or not end_time_str:
                    flash("Inicio y fin de jornada son obligatorios en modo simple.", "error")
                    return redirect(url_for("editar_horario", schedule_id=horario.id))

                try:
                    horario.start_time = datetime.strptime(start_time_str, "%H:%M").time()
                    horario.end_time = datetime.strptime(end_time_str, "%H:%M").time()
                except ValueError:
                    flash("Las horas de inicio y fin deben tener formato HH:MM.", "error")
                    return redirect(url_for("editar_horario", schedule_id=horario.id))

                horario.break_type = break_type
                horario.break_start = None
                horario.break_end = None
                horario.break_minutes = None
                horario.break_optional = False
                horario.break_paid = False

                if break_type == "fixed":
                    if not break_start_str or not break_end_str:
                        flash("Para descanso fijo debes indicar inicio y fin de descanso.", "error")
                        return redirect(url_for("editar_horario", schedule_id=horario.id))
                    try:
                        horario.break_start = datetime.strptime(break_start_str, "%H:%M").time()
                        horario.break_end = datetime.strptime(break_end_str, "%H:%M").time()
                    except ValueError:
                        flash("Las horas de descanso deben tener formato HH:MM.", "error")
                        return redirect(url_for("editar_horario", schedule_id=horario.id))
                elif break_type == "flexible":
                    if not break_minutes_str:
                        flash("Para descanso flexible debes indicar los minutos de descanso.", "error")
                        return redirect(url_for("editar_horario", schedule_id=horario.id))
                    try:
                        horario.break_minutes = int(break_minutes_str)
                    except ValueError:
                        flash("Los minutos de descanso deben ser numéricos.", "error")
                        return redirect(url_for("editar_horario", schedule_id=horario.id))

                if break_type == "none":
                    break_optional = False
                    break_paid = False
                else:
                    if break_paid and break_unpaid:
                        break_paid = True
                    elif not break_paid and not break_unpaid:
                        break_paid = False

                horario.break_optional = break_optional
                horario.break_paid = break_paid

                horario.days.clear()

            else:
                horario.start_time = time(0, 0)
                horario.end_time = time(23, 59)
                horario.break_type = "none"
                horario.break_start = None
                horario.break_end = None
                horario.break_minutes = None
                horario.break_optional = False
                horario.break_paid = False

                horario.days.clear()

                dias = [
                    ("mon", 0),
                    ("tue", 1),
                    ("wed", 2),
                    ("thu", 3),
                    ("fri", 4),
                    ("sat", 5),
                    ("sun", 6),
                ]

                tiene_algun_dia = False

                for prefix, dow in dias:
                    s_str = request.form.get(f"{prefix}_start", "").strip()
                    e_str = request.form.get(f"{prefix}_end", "").strip()
                    if not s_str or not e_str:
                        continue

                    try:
                        s_time = datetime.strptime(s_str, "%H:%M").time()
                        e_time = datetime.strptime(e_str, "%H:%M").time()
                    except ValueError:
                        flash(f"Hora inválida en el día {prefix.upper()} (formato HH:MM).", "error")
                        return redirect(url_for("editar_horario", schedule_id=horario.id))

                    b_type = request.form.get(f"{prefix}_break_type", "none")
                    bs = be = None
                    bmin = None
                    b_optional = bool(request.form.get(f"{prefix}_break_optional"))
                    b_paid = bool(request.form.get(f"{prefix}_break_paid"))
                    b_unpaid = bool(request.form.get(f"{prefix}_break_unpaid"))

                    if b_type == "fixed":
                        bs_str = request.form.get(f"{prefix}_break_start", "").strip()
                        be_str = request.form.get(f"{prefix}_break_end", "").strip()
                        if not bs_str or not be_str:
                            flash("Para descanso fijo debes indicar inicio y fin de descanso en cada día.", "error")
                            return redirect(url_for("editar_horario", schedule_id=horario.id))
                        try:
                            bs = datetime.strptime(bs_str, "%H:%M").time()
                            be = datetime.strptime(be_str, "%H:%M").time()
                        except ValueError:
                            flash("Las horas de descanso diario deben tener formato HH:MM.", "error")
                            return redirect(url_for("editar_horario", schedule_id=horario.id))
                    elif b_type == "flexible":
                        bmin_str = request.form.get(f"{prefix}_break_minutes", "").strip()
                        if not bmin_str:
                            flash("Para descanso flexible debes indicar los minutos de descanso en cada día.", "error")
                            return redirect(url_for("editar_horario", schedule_id=horario.id))
                        try:
                            bmin = int(bmin_str)
                        except ValueError:
                            flash("Los minutos de descanso diario deben ser numéricos.", "error")
                            return redirect(url_for("editar_horario", schedule_id=horario.id))

                    if b_type == "none":
                        b_optional = False
                        b_paid = False
                    else:
                        if b_paid and b_unpaid:
                            b_paid = True
                        elif not b_paid and not b_unpaid:
                            b_paid = False

                    dia_obj = ScheduleDay(
                        schedule_id=horario.id,
                        day_of_week=dow,
                        start_time=s_time,
                        end_time=e_time,
                        break_type=b_type,
                        break_start=bs,
                        break_end=be,
                        break_minutes=bmin,
                        break_optional=b_optional,
                        break_paid=b_paid,
                    )
                    horario.days.append(dia_obj)
                    tiene_algun_dia = True

                if not tiene_algun_dia:
                    flash("En modo por días, al menos un día debe tener horario.", "error")
                    return redirect(url_for("editar_horario", schedule_id=horario.id))

            db.session.commit()
            flash("Horario actualizado correctamente.", "success")
            return redirect(url_for("admin_horarios"))

        dias_map = {d.day_of_week: d for d in horario.days}
        return render_template("admin_horario_editar.html", horario=horario, dias_map=dias_map)
