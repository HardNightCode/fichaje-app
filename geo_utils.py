import math

EARTH_RADIUS_M = 6371000  # radio de la Tierra en metros


def haversine_distance_m(lat1, lon1, lat2, lon2) -> float:
    """
    Devuelve la distancia en metros entre dos puntos (lat, lon) usando la fórmula de Haversine.
    Las latitudes y longitudes se reciben en grados decimales.
    """
    # Convertir a radianes
    rlat1 = math.radians(lat1)
    rlon1 = math.radians(lon1)
    rlat2 = math.radians(lat2)
    rlon2 = math.radians(lon2)

    dlat = rlat2 - rlat1
    dlon = rlon2 - rlon1

    a = math.sin(dlat / 2) ** 2 + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlon / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    return EARTH_RADIUS_M * c


def is_within_radius(lat_user, lon_user, lat_ref, lon_ref, radius_m: float) -> bool:
    """
    Devuelve True si la posición del usuario está dentro del radio especificado (en metros)
    respecto a la ubicación de referencia.
    """
    distance = haversine_distance_m(lat_user, lon_user, lat_ref, lon_ref)
    return distance <= radius_m
