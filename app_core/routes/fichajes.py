from datetime import datetime, timedelta

from flask import flash, redirect, request, session, url_for
from flask_login import current_user, login_required
from werkzeug.security import check_password_hash

from ..extensions import db
from ..logic import (
    agrupar_registros_en_intervalos,
    calcular_extra_y_defecto_intervalo,
    calcular_jornada_teorica,
    obtener_horario_aplicable,
    obtener_ubicaciones_usuario,
    usuario_tiene_flexible,
    usuario_tiene_intervalo_abierto,
    validar_secuencia_fichaje,
)
from ..models import Kiosk, KioskUser, Registro, RegistroJustificacion, User
from ..config import to_local, local_to_utc_naive
from geo_utils import is_within_radius


def register_fichaje_routes(app):
    @app.route("/fichar", methods=["POST"])
    @login_required
    def fichar():
        """
        Fichaje normal y modo KIOSKO.
        """
        accion = request.form.get("accion")
        if accion not in ("entrada", "salida", "descanso_inicio", "descanso_fin"):
            flash("Acción no válida", "error")
            redirect_home = "kiosko_panel" if current_user.role == "kiosko" else "index"
            return redirect(url_for(redirect_home))

        redirect_home = "kiosko_panel" if current_user.role == "kiosko" else "index"

        usuario_objetivo = current_user
        kiosk = None
        ku = None

        if current_user.role == "kiosko":
            usuario_id_str = request.form.get("usuario_id", "").strip()
            pin = request.form.get("pin", "").strip()

            if not usuario_id_str or not pin:
                flash("Debes seleccionar un usuario y proporcionar el PIN.", "error")
                return redirect(url_for(redirect_home))

            try:
                usuario_id = int(usuario_id_str)
            except ValueError:
                flash("Usuario seleccionado no válido.", "error")
                return redirect(url_for(redirect_home))

            usuario_objetivo = User.query.get(usuario_id)
            if not usuario_objetivo:
                flash("Usuario no encontrado.", "error")
                return redirect(url_for(redirect_home))

            kiosk = (
                Kiosk.query
                .filter_by(kiosk_account_id=current_user.id)
                .first()
            )
            if not kiosk:
                flash("Esta cuenta de kiosko no está asociada a ningún kiosko.", "error")
                return redirect(url_for(redirect_home))

            ku = (
                KioskUser.query
                .filter_by(kiosk_id=kiosk.id, user_id=usuario_objetivo.id)
                .first()
            )
            if not ku:
                flash("El usuario no está autorizado para fichar en este kiosko.", "error")
                return redirect(url_for(redirect_home))

            if not check_password_hash(ku.pin_hash, pin):
                flash("PIN incorrecto.", "error")
                return redirect(url_for(redirect_home))

        ubicaciones_usuario = obtener_ubicaciones_usuario(usuario_objetivo)

        if not ubicaciones_usuario:
            flash(
                "No tienes una ubicación asignada. Contacta con el administrador.",
                "error",
            )
            return redirect(url_for(redirect_home))

        flexible_activo = usuario_tiene_flexible(usuario_objetivo)

        if accion in ("descanso_inicio", "descanso_fin"):
            hoy = datetime.now().date()
            schedule = obtener_horario_aplicable(usuario_objetivo, hoy)

            descanso_fijo_hoy = False
            if schedule:
                if schedule.use_per_day:
                    dow = hoy.weekday()
                    dia = next((d for d in schedule.days if d.day_of_week == dow), None)
                    if dia and dia.break_type == "fixed":
                        descanso_fijo_hoy = True
                else:
                    if schedule.break_type == "fixed":
                        descanso_fijo_hoy = True

            if descanso_fijo_hoy:
                flash(
                    "Tu horario tiene un descanso fijo configurado. No puedes registrar descansos manuales.",
                    "error",
                )
                return redirect(url_for(redirect_home))

        ultimo_registro = (
            Registro.query.filter_by(usuario_id=usuario_objetivo.id)
            .order_by(Registro.momento.desc())
            .first()
        )

        if accion in ("entrada", "salida"):
            es_valido, msg_error = validar_secuencia_fichaje(accion, ultimo_registro)
            if not es_valido:
                flash(msg_error, "error")
                return redirect(url_for(redirect_home))
        else:
            if not usuario_tiene_intervalo_abierto(usuario_objetivo.id):
                flash("No puedes registrar un descanso si no has fichado la entrada.", "error")
                return redirect(url_for(redirect_home))

            if accion == "descanso_inicio":
                ultimo_inicio = (
                    Registro.query
                    .filter_by(usuario_id=usuario_objetivo.id, accion="descanso_inicio")
                    .order_by(Registro.momento.desc())
                    .first()
                )

                if ultimo_inicio:
                    fin_posterior = (
                        Registro.query
                        .filter(
                            Registro.usuario_id == usuario_objetivo.id,
                            Registro.accion == "descanso_fin",
                            Registro.momento > ultimo_inicio.momento,
                        )
                        .first()
                    )

                    salida_posterior = (
                        Registro.query
                        .filter(
                            Registro.usuario_id == usuario_objetivo.id,
                            Registro.accion == "salida",
                            Registro.momento > ultimo_inicio.momento,
                        )
                        .first()
                    )

                    if not fin_posterior and not salida_posterior:
                        flash("Ya tienes un descanso en curso.", "error")
                        return redirect(url_for(redirect_home))

            elif accion == "descanso_fin":
                ultimo_inicio = (
                    Registro.query
                    .filter_by(usuario_id=usuario_objetivo.id, accion="descanso_inicio")
                    .order_by(Registro.momento.desc())
                    .first()
                )
                if not ultimo_inicio:
                    flash("No hay ningún descanso en curso que terminar.", "error")
                    return redirect(url_for(redirect_home))

                fin_posterior = (
                    Registro.query
                    .filter(
                        Registro.usuario_id == usuario_objetivo.id,
                        Registro.accion == "descanso_fin",
                        Registro.momento > ultimo_inicio.momento,
                    )
                    .first()
                )

                salida_posterior = (
                    Registro.query
                    .filter(
                        Registro.usuario_id == usuario_objetivo.id,
                        Registro.accion == "salida",
                        Registro.momento > ultimo_inicio.momento,
                    )
                    .first()
                )

                if fin_posterior or salida_posterior:
                    flash("No hay ningún descanso en curso que terminar.", "error")
                    return redirect(url_for(redirect_home))

        settings = getattr(usuario_objetivo, "schedule_settings", None)
        if settings and settings.enforce_schedule:
            user_schedules = list(usuario_objetivo.schedules)

            if not user_schedules:
                flash(
                    "No tienes ningún horario asignado. Contacta con el administrador.",
                    "error",
                )
                return redirect(url_for(redirect_home))

            margin = settings.margin_minutes or 0
            ahora = datetime.now()
            hoy = ahora.date()
            dow = hoy.weekday()

            autorizado_por_horario = False

            for sched in user_schedules:
                if sched.use_per_day:
                    dia = next((d for d in sched.days if d.day_of_week == dow), None)
                    if not dia:
                        continue
                    inicio_t = dia.start_time
                    fin_t = dia.end_time
                else:
                    inicio_t = sched.start_time
                    fin_t = sched.end_time

                if not inicio_t or not fin_t:
                    continue

                inicio_dt = datetime.combine(hoy, inicio_t)
                fin_dt = datetime.combine(hoy, fin_t)

                if fin_dt <= inicio_dt:
                    fin_dt += timedelta(days=1)

                inicio_con_margen = inicio_dt - timedelta(minutes=margin)
                fin_con_margen = fin_dt + timedelta(minutes=margin)

                if inicio_con_margen <= ahora <= fin_con_margen:
                    autorizado_por_horario = True
                    break

            if not autorizado_por_horario:
                flash(
                    "No estás dentro de tu horario autorizado para fichar "
                    f"(se tiene en cuenta un margen de {margin} minutos).",
                    "error",
                )
                return redirect(url_for(redirect_home))

        lat_str = request.form.get("lat")
        lon_str = request.form.get("lon")

        if not lat_str or not lon_str:
            flash(
                "No se recibió la ubicación del dispositivo. Comprueba los permisos de geolocalización.",
                "error",
            )
            return redirect(url_for(redirect_home))

        try:
            lat_user = float(lat_str)
            lon_user = float(lon_str)
        except ValueError:
            flash("Coordenadas de ubicación inválidas.", "error")
            return redirect(url_for(redirect_home))

        if not flexible_activo:
            autorizado = False

            for loc in ubicaciones_usuario:
                if (loc.name or "").lower() == "flexible":
                    continue

                if is_within_radius(
                    lat_user,
                    lon_user,
                    loc.latitude,
                    loc.longitude,
                    loc.radius_meters,
                ):
                    autorizado = True
                    break

            if not autorizado:
                flash(
                    "No estás dentro de ninguna de tus ubicaciones autorizadas. No se registra el fichaje.",
                    "error",
                )
                return redirect(url_for(redirect_home))

        registro = Registro(
            usuario_id=usuario_objetivo.id,
            accion=accion,
            momento=datetime.utcnow(),
            latitude=lat_user,
            longitude=lon_user,
        )
        db.session.add(registro)
        db.session.flush()

        # Si es una SALIDA, comprobamos horas extra y pedimos justificación
        if accion == "salida":
            motivo_extra = (request.form.get("motivo_extra") or "").strip()
            detalle_extra = (request.form.get("detalle_extra") or "").strip()

            # Registros del día (en horario local) para calcular trabajado/esperado
            fecha_local = to_local(registro.momento).date()
            start_local = datetime.combine(fecha_local, datetime.min.time())
            end_local = datetime.combine(fecha_local, datetime.max.time())
            start_utc = local_to_utc_naive(start_local)
            end_utc = local_to_utc_naive(end_local)

            regs_dia = (
                Registro.query
                .filter(
                    Registro.usuario_id == usuario_objetivo.id,
                    Registro.momento >= start_utc,
                    Registro.momento <= end_utc,
                )
                .order_by(Registro.momento.asc())
                .all()
            )

            intervalos_dia = agrupar_registros_en_intervalos(regs_dia)
            trabajos_fecha = {}
            for it in intervalos_dia:
                extra_td, defecto_td = calcular_extra_y_defecto_intervalo(it)
                it.horas_extra = extra_td
                it.horas_defecto = defecto_td
                trabajo_real = getattr(it, "trabajo_real", None) or timedelta(0)
                fecha_it = it.entrada_momento.date() if it.entrada_momento else (
                    it.salida_momento.date() if it.salida_momento else fecha_local
                )
                trabajos_fecha[fecha_it] = trabajos_fecha.get(fecha_it, timedelta()) + trabajo_real

            # Esperado diario
            schedule = obtener_horario_aplicable(usuario_objetivo, fecha_local)
            esperado_dia = calcular_jornada_teorica(schedule, fecha_local) if schedule else timedelta(0)
            trabajado_dia = trabajos_fecha.get(fecha_local, timedelta())

            if trabajado_dia > esperado_dia:
                if not motivo_extra:
                    db.session.rollback()
                    flash("Has superado tu jornada prevista hoy. Indica un motivo para registrar la salida.", "error")
                    return redirect(url_for(redirect_home))
                just = RegistroJustificacion(
                    registro_id=registro.id,
                    motivo=motivo_extra,
                    detalle=detalle_extra if motivo_extra.lower() == "otro" else detalle_extra or "",
                )
                db.session.add(just)

        db.session.commit()

        if current_user.role == "kiosko":
            if ku and not ku.close_session_after_punch:
                session["kiosk_last_user_id"] = usuario_objetivo.id
            else:
                session.pop("kiosk_last_user_id", None)

            flash("Fichaje registrado correctamente", "success")
            return redirect(url_for("kiosko_panel"))

        flash("Fichaje registrado correctamente", "success")
        return redirect(url_for("index"))
