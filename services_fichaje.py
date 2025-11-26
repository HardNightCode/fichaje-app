from datetime import datetime, timedelta
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
        return True, ""

    # Hay registros previos
    if ultimo_registro.accion == accion:
        if accion == "entrada":
            return False, "Ya tienes una ENTRADA sin una SALIDA posterior."
        else:
            return False, "Ya has fichado SALIDA. Debes fichar ENTRADA antes de otra SALIDA."

    return True, ""


def calcular_horas_trabajadas(registros) -> Dict[datetime.date, timedelta]:
    """
    A partir de una lista de registros ordenados por fecha (ascendente) para un usuario,
    calcula las horas trabajadas por día.
    Empareja ENTRADA -> SALIDA; si hay ENTRADA sin SALIDA, se ignora el último tramo.
    """
    horas_por_dia: Dict[datetime.date, timedelta] = {}
    entrada_actual: Optional[datetime] = None

    for reg in registros:
        if reg.accion == "entrada":
            entrada_actual = reg.momento
        elif reg.accion == "salida":
            if entrada_actual is not None and reg.momento > entrada_actual:
                dia = entrada_actual.date()
                duracion = reg.momento - entrada_actual
                horas_por_dia[dia] = horas_por_dia.get(dia, timedelta()) + duracion
                entrada_actual = None
            else:
                # SALIDA sin ENTRADA previa coherente: la ignoramos
                continue

    return horas_por_dia


def formatear_timedelta(td: timedelta) -> str:
    """
    Convierte un timedelta a 'HH:MM' redondeando minutos.
    """
    total_segundos = int(td.total_seconds())
    horas = total_segundos // 3600
    minutos = (total_segundos % 3600) // 60
    return f"{horas:02d}:{minutos:02d}"
