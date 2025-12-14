from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Optional

# Zona horaria local (España peninsular)
TZ_LOCAL = ZoneInfo("Europe/Madrid")


def to_local(dt_utc_naive: Optional[datetime]):
    """
    Convierte un datetime naive de BD (interpretado como UTC)
    a hora local Europe/Madrid (aware), respetando cambios de horario.
    """
    if dt_utc_naive is None:
        return None

    # Si ya viene con tzinfo, lo consideramos en UTC
    if dt_utc_naive.tzinfo is not None:
        dt_aware_utc = dt_utc_naive.astimezone(timezone.utc)
    else:
        dt_aware_utc = dt_utc_naive.replace(tzinfo=timezone.utc)

    return dt_aware_utc.astimezone(TZ_LOCAL)


def local_to_utc_naive(dt_local_naive: Optional[datetime]):
    """
    Convierte un datetime naive de hora local Europe/Madrid
    a datetime naive en UTC (para guardar en BD).
    """
    if dt_local_naive is None:
        return None

    # Interpretamos el naive como hora local
    dt_local_aware = dt_local_naive.replace(tzinfo=TZ_LOCAL)
    dt_utc_aware = dt_local_aware.astimezone(timezone.utc)
    return dt_utc_aware.replace(tzinfo=None)
