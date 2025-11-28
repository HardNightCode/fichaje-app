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

