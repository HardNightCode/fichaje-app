from datetime import datetime, timedelta, date, time
from collections import OrderedDict

from flask import render_template, redirect, url_for, request
from flask_login import current_user, login_required

from ..config import local_to_utc_naive
from ..logic import (
    agrupar_registros_en_intervalos,
    calcular_descanso_intervalo_para_usuario,
    calcular_extra_y_defecto_intervalo,
    formatear_timedelta,
    obtener_horario_aplicable,
    obtener_ubicaciones_usuario,
    usuario_tiene_flexible,
)
from ..models import Registro, RegistroJustificacion, User


def _fin_con_margen(usuario, fecha_local):
    schedule = obtener_horario_aplicable(usuario, fecha_local)
    if not schedule:
        return None

    settings = getattr(usuario, "schedule_settings", None)
    margin = settings.margin_minutes if settings and settings.margin_minutes is not None else 0

    if schedule.use_per_day:
        dia = next((d for d in schedule.days if d.day_of_week == fecha_local.weekday()), None)
        if not dia:
            return None
        start_t = dia.start_time
        end_t = dia.end_time
    else:
        start_t = schedule.start_time
        end_t = schedule.end_time

    if not end_t:
        return None
    if not start_t:
        start_t = time.min

    inicio_dt = datetime.combine(fecha_local, start_t)
    fin_dt = datetime.combine(fecha_local, end_t)
    if fin_dt <= inicio_dt:
        fin_dt += timedelta(days=1)

    return fin_dt + timedelta(minutes=margin)


def register_dashboard_routes(app):
    @app.route("/")
    @login_required
    def index():
        # Si es cuenta de kiosko, no mostramos el dashboard normal
        if current_user.role == "kiosko":
            return redirect(url_for("kiosko_panel"))

        if current_user.role == "admin":
            hoy = datetime.now().date()
            inicio_local = datetime.combine(hoy, time.min)
            fin_local = datetime.combine(hoy, time.max)
            inicio_utc = local_to_utc_naive(inicio_local)
            fin_utc = local_to_utc_naive(fin_local)

            total_usuarios = User.query.count()
            total_empleados = User.query.filter_by(role="empleado").count()
            total_kioskos = User.query.filter_by(role="kiosko").count()
            total_admins = User.query.filter(User.role.in_(["admin", "kiosko_admin"])).count()

            registros_hoy = Registro.query.filter(
                Registro.momento >= inicio_utc,
                Registro.momento <= fin_utc,
            ).count()
            entradas_hoy = Registro.query.filter(
                Registro.accion == "entrada",
                Registro.momento >= inicio_utc,
                Registro.momento <= fin_utc,
            ).count()
            salidas_hoy = Registro.query.filter(
                Registro.accion == "salida",
                Registro.momento >= inicio_utc,
                Registro.momento <= fin_utc,
            ).count()

            justificaciones_hoy = (
                RegistroJustificacion.query
                .join(Registro, Registro.id == RegistroJustificacion.registro_id)
                .filter(
                    Registro.momento >= inicio_utc,
                    Registro.momento <= fin_utc,
                )
                .count()
            )

            return render_template(
                "admin_dashboard.html",
                total_usuarios=total_usuarios,
                total_empleados=total_empleados,
                total_kioskos=total_kioskos,
                total_admins=total_admins,
                registros_hoy=registros_hoy,
                entradas_hoy=entradas_hoy,
                salidas_hoy=salidas_hoy,
                justificaciones_hoy=justificaciones_hoy,
            )

        registros_usuario = (
            Registro.query.filter_by(usuario_id=current_user.id)
            .order_by(Registro.momento.asc())
            .all()
        )

        intervalos_usuario = agrupar_registros_en_intervalos(registros_usuario)

        for it in intervalos_usuario:
            if it.usuario and it.entrada_momento:
                ahora_ref = datetime.utcnow()
                descanso_td, en_curso, inicio = calcular_descanso_intervalo_para_usuario(
                    it.usuario.id,
                    it.entrada_momento,
                    it.salida_momento,
                    ahora=ahora_ref,
                )

                base_segundos = 0
                if en_curso and inicio:
                    abierto = max(ahora_ref - inicio, timedelta(0))
                    base_td = descanso_td - abierto
                    if base_td.total_seconds() < 0:
                        base_td = timedelta(0)
                    base_segundos = int(base_td.total_seconds())
            else:
                descanso_td, en_curso, inicio = timedelta(0), False, None
                base_segundos = 0

            it.descanso_td = descanso_td
            it.descanso_en_curso = en_curso
            it.descanso_inicio_iso = inicio.isoformat() if inicio else ""
            it.descanso_base_segundos = base_segundos

            if en_curso:
                it.descanso_label = "Descansando"
            elif descanso_td.total_seconds() > 0:
                it.descanso_label = formatear_timedelta(descanso_td)
            else:
                it.descanso_label = "Sin descanso"

        hoy = datetime.now().date()
        total_trabajo_hoy = timedelta(0)
        total_trabajo_semana = timedelta(0)
        for it in intervalos_usuario:
            extra_td, defecto_td = calcular_extra_y_defecto_intervalo(it)
            it.horas_extra = extra_td
            it.horas_defecto = defecto_td
            trabajo_real = getattr(it, "trabajo_real", timedelta(0))

            if trabajo_real.total_seconds() < 0:
                trabajo_real = timedelta(0)

            fecha_it = None
            if it.entrada_momento:
                fecha_it = it.entrada_momento.date()
            elif it.salida_momento:
                fecha_it = it.salida_momento.date()

            if fecha_it == hoy:
                total_trabajo_hoy += trabajo_real

        # Agrupar intervalos por semana ISO (año, semana)
        week_map = OrderedDict()
        for it in intervalos_usuario:
            ref = it.entrada_momento or it.salida_momento
            if not ref:
                continue
            iso = ref.isocalendar()
            key = (iso.year, iso.week)
            if key not in week_map:
                week_map[key] = []
            week_map[key].append(it)

        week_keys = sorted(week_map.keys(), reverse=True)
        week_page = request.args.get("week_page", "1")
        try:
            week_page_int = max(1, int(week_page))
        except ValueError:
            week_page_int = 1

        total_pages = len(week_keys) if week_keys else 1
        if week_page_int > total_pages:
            week_page_int = total_pages

        selected_key = week_keys[week_page_int - 1] if week_keys else None
        intervalos_semana = week_map.get(selected_key, []) if selected_key else []

        # Calcular total trabajado en la semana seleccionada
        for it in intervalos_semana:
            trabajo_real = getattr(it, "trabajo_real", timedelta(0)) or timedelta(0)
            if trabajo_real.total_seconds() > 0:
                total_trabajo_semana += trabajo_real

        # Etiquetas de semanas
        semanas_meta = []
        for idx, key in enumerate(week_keys, start=1):
            year, wk = key
            # Fecha de lunes de esa semana ISO
            lunes = date.fromisocalendar(year, wk, 1)
            domingo = lunes + timedelta(days=6)
            label = f"Semana {wk} ({lunes.strftime('%d/%m')} - {domingo.strftime('%d/%m')})"
            semanas_meta.append({"page": idx, "label": label})

        semana_actual_label = None
        if selected_key:
            year, wk = selected_key
            lunes = date.fromisocalendar(year, wk, 1)
            domingo = lunes + timedelta(days=6)
            semana_actual_label = f"Semana {wk} ({lunes.strftime('%d/%m')} - {domingo.strftime('%d/%m')})"

        resumen_horas_hoy = formatear_timedelta(total_trabajo_hoy) if total_trabajo_hoy.total_seconds() > 0 else None
        resumen_horas_semana = formatear_timedelta(total_trabajo_semana) if intervalos_semana else formatear_timedelta(total_trabajo_hoy)

        ubicaciones_usuario = obtener_ubicaciones_usuario(current_user)
        tiene_ubicaciones = len(ubicaciones_usuario) > 0
        tiene_flexible = usuario_tiene_flexible(current_user)

        ultimo_trabajo = (
            Registro.query.filter(
                Registro.usuario_id == current_user.id,
                Registro.accion.in_(["entrada", "salida"]),
            )
            .order_by(Registro.momento.desc())
            .first()
        )

        if ultimo_trabajo is None:
            bloquear_entrada = False
            bloquear_salida = True
        else:
            if ultimo_trabajo.accion == "entrada":
                bloquear_entrada = True
                bloquear_salida = False
            else:
                bloquear_entrada = False
                bloquear_salida = True

        hoy = datetime.now().date()
        schedule = obtener_horario_aplicable(current_user, hoy)
        tiene_descanso = False
        descanso_es_flexible = False

        if schedule:
            if schedule.use_per_day:
                dow = hoy.weekday()
                dia = next((d for d in schedule.days if d.day_of_week == dow), None)
                if dia:
                    if dia.break_type in ("fixed", "flexible"):
                        tiene_descanso = True
                    if dia.break_type == "flexible" or (dia.break_type == "fixed" and getattr(dia, "break_optional", False)):
                        descanso_es_flexible = True
            else:
                if schedule.break_type in ("fixed", "flexible"):
                    tiene_descanso = True
                if schedule.break_type == "flexible" or (schedule.break_type == "fixed" and getattr(schedule, "break_optional", False)):
                    descanso_es_flexible = True

        ultimo_entrada = (
            Registro.query.filter(
                Registro.usuario_id == current_user.id,
                Registro.accion == "entrada",
            )
            .order_by(Registro.momento.desc())
            .first()
        )
        ultimo_salida = (
            Registro.query.filter(
                Registro.usuario_id == current_user.id,
                Registro.accion == "salida",
            )
            .order_by(Registro.momento.desc())
            .first()
        )

        entrada_abierta = False
        if ultimo_entrada:
            if not ultimo_salida or ultimo_entrada.momento > ultimo_salida.momento:
                entrada_abierta = True

        ultimo_descanso_inicio = (
            Registro.query.filter(
                Registro.usuario_id == current_user.id,
                Registro.accion == "descanso_inicio",
            )
            .order_by(Registro.momento.desc())
            .first()
        )
        ultimo_descanso_fin = (
            Registro.query.filter(
                Registro.usuario_id == current_user.id,
                Registro.accion == "descanso_fin",
            )
            .order_by(Registro.momento.desc())
            .first()
        )

        descanso_en_curso = False
        if ultimo_descanso_inicio and entrada_abierta:
            if (not ultimo_descanso_fin) or (
                ultimo_descanso_inicio.momento > ultimo_descanso_fin.momento
            ):
                if (not ultimo_entrada) or (
                    ultimo_descanso_inicio.momento >= ultimo_entrada.momento
                ):
                    descanso_en_curso = True

        if (not entrada_abierta) or (not descanso_es_flexible):
            bloquear_descanso = True
        else:
            bloquear_descanso = False

        fin_con_margen_local = _fin_con_margen(current_user, hoy)
        fin_margen_iso = fin_con_margen_local.isoformat() if fin_con_margen_local else ""

        return render_template(
            "index.html",
            intervalos_usuario=intervalos_semana,
            resumen_horas_hoy=resumen_horas_hoy,
            resumen_horas_semana=resumen_horas_semana,
            ubicaciones_usuario=ubicaciones_usuario,
            tiene_ubicaciones=tiene_ubicaciones,
            tiene_flexible=tiene_flexible,
            bloquear_entrada=bloquear_entrada,
            bloquear_salida=bloquear_salida,
            tiene_descanso=tiene_descanso,
            descanso_es_flexible=descanso_es_flexible,
            descanso_en_curso=descanso_en_curso,
            bloquear_descanso=bloquear_descanso,
            semana_actual_label=semana_actual_label,
            week_page=week_page_int,
            total_pages=total_pages,
            semanas_meta=semanas_meta,
            fin_margen_iso=fin_margen_iso,
        )
