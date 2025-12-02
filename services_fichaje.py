from datetime import datetime, timedelta, time
from typing import Dict, Tuple, Optional

def validar_secuencia_fichaje(accion: str, ultimo_registro) -> Tuple[bool, str]:
    """
    Valida que la acción de fichaje tenga sentido según el último registro.
    Devuelve (es_valido, mensaje_error).
    ultimo_registro puede ser None o un objeto con atributo .accion
    """

    if accion not in ("entrada", "salida"):
        return False, "Acción no válida."

    # Sin registros previos
    if ultimo_registro is None:
        if accion == "salida":
            return False, "Tu primer fichaje debe ser una ENTRADA, no una salida."
        return True, ""  # Primer fichaje debe ser una entrada

    # Hay registros previos
    if ultimo_registro.accion == accion:
        if accion == "entrada":
            return False, "Ya tienes una ENTRADA sin una SALIDA posterior."
        else:
            return False, "Ya has fichado SALIDA. Debes fichar ENTRADA antes de otra SALIDA."

    return True, ""


def calcular_horas_trabajadas(registros):
    """
    Calcula las horas trabajadas por cada usuario según los registros de entrada y salida.
    Devuelve un diccionario con el nombre del usuario y el total de horas trabajadas.
    """
    horas_trabajadas = {}
    entrada = None  # Variable para almacenar el momento de la entrada

    for registro in registros:
        usuario = registro.usuario.username
        if usuario not in horas_trabajadas:
            horas_trabajadas[usuario] = timedelta()

        if registro.accion == 'entrada':
            # Guardamos el momento de la entrada
            entrada = registro.momento
        elif registro.accion == 'salida' and entrada:
            # Solo calculamos el tiempo trabajado si hay una entrada previa
            horas_trabajadas[usuario] += registro.momento - entrada
            entrada = None  # Reseteamos la entrada después de la salida

    print("Horas trabajadas por usuario:", horas_trabajadas)  # Imprimir para depuración
    return horas_trabajadas

def formatear_timedelta(td: timedelta) -> str:
    """
    Convierte un timedelta a 'HH:MM' redondeando minutos.
    """
    total_segundos = int(td.total_seconds())
    horas = total_segundos // 3600
    minutos = (total_segundos % 3600) // 60
    return f"{horas:02d}:{minutos:02d}"

def calcular_duracion_jornada(schedule, fecha: datetime.date) -> timedelta:
    """
    Dado un Schedule y una fecha (por si en el futuro quieres por-día),
    devuelve la duración neta de la jornada (restando descansos).
    """
    # Construimos datetimes sólo para facilitar restas, usando la fecha indicada
    ws_dt = datetime.combine(fecha, schedule.work_start)
    we_dt = datetime.combine(fecha, schedule.work_end)

    if we_dt <= ws_dt:
        # Jornada que cruza medianoche (no lo hemos modelado aún, simplificamos)
        we_dt += timedelta(days=1)

    duracion = we_dt - ws_dt

    if schedule.break_type == "fixed" and schedule.fixed_break_start and schedule.fixed_break_end:
        bs = datetime.combine(fecha, schedule.fixed_break_start)
        be = datetime.combine(fecha, schedule.fixed_break_end)
        if be <= bs:
            be += timedelta(days=1)
        duracion -= (be - bs)

    elif schedule.break_type == "flexible" and schedule.flexible_break_minutes:
        duracion -= timedelta(minutes=schedule.flexible_break_minutes)

    if duracion.total_seconds() < 0:
        duracion = timedelta(0)

    return duracion


def calcular_extra_y_defecto(trabajado: timedelta,
                             schedule,
                             fecha: datetime.date) -> (timedelta, timedelta):
    """
    Devuelve (extra, defecto) en función del tiempo trabajado y la jornada del horario.
    """
    jornada = calcular_duracion_jornada(schedule, fecha)
    if trabajado > jornada:
        return (trabajado - jornada, timedelta(0))
    elif trabajado < jornada:
        return (timedelta(0), jornada - trabajado)
    else:
        return (timedelta(0), timedelta(0))