from datetime import datetime, timedelta
from types import SimpleNamespace

from flask import flash, redirect, render_template, request, url_for, session
from flask_login import current_user, login_required
from flask_weasyprint import HTML, render_pdf

from ..auth import admin_required
from ..config import TZ_LOCAL
from ..extensions import db
from ..logic import (
    agrupar_registros_en_intervalos,
    calcular_descanso_intervalo_para_usuario,
    calcular_descanso_intervalos,
    calcular_duracion_trabajada_intervalo,
    calcular_extra_y_defecto_intervalo,
    calcular_horas_trabajadas,
    determinar_ubicacion_por_coordenadas,
    formatear_timedelta,
    local_to_utc_naive,
    obtener_horario_aplicable,
    obtener_trabajo_y_esperado_por_periodo,
)
from ..models import (
    Kiosk,
    Location,
    Registro,
    RegistroEdicion,
    User,
)
from ..reporting import generar_csv, generar_pdf


def register_admin_registro_routes(app):
    @app.route("/admin/generar_informe", methods=["POST"])
    @login_required
    def generar_informe():
        usuario_id = request.form.get("usuario_id")
        fecha_desde = request.form.get("fecha_desde")
        fecha_hasta = request.form.get("fecha_hasta")

        try:
            fecha_desde_dt = datetime.strptime(fecha_desde, "%Y-%m-%d")
            fecha_hasta_dt = datetime.strptime(fecha_hasta, "%Y-%m-%d") + timedelta(days=1)
        except ValueError:
            flash("Las fechas no son válidas.", "error")
            return redirect(url_for("admin_registros"))

        query = Registro.query.filter(Registro.momento >= fecha_desde_dt, Registro.momento <= fecha_hasta_dt)

        if usuario_id != "all":
            query = query.filter(Registro.usuario_id == int(usuario_id))

        registros = query.all()

        if not registros:
            flash("No se encontraron registros para este periodo y usuario.", "error")
            return redirect(url_for("admin_registros"))

        resumen_horas = calcular_horas_trabajadas(registros)

        try:
            html = render_template("informe_pdf.html", registros=registros, resumen_horas=resumen_horas, tipo_periodo="rango")
            return render_pdf(HTML(string=html))
        except Exception as e:
            app.logger.error(f"Error al generar el PDF: {e}")
            flash("Hubo un problema generando el informe PDF.", "error")
            return redirect(url_for("admin_registros"))

    @app.route("/admin/registros", methods=["GET", "POST"])
    @admin_required
    def admin_registros():
        usuarios = User.query.order_by(User.username).all()
        ubicaciones_definidas = (
            Location.query.filter(Location.name != "Flexible")
            .order_by(Location.name)
            .all()
        )

        # Valores iniciales
        usuario_seleccionado = "all"
        tipo_periodo = "rango"
        fecha_desde = ""
        fecha_hasta = ""
        fecha_semana = ""
        mes = None
        registros = []
        intervalos = []
        ubicacion_filtro = "all"
        modo_conteo = "semanal"
        accion = "filtrar"

        # Recuperar filtros guardados en sesión si GET y existen
        filtros_guardados = session.get("admin_registros_filtros", {})
        restored_from_session = False

        if request.method == "POST":
            usuario_seleccionado = request.form.get("usuario_id", "all")
            tipo_periodo = request.form.get("tipo_periodo", "rango")
            fecha_desde = request.form.get("fecha_desde", "")
            fecha_hasta = request.form.get("fecha_hasta", "")
            fecha_semana = request.form.get("fecha_semana", "")
            mes_str = request.form.get("mes", "")
            accion = request.form.get("accion", "filtrar")
            ubicacion_filtro = request.form.get("ubicacion_filtro", "all")
            modo_conteo = request.form.get("modo_conteo", "semanal")

            session["admin_registros_filtros"] = {
                "usuario_id": usuario_seleccionado,
                "tipo_periodo": tipo_periodo,
                "fecha_desde": fecha_desde,
                "fecha_hasta": fecha_hasta,
                "fecha_semana": fecha_semana,
                "mes": mes_str,
                "ubicacion_filtro": ubicacion_filtro,
                "modo_conteo": modo_conteo,
            }
        elif filtros_guardados:
            usuario_seleccionado = filtros_guardados.get("usuario_id", "all")
            tipo_periodo = filtros_guardados.get("tipo_periodo", "rango")
            fecha_desde = filtros_guardados.get("fecha_desde", "")
            fecha_hasta = filtros_guardados.get("fecha_hasta", "")
            fecha_semana = filtros_guardados.get("fecha_semana", "")
            mes_str = filtros_guardados.get("mes", "")
            ubicacion_filtro = filtros_guardados.get("ubicacion_filtro", "all")
            modo_conteo = filtros_guardados.get("modo_conteo", "semanal")
            accion = "filtrar"
            restored_from_session = True
        else:
            mes_str = ""

        mes = int(mes_str) if mes_str else None

        # Aplicar filtros cuando procede
        if request.method == "POST" or restored_from_session:
            query = Registro.query.join(User).order_by(Registro.momento.desc())

            if usuario_seleccionado != "all":
                try:
                    uid = int(usuario_seleccionado)
                    query = query.filter(Registro.usuario_id == uid)
                except ValueError:
                    flash("Usuario no válido.", "error")

            if tipo_periodo == "rango":
                if fecha_desde:
                    try:
                        dt_desde_local = datetime.strptime(fecha_desde, "%Y-%m-%d")
                        dt_desde_local = dt_desde_local.replace(
                            hour=0, minute=0, second=0, microsecond=0
                        )
                        dt_desde_utc = local_to_utc_naive(dt_desde_local)
                        query = query.filter(Registro.momento >= dt_desde_utc)
                    except ValueError:
                        flash("Fecha 'desde' no válida.", "error")

                if fecha_hasta:
                    try:
                        dt_hasta_local = datetime.strptime(fecha_hasta, "%Y-%m-%d")
                        dt_hasta_local = dt_hasta_local.replace(
                            hour=23,
                            minute=59,
                            second=59,
                            microsecond=999999,
                        )
                        dt_hasta_utc = local_to_utc_naive(dt_hasta_local)
                        query = query.filter(Registro.momento <= dt_hasta_utc)
                    except ValueError:
                        flash("Fecha 'hasta' no válida.", "error")

            elif tipo_periodo == "semanal":
                if fecha_semana:
                    try:
                        start_of_week_local = datetime.strptime(fecha_semana, "%Y-%m-%d")
                        start_of_week_local = start_of_week_local.replace(
                            hour=0, minute=0, second=0, microsecond=0
                        )
                        end_of_week_local = start_of_week_local + timedelta(
                            days=6, hours=23, minutes=59, seconds=59
                        )

                        start_of_week_utc = local_to_utc_naive(start_of_week_local)
                        end_of_week_utc = local_to_utc_naive(end_of_week_local)

                        query = query.filter(
                            Registro.momento >= start_of_week_utc,
                            Registro.momento <= end_of_week_utc,
                        )
                    except ValueError:
                        flash("Fecha de semana no válida.", "error")

            elif tipo_periodo == "mensual":
                if mes:
                    try:
                        hoy_local = datetime.now(TZ_LOCAL)
                        year = hoy_local.year

                        start_of_month_local = datetime(year, mes, 1, 0, 0, 0)
                        if mes == 12:
                            next_month_local = datetime(year + 1, 1, 1, 0, 0, 0)
                        else:
                            next_month_local = datetime(year, mes + 1, 1, 0, 0, 0)

                        start_of_month_utc = local_to_utc_naive(start_of_month_local)
                        next_month_utc = local_to_utc_naive(next_month_local)
                        end_of_month_utc = next_month_utc - timedelta(seconds=1)

                        query = query.filter(
                            Registro.momento >= start_of_month_utc,
                            Registro.momento <= end_of_month_utc,
                        )
                    except ValueError:
                        flash("Mes no válido.", "error")

            elif tipo_periodo == "historico":
                pass

            registros = query.all()

            if ubicacion_filtro != "all":
                registros_filtrados = []
                if ubicacion_filtro == "flexible":
                    for r in registros:
                        loc_match = determinar_ubicacion_por_coordenadas(
                            r.latitude,
                            r.longitude,
                            ubicaciones_definidas,
                        )
                        if loc_match is None:
                            registros_filtrados.append(r)
                else:
                    try:
                        loc_id = int(ubicacion_filtro)
                        loc_sel = Location.query.get(loc_id)
                    except ValueError:
                        loc_sel = None

                    if loc_sel:
                        for r in registros:
                            if r.latitude is None or r.longitude is None:
                                continue
                            if determinar_ubicacion_por_coordenadas(
                                r.latitude,
                                r.longitude,
                                [loc_sel],
                                margen_extra_m=0.0,
                            ):
                                registros_filtrados.append(r)

                registros = registros_filtrados

            intervalos = agrupar_registros_en_intervalos(registros)

            for it in intervalos:
                extra_td, defecto_td = calcular_extra_y_defecto_intervalo(it)
                it.horas_extra = extra_td
                it.horas_defecto = defecto_td

            calcular_descanso_intervalos(intervalos, registros)
            
            if request.method == "POST" and accion == "csv":
                return generar_csv(intervalos, modo_conteo)
            if request.method == "POST" and accion == "pdf":
                return generar_pdf(intervalos, tipo_periodo, modo_conteo)

        # Mapa usuario -> fecha -> trabajo real
        trabajos_por_usuario_fecha = {}
        intervalos_por_usuario_fecha = defaultdict(lambda: defaultdict(list))
        esperado_por_usuario_fecha = defaultdict(lambda: defaultdict(timedelta))

        for it in intervalos:
            if not it.usuario:
                continue

            trabajo_real = getattr(it, "trabajo_real", None)
            if trabajo_real is None:
                extra_td, defecto_td = calcular_extra_y_defecto_intervalo(it)
                it.horas_extra = extra_td
                it.horas_defecto = defecto_td
                trabajo_real = getattr(it, "trabajo_real", timedelta(0))

            if trabajo_real.total_seconds() <= 0:
                dur = calcular_duracion_trabajada_intervalo(it) or timedelta(0)
                descanso_simple = getattr(it, "descanso_total", None)
                if descanso_simple is None:
                    descanso_simple = timedelta(0)
                trabajo_estimado = dur - descanso_simple
                if trabajo_estimado.total_seconds() > 0:
                trabajo_real = trabajo_estimado
                it.trabajo_real = trabajo_real

            if trabajo_real.total_seconds() <= 0:
                continue

            usuario = it.usuario
            username = usuario.username
            trabajos_por_usuario_fecha.setdefault(username, {})
            fecha_base = it.entrada_momento.date() if it.entrada_momento else (
                it.salida_momento.date() if it.salida_momento else None
            )

            if fecha_base:
                intervalos_por_usuario_fecha[usuario.id][fecha_base].append(it)
                schedule = obtener_horario_aplicable(usuario, fecha_base)
                esperado_td = calcular_jornada_teorica(schedule, fecha_base) if schedule else timedelta(0)
                esperado_por_usuario_fecha[usuario.id][fecha_base] += esperado_td
                trabajos_por_usuario_fecha[username][fecha_base] = trabajos_por_usuario_fecha[username].get(fecha_base, timedelta()) + trabajo_real

        # Asignar extra/defecto agregados por día al primer intervalo de cada fecha
        for uid, fechas in intervalos_por_usuario_fecha.items():
            for fecha_base, lista in fechas.items():
                lista_ordenada = sorted(
                    lista,
                    key=lambda x: x.entrada_momento or x.salida_momento or datetime.min,
                )
                trabajado = sum((getattr(it, "trabajo_real", timedelta(0)) or timedelta(0) for it in lista_ordenada), timedelta(0))
                esperado = esperado_por_usuario_fecha[uid][fecha_base]
                diff = trabajado - esperado
                extra_d = diff if diff.total_seconds() > 0 else timedelta(0)
                defecto_d = -diff if diff.total_seconds() < 0 else timedelta(0)

                for idx, it in enumerate(lista_ordenada):
                    if idx == 0:
                        it.horas_extra = extra_d
                        it.horas_defecto = defecto_d
                    else:
                        it.horas_extra = None
                        it.horas_defecto = None

        horas_por_usuario = {}
        for username, trabajos_fecha in trabajos_por_usuario_fecha.items():
            user_obj = next((u for u in usuarios if u.username == username), None)
            if user_obj is None:
                continue
            total_trab, total_esp, extra_td, defecto_td = obtener_trabajo_y_esperado_por_periodo(
                user_obj, trabajos_fecha, modo_conteo
            )
            horas_por_usuario[username] = SimpleNamespace(
                trabajado=formatear_timedelta(total_trab),
                esperado=formatear_timedelta(total_esp),
                extra=formatear_timedelta(extra_td),
                defecto=formatear_timedelta(defecto_td),
            )

        return render_template(
            "admin_registros.html",
            usuarios=usuarios,
            intervalos=intervalos,
            usuario_seleccionado=usuario_seleccionado,
            fecha_desde=fecha_desde,
            fecha_hasta=fecha_hasta,
            fecha_semana=fecha_semana,
            tipo_periodo=tipo_periodo,
            horas_por_usuario=horas_por_usuario,
            mes=mes,
            ubicaciones_definidas=ubicaciones_definidas,
            ubicacion_filtro=ubicacion_filtro,
            modo_conteo=modo_conteo,
            formatear_timedelta=formatear_timedelta,
        )

    @app.route("/admin/registros/<int:registro_id>/editar", methods=["GET", "POST"])
    @admin_required
    def editar_registro(registro_id):
        """
        Editor de intervalo (entrada + salida) a partir de un id de registro.
        """
        usuarios = User.query.order_by(User.username).all()

        if request.method == "POST":
            usuario_id_str = request.form.get("usuario_id")
            try:
                nuevo_usuario_id = int(usuario_id_str)
                usuario_nuevo = User.query.get(nuevo_usuario_id)
                if usuario_nuevo is None:
                    raise ValueError
            except (TypeError, ValueError):
                flash("Usuario no válido.", "error")
                return redirect(url_for("editar_registro", registro_id=registro_id))

            entrada_id_str = request.form.get("entrada_id", "").strip()
            salida_id_str = request.form.get("salida_id", "").strip()

            if "eliminar" in request.form:
                if entrada_id_str:
                    entrada = Registro.query.get(int(entrada_id_str))
                    if entrada:
                        db.session.delete(entrada)
                if salida_id_str:
                    salida = Registro.query.get(int(salida_id_str))
                    if salida:
                        db.session.delete(salida)

                db.session.commit()
                flash("Registro (intervalo) eliminado correctamente.", "success")
                return redirect(url_for("admin_registros"))

            entrada_momento_str = request.form.get("entrada_momento", "").strip()
            entrada_lat_str = request.form.get("entrada_latitude", "").strip()
            entrada_lon_str = request.form.get("entrada_longitude", "").strip()

            entrada = Registro.query.get(int(entrada_id_str)) if entrada_id_str else None
            entrada_momento = None

            if entrada_momento_str:
                try:
                    entrada_local = datetime.strptime(
                        entrada_momento_str, "%Y-%m-%dT%H:%M"
                    )
                except (TypeError, ValueError):
                    flash("Fecha y hora de entrada no válidas.", "error")
                    return redirect(url_for("editar_registro", registro_id=registro_id))

                try:
                    entrada_lat = float(entrada_lat_str.replace(",", ".")) if entrada_lat_str else None
                    entrada_lon = float(entrada_lon_str.replace(",", ".")) if entrada_lon_str else None
                except ValueError:
                    flash("Latitud/longitud de entrada no válidas.", "error")
                    return redirect(url_for("editar_registro", registro_id=registro_id))

                entrada_momento = local_to_utc_naive(entrada_local)

                if entrada:
                    auditoria_e = RegistroEdicion(
                        registro_id=entrada.id,
                        editor_id=current_user.id,
                        edit_time=datetime.utcnow(),
                        editor_ip=request.remote_addr,
                        old_accion=entrada.accion,
                        old_momento=entrada.momento,
                        old_latitude=entrada.latitude,
                        old_longitude=entrada.longitude,
                    )
                    db.session.add(auditoria_e)

                    entrada.usuario_id = nuevo_usuario_id
                    entrada.accion = "entrada"
                    entrada.momento = entrada_momento
                    entrada.latitude = entrada_lat
                    entrada.longitude = entrada_lon
                else:
                    entrada = Registro(
                        usuario_id=nuevo_usuario_id,
                        accion="entrada",
                        momento=entrada_momento,
                        latitude=entrada_lat,
                        longitude=entrada_lon,
                    )
                    db.session.add(entrada)

            salida_momento_str = request.form.get("salida_momento", "").strip()
            salida_lat_str = request.form.get("salida_latitude", "").strip()
            salida_lon_str = request.form.get("salida_longitude", "").strip()

            salida = Registro.query.get(int(salida_id_str)) if salida_id_str else None
            salida_momento = None

            if salida_momento_str:
                try:
                    salida_local = datetime.strptime(
                        salida_momento_str, "%Y-%m-%dT%H:%M"
                    )
                except (TypeError, ValueError):
                    flash("Fecha y hora de salida no válidas.", "error")
                    return redirect(url_for("editar_registro", registro_id=registro_id))

                try:
                    salida_lat = float(salida_lat_str.replace(",", ".")) if salida_lat_str else None
                    salida_lon = float(salida_lon_str.replace(",", ".")) if salida_lon_str else None
                except ValueError:
                    flash("Latitud/longitud de salida no válidas.", "error")
                    return redirect(url_for("editar_registro", registro_id=registro_id))

                salida_momento = local_to_utc_naive(salida_local)

                if salida:
                    auditoria_s = RegistroEdicion(
                        registro_id=salida.id,
                        editor_id=current_user.id,
                        edit_time=datetime.utcnow(),
                        editor_ip=request.remote_addr,
                        old_accion=salida.accion,
                        old_momento=salida.momento,
                        old_latitude=salida.latitude,
                        old_longitude=salida.longitude,
                    )
                    db.session.add(auditoria_s)

                    salida.usuario_id = nuevo_usuario_id
                    salida.accion = "salida"
                    salida.momento = salida_momento
                    salida.latitude = salida_lat
                    salida.longitude = salida_lon
                else:
                    salida = Registro(
                        usuario_id=nuevo_usuario_id,
                        accion="salida",
                        momento=salida_momento,
                        latitude=salida_lat,
                        longitude=salida_lon,
                    )
                    db.session.add(salida)

            if entrada_id_str and not entrada:
                entrada = Registro.query.get(int(entrada_id_str))
            if salida_id_str and not salida:
                salida = Registro.query.get(int(salida_id_str))

            entrada_m = entrada.momento if entrada else None
            salida_m = salida.momento if salida else None

            if entrada_m and salida_m and entrada_m > salida_m:
                db.session.rollback()
                flash("La fecha/hora de entrada no puede ser posterior a la de salida.", "error")
                return redirect(url_for("editar_registro", registro_id=registro_id))

            descanso_str = request.form.get("descanso_manual", "").strip()

            if entrada_m and salida_m and descanso_str:
                try:
                    partes = descanso_str.split(":")
                    if len(partes) != 2:
                        raise ValueError("Formato incorrecto")

                    horas = int(partes[0])
                    minutos = int(partes[1])
                    total_min = horas * 60 + minutos

                    if total_min < 0:
                        total_min = 0
                except Exception:
                    db.session.rollback()
                    flash("Formato de descanso no válido (usa HH:MM).", "error")
                    return redirect(url_for("editar_registro", registro_id=registro_id))

                if total_min >= 0:
                    Registro.query.filter(
                        Registro.usuario_id == nuevo_usuario_id,
                        Registro.momento >= entrada_m,
                        Registro.momento <= salida_m,
                        Registro.accion.in_(["descanso_inicio", "descanso_fin"]),
                    ).delete(synchronize_session=False)

                    if total_min > 0:
                        duracion_descanso = timedelta(minutes=total_min)

                        dur_trabajo = salida_m - entrada_m
                        if dur_trabajo.total_seconds() < duracion_descanso.total_seconds():
                            duracion_descanso = dur_trabajo

                        mitad = entrada_m + dur_trabajo / 2
                        inicio_descanso = mitad - duracion_descanso / 2
                        fin_descanso = inicio_descanso + duracion_descanso

                        reg_ini = Registro(
                            usuario_id=nuevo_usuario_id,
                            accion="descanso_inicio",
                            momento=inicio_descanso,
                            latitude=entrada.latitude if entrada else None,
                            longitude=entrada.longitude if entrada else None,
                        )
                        reg_fin = Registro(
                            usuario_id=nuevo_usuario_id,
                            accion="descanso_fin",
                            momento=fin_descanso,
                            latitude=salida.latitude if salida else None,
                            longitude=salida.longitude if salida else None,
                        )
                        db.session.add(reg_ini)
                        db.session.add(reg_fin)

            db.session.commit()
            flash("Registro actualizado correctamente.", "success")
            return redirect(url_for("admin_registros"))

        reg_base = Registro.query.get_or_404(registro_id)

        regs_usuario = (
            Registro.query.filter_by(usuario_id=reg_base.usuario_id)
            .order_by(Registro.momento.asc())
            .all()
        )
        intervalos = agrupar_registros_en_intervalos(regs_usuario)

        intervalo = None
        for it in intervalos:
            if (it.entrada and it.entrada.id == registro_id) or \
               (it.salida and it.salida.id == registro_id) or \
               (it.row_id == registro_id):
                intervalo = it
                break

        if intervalo is None:
            if reg_base.accion == "entrada":
                entrada = reg_base
                salida = None
            else:
                entrada = None
                salida = reg_base

            intervalo = SimpleNamespace(
                usuario=reg_base.usuario,
                entrada=entrada,
                salida=salida,
                descanso_en_curso=False,
                descanso_total=None,
                descanso_label=None,
            )

        entrada = intervalo.entrada
        salida = intervalo.salida

        entrada_momento_val = ""
        if entrada and entrada.momento:
            entrada_local = app.jinja_env.filters["to_local"](entrada.momento)
            entrada_momento_val = entrada_local.strftime("%Y-%m-%dT%H:%M")

        salida_momento_val = ""
        if salida and salida.momento:
            salida_local = app.jinja_env.filters["to_local"](salida.momento)
            salida_momento_val = salida_local.strftime("%Y-%m-%dT%H:%M")

        entrada_lat = f"{entrada.latitude:.6f}" if entrada and entrada.latitude is not None else ""
        entrada_lon = f"{entrada.longitude:.6f}" if entrada and entrada.longitude is not None else ""
        salida_lat = f"{salida.latitude:.6f}" if salida and salida.latitude is not None else ""
        salida_lon = f"{salida.longitude:.6f}" if salida and salida.longitude is not None else ""
        if intervalo.usuario and entrada:
            descanso_td, descanso_en_curso, _ = calcular_descanso_intervalo_para_usuario(
                intervalo.usuario.id,
                entrada.momento,
                salida.momento if salida else None,
            )
        else:
            descanso_td, descanso_en_curso = timedelta(0), False

        descanso_val = formatear_timedelta(descanso_td) if descanso_td else "00:00"
        return render_template(
            "admin_registro_editar.html",
            usuarios=usuarios,
            intervalo=intervalo,
            entrada=entrada,
            salida=salida,
            entrada_momento_val=entrada_momento_val,
            salida_momento_val=salida_momento_val,
            entrada_lat=entrada_lat,
            entrada_lon=entrada_lon,
            salida_lat=salida_lat,
            salida_lon=salida_lon,
            descanso_val=descanso_val,
        )

    @app.route("/admin/registros/nuevo", methods=["GET", "POST"])
    @admin_required
    def admin_registro_nuevo():
        """
        Crear un registro (intervalo) desde cero.
        Reutiliza el mismo formulario que la edición.
        """
        usuarios = User.query.order_by(User.username).all()

        if request.method == "POST":
            usuario_id_str = request.form.get("usuario_id")
            try:
                nuevo_usuario_id = int(usuario_id_str)
                usuario_nuevo = User.query.get(nuevo_usuario_id)
                if usuario_nuevo is None:
                    raise ValueError
            except (TypeError, ValueError):
                flash("Usuario no válido.", "error")
                return redirect(url_for("admin_registro_nuevo"))

            entrada_momento_str = request.form.get("entrada_momento", "").strip()
            salida_momento_str = request.form.get("salida_momento", "").strip()
            entrada_lat_str = request.form.get("entrada_latitude", "").strip()
            entrada_lon_str = request.form.get("entrada_longitude", "").strip()
            salida_lat_str = request.form.get("salida_latitude", "").strip()
            salida_lon_str = request.form.get("salida_longitude", "").strip()

            entrada = None
            salida = None

            if entrada_momento_str:
                try:
                    entrada_local = datetime.strptime(
                        entrada_momento_str, "%Y-%m-%dT%H:%M"
                    )
                    entrada_lat = float(entrada_lat_str.replace(",", ".")) if entrada_lat_str else None
                    entrada_lon = float(entrada_lon_str.replace(",", ".")) if entrada_lon_str else None
                except Exception:
                    flash("Datos de entrada no válidos.", "error")
                    return redirect(url_for("admin_registro_nuevo"))

                entrada_momento = local_to_utc_naive(entrada_local)
                entrada = Registro(
                    usuario_id=nuevo_usuario_id,
                    accion="entrada",
                    momento=entrada_momento,
                    latitude=entrada_lat,
                    longitude=entrada_lon,
                )
                db.session.add(entrada)

            if salida_momento_str:
                try:
                    salida_local = datetime.strptime(
                        salida_momento_str, "%Y-%m-%dT%H:%M"
                    )
                    salida_lat = float(salida_lat_str.replace(",", ".")) if salida_lat_str else None
                    salida_lon = float(salida_lon_str.replace(",", ".")) if salida_lon_str else None
                except Exception:
                    flash("Datos de salida no válidos.", "error")
                    db.session.rollback()
                    return redirect(url_for("admin_registro_nuevo"))

                salida_momento = local_to_utc_naive(salida_local)
                salida = Registro(
                    usuario_id=nuevo_usuario_id,
                    accion="salida",
                    momento=salida_momento,
                    latitude=salida_lat,
                    longitude=salida_lon,
                )
                db.session.add(salida)

            if entrada and salida and entrada.momento > salida.momento:
                db.session.rollback()
                flash("La fecha/hora de entrada no puede ser posterior a la de salida.", "error")
                return redirect(url_for("admin_registro_nuevo"))

            db.session.commit()
            if entrada:
                return redirect(url_for("editar_registro", registro_id=entrada.id))
            elif salida:
                return redirect(url_for("editar_registro", registro_id=salida.id))
            else:
                flash("Debes indicar al menos una entrada o una salida.", "error")
                return redirect(url_for("admin_registro_nuevo"))

        # GET: intervalo vacío
        intervalo = SimpleNamespace(
            usuario=None,
            entrada=None,
            salida=None,
            descanso_en_curso=False,
            descanso_total=None,
            descanso_label=None,
        )
        return render_template(
            "admin_registro_editar.html",
            usuarios=usuarios,
            intervalo=intervalo,
            entrada=None,
            salida=None,
            entrada_momento_val="",
            salida_momento_val="",
            entrada_lat="",
            entrada_lon="",
            salida_lat="",
            salida_lon="",
            descanso_val="00:00",
        )
