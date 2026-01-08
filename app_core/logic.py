from collections import defaultdict
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Optional

from flask import current_app

from geo_utils import is_within_radius
from services_fichaje import (
    validar_secuencia_fichaje,
    formatear_timedelta,
)

from .config import TZ_LOCAL, local_to_utc_naive
from .extensions import db
from .models import (
    Kiosk,
    KioskUser,
    Location,
    Registro,
    RegistroJustificacion,
    User,
    UserScheduleSettings,
    Schedule,
    ScheduleDay,
)


def obtener_ubicaciones_usuario(user):
    """
    Devuelve una lista de Location asociadas al usuario.
    Soporta:
      - Esquema nuevo: user.locations_multi (many-to-many)
      - Esquema antiguo: user.location (FK simple)
    """
    locs = []

    # Esquema nuevo many-to-many
    if hasattr(user, "locations_multi") and user.locations_multi:
        locs = list(user.locations_multi)

    # Esquema antiguo one-to-many (location_id + relationship location)
    elif getattr(user, "location", None) is not None:
        locs = [user.location]

    return locs


def usuario_tiene_flexible(user) -> bool:
    """
    Devuelve True si el usuario tiene alguna ubicación llamada 'Flexible'
    (ignorando mayúsculas/minúsculas).
    """
    for loc in obtener_ubicaciones_usuario(user):
        if (loc.name or "").lower() == "flexible":
            return True
    return False


def get_or_create_schedule_settings(user):
    """
    Devuelve el objeto UserScheduleSettings para el usuario.
    Si no existe, lo crea con valores por defecto.
    """
    settings = getattr(user, "schedule_settings", None)
    if settings is None:
        settings = UserScheduleSettings(user_id=user.id)
        db.session.add(settings)
        db.session.commit()
    return settings


def obtener_horario_aplicable(usuario, dt):
    """
    Devuelve un Schedule aplicable al usuario para la fecha dt.
    Versión simple: si el usuario tiene varios horarios, usamos el primero.
    Si no tiene ninguno, devolvemos None.
    """
    if not hasattr(usuario, "schedules"):
        return None

    schedules = list(usuario.schedules)
    if not schedules:
        return None

    # TODO (futuro): si settings.detect_schedule está activo, elegir el que mejor encaje
    return schedules[0]


def calcular_jornada_teorica(schedule: Schedule, dt: datetime.date) -> timedelta:
    """
    Devuelve la duración teórica de trabajo para un día concreto (dt)
    según el horario (global o por días).
    """
    # MODO POR DÍAS
    if schedule.use_per_day:
        dow = dt.weekday()  # 0 = lunes ... 6 = domingo
        dia = next((d for d in schedule.days if d.day_of_week == dow), None)
        if dia is None:
            # Día sin configuración -> no se trabaja
            return timedelta(0)

        inicio = datetime.combine(dt, dia.start_time)
        fin = datetime.combine(dt, dia.end_time)

        # Si cruza medianoche
        if fin <= inicio:
            fin += timedelta(days=1)

        duracion = fin - inicio

        break_paid = getattr(dia, "break_paid", False)
        if not break_paid:
            # Descanso
            if dia.break_type == "fixed":
                if dia.break_start and dia.break_end:
                    b_inicio = datetime.combine(dt, dia.break_start)
                    b_fin = datetime.combine(dt, dia.break_end)
                    if b_fin <= b_inicio:
                        b_fin += timedelta(days=1)
                    duracion -= (b_fin - b_inicio)
            elif dia.break_type == "flexible":
                if dia.break_minutes:
                    duracion -= timedelta(minutes=dia.break_minutes or 0)

        if duracion.total_seconds() < 0:
            duracion = timedelta(0)

        return duracion

    # MODO SIMPLE (mismas horas todos los días)
    if not schedule.start_time or not schedule.end_time:
        return timedelta(0)

    inicio = datetime.combine(dt, schedule.start_time)
    fin = datetime.combine(dt, schedule.end_time)

    if fin <= inicio:
        fin += timedelta(days=1)

    duracion = fin - inicio

    break_paid = getattr(schedule, "break_paid", False)
    if not break_paid:
        if schedule.break_type == "fixed":
            if schedule.break_start and schedule.break_end:
                b_inicio = datetime.combine(dt, schedule.break_start)
                b_fin = datetime.combine(dt, schedule.break_end)
                if b_fin <= b_inicio:
                    b_fin += timedelta(days=1)
                duracion -= (b_fin - b_inicio)
        elif schedule.break_type == "flexible":
            if schedule.break_minutes:
                duracion -= timedelta(minutes=schedule.break_minutes or 0)

    if duracion.total_seconds() < 0:
        duracion = timedelta(0)

    return duracion


def calcular_duracion_trabajada_intervalo(it) -> Optional[timedelta]:
    """
    Devuelve la duración real del intervalo (entrada->salida).
    Si falta entrada o salida, devuelve None (no calculamos todavía).
    """
    if it.entrada_momento is None or it.salida_momento is None:
        return None

    inicio = it.entrada_momento
    fin = it.salida_momento
    real = fin - inicio

    # Si el fichaje cruza medianoche (salida "antes" que entrada), corregimos
    if real.total_seconds() < 0:
        real += timedelta(days=1)

    return real


def calcular_descanso_intervalos(intervalos, registros, ahora=None):
    """
    Para cada intervalo (entrada/salida) calcula:
      - it.descanso_total: timedelta total de descanso REAL dentro del intervalo
      - it.descanso_en_curso: bool (True si hay un descanso abierto)
      - it.descanso_label: texto amigable para mostrar en la tabla
    """
    if ahora is None:
        ahora = datetime.now()

    # Agrupamos todos los registros por usuario
    regs_por_usuario = defaultdict(list)
    for r in registros:
        if r.usuario_id is not None and r.momento is not None:
            regs_por_usuario[r.usuario_id].append(r)

    # Ordenamos cronológicamente por usuario
    for uid in regs_por_usuario:
        regs_por_usuario[uid].sort(key=lambda r: r.momento)

    for it in intervalos:
        it.descanso_total = timedelta(0)
        it.descanso_en_curso = False
        it.descanso_label = "Sin descanso"

        if not getattr(it, "usuario", None) or not it.usuario:
            continue

        regs_usuario = regs_por_usuario.get(it.usuario.id, [])
        if not regs_usuario:
            continue

        # Determinar ventana de tiempo del intervalo
        if it.entrada_momento:
            inicio_ventana = it.entrada_momento
        elif it.salida_momento:
            inicio_ventana = it.salida_momento - timedelta(hours=12)
        else:
            continue

        if it.salida_momento:
            fin_ventana = it.salida_momento
        else:
            fin_ventana = ahora

        # Ajuste por si cruza medianoche (salida "antes" que entrada)
        if fin_ventana < inicio_ventana:
            fin_ventana += timedelta(days=1)

        total = timedelta(0)
        ultimo_inicio = None

        for r in regs_usuario:
            if r.momento < inicio_ventana:
                continue
            if r.momento > fin_ventana:
                break

            if r.accion == "descanso_inicio":
                ultimo_inicio = r.momento
            elif r.accion == "descanso_fin" and ultimo_inicio is not None:
                fin = r.momento
                if fin < ultimo_inicio:
                    fin += timedelta(days=1)
                total += (fin - ultimo_inicio)
                ultimo_inicio = None

        en_curso = False
        if ultimo_inicio is not None:
            fin = fin_ventana
            if fin < ultimo_inicio:
                fin += timedelta(days=1)
            total += (fin - ultimo_inicio)

            if it.salida_momento is None:
                en_curso = True

        it.descanso_total = total
        it.descanso_en_curso = en_curso

        if en_curso and total.total_seconds() > 0:
            it.descanso_label = f"Descansando ({formatear_timedelta(total)})"
        elif total.total_seconds() > 0:
            it.descanso_label = formatear_timedelta(total)
        else:
            it.descanso_label = "Sin descanso"


def calcular_descanso_intervalo_para_usuario(
    usuario_id,
    entrada_momento,
    salida_momento=None,
    ahora=None,
):
    """
    Calcula el tiempo de descanso real dentro de un intervalo [entrada_momento, salida_momento]
    usando registros 'descanso_inicio' / 'descanso_fin' del usuario.
    """
    if entrada_momento is None:
        return timedelta(0), False, None

    if ahora is None:
        ahora = datetime.utcnow()

    if salida_momento is None:
        limite_superior = ahora
    else:
        limite_superior = salida_momento

    registros_descanso = (
        Registro.query.filter(
            Registro.usuario_id == usuario_id,
            Registro.momento >= entrada_momento,
            Registro.momento <= limite_superior,
            Registro.accion.in_(["descanso_inicio", "descanso_fin"]),
        )
        .order_by(Registro.momento.asc())
        .all()
    )

    total = timedelta(0)
    inicio_actual = None
    descanso_en_curso = False
    inicio_en_curso = None

    for r in registros_descanso:
        if r.accion == "descanso_inicio":
            if inicio_actual is None:
                inicio_actual = r.momento
        elif r.accion == "descanso_fin":
            if inicio_actual is not None:
                fin = r.momento
                if fin < inicio_actual:
                    fin = inicio_actual
                total += (fin - inicio_actual)
                inicio_actual = None

    if inicio_actual is not None:
        if salida_momento is None:
            if ahora > inicio_actual:
                total += (ahora - inicio_actual)
            descanso_en_curso = True
            inicio_en_curso = inicio_actual
        else:
            fin = limite_superior
            if fin > inicio_actual:
                total += (fin - inicio_actual)

    return total, descanso_en_curso, inicio_en_curso


def usuario_tiene_intervalo_abierto(user_id: int) -> bool:
    """
    Devuelve True si el usuario tiene una ENTRADA sin SALIDA posterior.
    Es decir, si está "en jornada" y aún no ha fichado la salida.
    """
    ultima_entrada = (
        Registro.query
        .filter_by(usuario_id=user_id, accion="entrada")
        .order_by(Registro.momento.desc())
        .first()
    )
    if not ultima_entrada:
        return False

    salida_posterior = (
        Registro.query
        .filter(
            Registro.usuario_id == user_id,
            Registro.accion == "salida",
            Registro.momento > ultima_entrada.momento,
        )
        .first()
    )
    return salida_posterior is None


def calcular_extra_y_defecto_intervalo(it):
    """
    Calcula y deja en el intervalo:
      - it.trabajo_real -> tiempo realmente trabajado en el intervalo
                           según horario + descansos.

    NOTA: ya no computamos extra/defecto por intervalo porque provoca
    restar la jornada teórica varias veces en un mismo día cuando hay
    múltiples intervalos. El extra/defecto se obtiene agregando
    trabajo_real por fecha (ver obtener_trabajo_y_esperado_por_periodo).

    Devuelve siempre (0, 0) para extra/defecto del intervalo.
    """
    it.trabajo_real = timedelta(0)

    if not it.usuario:
        return timedelta(0), timedelta(0)

    dur_real = calcular_duracion_trabajada_intervalo(it)
    if dur_real is None:
        return timedelta(0), timedelta(0)

    user = it.usuario
    fecha = it.entrada_momento.date()

    descanso_real_td, _, _ = calcular_descanso_intervalo_para_usuario(
        user.id,
        it.entrada_momento,
        it.salida_momento,
    )

    schedule = obtener_horario_aplicable(user, fecha)

    if schedule is None:
        trabajo_neto = dur_real - descanso_real_td
        if trabajo_neto.total_seconds() < 0:
            trabajo_neto = timedelta(0)
        it.trabajo_real = trabajo_neto
        return trabajo_neto, timedelta(0)

    if schedule.use_per_day:
        dow = fecha.weekday()
        dia = next((d for d in schedule.days if d.day_of_week == dow), None)
        if dia is None:
            trabajo_neto = dur_real - descanso_real_td
            if trabajo_neto.total_seconds() < 0:
                trabajo_neto = timedelta(0)
            it.trabajo_real = trabajo_neto
            return trabajo_neto, timedelta(0)

        inicio_j = datetime.combine(fecha, dia.start_time)
        fin_j = datetime.combine(fecha, dia.end_time)
        if fin_j <= inicio_j:
            fin_j += timedelta(days=1)

        longitud_bruta = fin_j - inicio_j

        break_optional = getattr(dia, "break_optional", False)
        break_paid = getattr(dia, "break_paid", False)

        if break_paid:
            descanso_teorico_td = timedelta(0)
        elif dia.break_type == "fixed" and dia.break_start and dia.break_end:
            b_inicio = datetime.combine(fecha, dia.break_start)
            b_fin = datetime.combine(fecha, dia.break_end)
            if b_fin <= b_inicio:
                b_fin += timedelta(days=1)
            descanso_teorico_td = b_fin - b_inicio
        elif dia.break_type == "flexible" and (dia.break_minutes or 0) > 0:
            descanso_teorico_td = timedelta(minutes=dia.break_minutes or 0)
        else:
            descanso_teorico_td = timedelta(0)

    else:
        if not schedule.start_time or not schedule.end_time:
            trabajo_neto = dur_real - descanso_real_td
            if trabajo_neto.total_seconds() < 0:
                trabajo_neto = timedelta(0)
            it.trabajo_real = trabajo_neto
            return trabajo_neto, timedelta(0)

        inicio_j = datetime.combine(fecha, schedule.start_time)
        fin_j = datetime.combine(fecha, schedule.end_time)
        if fin_j <= inicio_j:
            fin_j += timedelta(days=1)

        longitud_bruta = fin_j - inicio_j

        break_optional = getattr(schedule, "break_optional", False)
        break_paid = getattr(schedule, "break_paid", False)

        if break_paid:
            descanso_teorico_td = timedelta(0)
        elif schedule.break_type == "fixed" and schedule.break_start and schedule.break_end:
            b_inicio = datetime.combine(fecha, schedule.break_start)
            b_fin = datetime.combine(fecha, schedule.break_end)
            if b_fin <= b_inicio:
                b_fin += timedelta(days=1)
            descanso_teorico_td = b_fin - b_inicio
        elif schedule.break_type == "flexible" and (schedule.break_minutes or 0) > 0:
            descanso_teorico_td = timedelta(minutes=schedule.break_minutes or 0)
        else:
            descanso_teorico_td = timedelta(0)

    dur_teorica = longitud_bruta - descanso_teorico_td
    if dur_teorica.total_seconds() < 0:
        dur_teorica = timedelta(0)

    if break_paid:
        descanso_efectivo = timedelta(0)
    elif break_optional:
        descanso_efectivo = descanso_real_td
    else:
        descanso_efectivo = max(descanso_real_td, descanso_teorico_td)

    trabajo_real = dur_real - descanso_efectivo
    if trabajo_real.total_seconds() < 0:
        trabajo_real = timedelta(0)

    it.trabajo_real = trabajo_real

    # Extra/defecto se calcula a nivel de día/periodo, no por intervalo.
    return timedelta(0), timedelta(0)


def obtener_trabajo_y_esperado_por_periodo(usuario, trabajos_por_fecha, modo="dia"):
    """
    Agrupa el trabajo real y esperado por día/semana/mes según 'modo'.
    trabajos_por_fecha: dict fecha -> timedelta trabajada
    """
    grupos = defaultdict(lambda: {"trabajado": timedelta(0), "esperado": timedelta(0)})

    for fecha, trabajado in trabajos_por_fecha.items():
        schedule = obtener_horario_aplicable(usuario, fecha)
        esperado = calcular_jornada_teorica(schedule, fecha) if schedule else timedelta(0)

        if modo == "semanal":
            clave = (fecha.isocalendar().year, fecha.isocalendar().week)
        elif modo == "mensual":
            clave = (fecha.year, fecha.month)
        else:
            clave = fecha

        grupos[clave]["trabajado"] += trabajado
        grupos[clave]["esperado"] += esperado

    # Totales acumulados
    total_trabajado = sum((v["trabajado"] for v in grupos.values()), timedelta(0))
    total_esperado = sum((v["esperado"] for v in grupos.values()), timedelta(0))

    extra = timedelta(0)
    defecto = timedelta(0)
    for data in grupos.values():
        diff = data["trabajado"] - data["esperado"]
        if diff.total_seconds() > 0:
            extra += diff
        elif diff.total_seconds() < 0:
            defecto += -diff

    return total_trabajado, total_esperado, extra, defecto


def determinar_ubicacion_por_coordenadas(lat, lon, ubicaciones, margen_extra_m=10.0):
    """
    Dado un par (lat, lon) y una lista de Location,
    devuelve la Location cuyo área (radio_meters) contenga ese punto.
    """
    if lat is None or lon is None:
        return None

    for loc in ubicaciones:
        radio_base = loc.radius_meters or 0.0
        radio_efectivo = radio_base + margen_extra_m

        if radio_efectivo <= 0:
            continue

        if is_within_radius(
            lat,
            lon,
            loc.latitude,
            loc.longitude,
            radio_efectivo,
        ):
            return loc

    return None


def construir_intervalo(entrada, salida, ubicaciones_definidas):
    """
    Construye un objeto 'intervalo' a partir de una posible entrada y una posible salida.
    """

    usuario = entrada.usuario if entrada is not None else salida.usuario if salida else None

    def info_ubicacion(reg):
        """
        Para un Registro devuelve (label, lat, lon)
        """
        if reg is None:
            return None, None, None

        lat = reg.latitude
        lon = reg.longitude
        if lat is None or lon is None:
            return "Sin datos", None, None

        loc = determinar_ubicacion_por_coordenadas(lat, lon, ubicaciones_definidas)
        if loc:
            label = loc.name
        else:
            try:
                label = f"{lat:.6f}, {lon:.6f}"
            except Exception:
                label = f"{lat}, {lon}"
        return label, lat, lon

    label_e, lat_e, lon_e = info_ubicacion(entrada)
    label_s, lat_s, lon_s = info_ubicacion(salida)

    if label_e and label_s:
        if label_e == label_s:
            ubicacion_label = label_e
        else:
            ubicacion_label = f"{label_e} - {label_s}"
    else:
        ubicacion_label = label_e or label_s or "Sin datos"

    row_id = entrada.id if entrada is not None else salida.id if salida is not None else None

    return SimpleNamespace(
        usuario=usuario,
        entrada=entrada,
        salida=salida,
        entrada_momento=entrada.momento if entrada is not None else None,
        salida_momento=salida.momento if salida is not None else None,
        label_entrada=label_e,
        label_salida=label_s,
        ubicacion_label=ubicacion_label,
        entrada_lat=lat_e,
        entrada_lon=lon_e,
        salida_lat=lat_s,
        salida_lon=lon_s,
        row_id=row_id,
    )


def agrupar_registros_en_intervalos(registros):
    """
    A partir de una lista de Registro (ya filtrada),
    agrupa en intervalos Entrada/Salida por usuario, sin cortar por día.
    """
    intervalos = []

    ubicaciones_definidas = Location.query.filter(
        Location.name != "Flexible"
    ).all()

    regs_por_usuario = defaultdict(list)
    for r in registros:
        if r.usuario_id is None or r.momento is None:
            continue
        regs_por_usuario[r.usuario_id].append(r)

    for uid, regs_usuario in regs_por_usuario.items():
        regs_ordenados = sorted(regs_usuario, key=lambda x: x.momento)
        entrada_actual = None

        for r in regs_ordenados:
            if r.accion == "entrada":
                if entrada_actual is None:
                    entrada_actual = r
                else:
                    intervalos.append(
                        construir_intervalo(
                            entrada_actual, None, ubicaciones_definidas
                        )
                    )
                    entrada_actual = r

            elif r.accion == "salida":
                if entrada_actual is not None:
                    intervalos.append(
                        construir_intervalo(
                            entrada_actual, r, ubicaciones_definidas
                        )
                    )
                    entrada_actual = None
                else:
                    intervalos.append(
                        construir_intervalo(
                            None, r, ubicaciones_definidas
                        )
                    )

        if entrada_actual is not None:
            intervalos.append(
                construir_intervalo(
                    entrada_actual, None, ubicaciones_definidas
                )
            )

    grupos = defaultdict(list)
    for it in intervalos:
        uid = it.usuario.id if it.usuario is not None else None

        if it.entrada_momento is not None:
            dia = it.entrada_momento.date()
        elif it.salida_momento is not None:
            dia = it.salida_momento.date()
        else:
            dia = None

        grupos[(uid, dia)].append(it)

    intervalos_limpios = []

    for (uid, dia), ints in grupos.items():
        completos = [
            it for it in ints
            if it.entrada_momento is not None and it.salida_momento is not None
        ]

        if not completos:
            intervalos_limpios.extend(ints)
            continue

        entradas_completas = {
            it.entrada_momento for it in completos if it.entrada_momento
        }
        salidas_completas = {
            it.salida_momento for it in completos if it.salida_momento
        }

        for it in ints:
            if it in completos:
                intervalos_limpios.append(it)
                continue

            descartar = False

            if (
                it.entrada_momento is not None
                and it.salida_momento is None
                and it.entrada_momento in entradas_completas
            ):
                descartar = True

            if (
                it.salida_momento is not None
                and it.entrada_momento is None
                and it.salida_momento in salidas_completas
            ):
                descartar = True

            if not descartar:
                intervalos_limpios.append(it)

    def key_intervalo(it):
        if it.entrada_momento is not None:
            return it.entrada_momento
        elif it.salida_momento is not None:
            return it.salida_momento
        else:
            return datetime.min

    intervalos_limpios.sort(key=key_intervalo, reverse=True)
    return intervalos_limpios


def calcular_horas_trabajadas(registros):
    """
    Delegamos en la implementación original de services_fichaje para no
    duplicar lógica aquí y evitar errores.
    """
    from services_fichaje import calcular_horas_trabajadas as _calc
    return _calc(registros)


__all__ = [
    "agrupar_registros_en_intervalos",
    "calcular_descanso_intervalo_para_usuario",
    "calcular_descanso_intervalos",
    "calcular_duracion_trabajada_intervalo",
    "calcular_extra_y_defecto_intervalo",
    "calcular_horas_trabajadas",
    "calcular_jornada_teorica",
    "construir_intervalo",
    "determinar_ubicacion_por_coordenadas",
    "get_or_create_schedule_settings",
    "obtener_horario_aplicable",
    "obtener_ubicaciones_usuario",
    "usuario_tiene_flexible",
    "usuario_tiene_intervalo_abierto",
    "validar_secuencia_fichaje",
    "formatear_timedelta",
    "TZ_LOCAL",
    "local_to_utc_naive",
]
