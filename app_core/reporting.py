import csv
from datetime import datetime, timedelta
from io import StringIO

from flask import Response, render_template
from flask_weasyprint import HTML, render_pdf

from .logic import (
    calcular_duracion_trabajada_intervalo,
    calcular_extra_y_defecto_intervalo,
    formatear_timedelta,
)


def generar_csv(intervalos):
    """Generar un archivo CSV a partir de los intervalos Entrada/Salida."""
    output = StringIO()
    writer = csv.writer(output, delimiter=";")

    writer.writerow([
        "Usuario",
        "Fecha/hora entrada",
        "Fecha/hora salida",
        "Descanso",
        "Ubicación",
        "Horas extra",
        "Horas en defecto",
    ])

    for it in intervalos:
        if it.entrada_momento is not None:
            fe = it.entrada_momento.strftime("%H:%M %d/%m/%Y")
        else:
            fe = ""

        if it.salida_momento is not None:
            fs = it.salida_momento.strftime("%H:%M %d/%m/%Y")
        else:
            fs = ""

        if hasattr(it, "descanso_total") and it.descanso_total:
            descanso_str = formatear_timedelta(it.descanso_total)
        else:
            descanso_str = "00:00"

        he = ""
        hd = ""
        if hasattr(it, "horas_extra") and it.horas_extra.total_seconds() > 0:
            he = formatear_timedelta(it.horas_extra)
        if hasattr(it, "horas_defecto") and it.horas_defecto.total_seconds() > 0:
            hd = formatear_timedelta(it.horas_defecto)

        writer.writerow([
            it.usuario.username if it.usuario else "",
            fe,
            fs,
            descanso_str,
            it.ubicacion_label or "",
            he,
            hd,
        ])

    csv_data = output.getvalue().encode("utf-8-sig")
    output.close()

    filename = f"registros_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.csv"
    return Response(
        csv_data,
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def generar_pdf(intervalos, tipo_periodo: str):
    """
    Genera un PDF usando la plantilla informe_pdf.html,
    mostrando intervalos Entrada/Salida y su resumen de horas.
    """

    for it in intervalos:
        extra_td, defecto_td = calcular_extra_y_defecto_intervalo(it)
        it.horas_extra = extra_td
        it.horas_defecto = defecto_td

    resumen_td = {}

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

        username = it.usuario.username
        resumen_td[username] = resumen_td.get(username, timedelta()) + trabajo_real

    resumen_horas = resumen_td

    html = render_template(
        "informe_pdf.html",
        intervalos=intervalos,
        resumen_horas=resumen_horas,
        tipo_periodo=tipo_periodo,
        formatear_timedelta=formatear_timedelta,
    )
    return render_pdf(HTML(string=html))
