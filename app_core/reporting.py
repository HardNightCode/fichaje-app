import csv
from datetime import datetime, timedelta
from io import StringIO
from collections import defaultdict

from flask import Response, render_template
from flask_weasyprint import HTML, render_pdf

from .logic import (
    calcular_duracion_trabajada_intervalo,
    calcular_extra_y_defecto_intervalo,
    formatear_timedelta,
    obtener_trabajo_y_esperado_por_periodo,
)
from .models import CompanyInfo, RegistroEdicion


def _build_user_sections(intervalos, modo_conteo):
    per_user = defaultdict(list)
    trabajos_por_usuario_fecha = defaultdict(dict)

    for it in intervalos:
        if not it.usuario:
            continue
        per_user[it.usuario.username].append(it)

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

        fecha_base = it.entrada_momento.date() if it.entrada_momento else (
            it.salida_momento.date() if it.salida_momento else None
        )
        if fecha_base:
            trabajos_por_usuario_fecha[it.usuario.username][fecha_base] = trabajos_por_usuario_fecha[it.usuario.username].get(fecha_base, timedelta()) + trabajo_real

    def to_td(val):
        if val is None:
            return timedelta(0)
        if isinstance(val, (int, float)):
            return timedelta(seconds=val)
        return val

    sections = []
    for username, ints in per_user.items():
        ints_sorted = sorted(ints, key=lambda x: x.entrada_momento or x.salida_momento or datetime.min)
        user_obj = ints_sorted[0].usuario
        total_trab, total_esp, extra_td, defecto_td = obtener_trabajo_y_esperado_por_periodo(
            user_obj, trabajos_por_usuario_fecha.get(username, {}), modo_conteo
        )

        # Normalizar atributos para evitar ints en plantillas
        for it in ints_sorted:
            it.descanso_total = to_td(getattr(it, "descanso_total", None))
            it.horas_extra = to_td(getattr(it, "horas_extra", None))
            it.horas_defecto = to_td(getattr(it, "horas_defecto", None))

        sections.append({
            "username": username,
            "intervalos": ints_sorted,
            "trabajado": total_trab,
            "esperado": total_esp,
            "extra": extra_td,
            "defecto": defecto_td,
        })
    return sections


def generar_csv(intervalos, modo_conteo):
    """Generar un archivo CSV agrupado por usuario."""
    sections = _build_user_sections(intervalos, modo_conteo)

    output = StringIO()
    writer = csv.writer(output, delimiter=";")

    writer.writerow(["Usuario", "Fecha/hora entrada", "Fecha/hora salida", "Ubicación", "Descanso", "Extra", "Defecto"])

    for sec in sections:
        writer.writerow([sec["username"], "", "", "", "", "", ""])
        writer.writerow(["", "Trabajado", "Esperado", "Extra", "Defecto", "", ""])
        writer.writerow([
            "",
            formatear_timedelta(sec["trabajado"]),
            formatear_timedelta(sec["esperado"]),
            formatear_timedelta(sec["extra"]),
            formatear_timedelta(sec["defecto"]),
            "",
            "",
        ])
        for it in sec["intervalos"]:
            fe = it.entrada_momento.strftime("%H:%M %d/%m/%Y") if it.entrada_momento else ""
            fs = it.salida_momento.strftime("%H:%M %d/%m/%Y") if it.salida_momento else ""
            descanso_str = formatear_timedelta(getattr(it, "descanso_total", timedelta(0)))
            he_td = getattr(it, "horas_extra", timedelta(0)) or timedelta(0)
            hd_td = getattr(it, "horas_defecto", timedelta(0)) or timedelta(0)
            he = formatear_timedelta(he_td)
            hd = formatear_timedelta(hd_td)
            # Añadimos marcas para destacar visualmente en hojas de cálculo
            if he_td.total_seconds() > 0:
                he = f"+{he}"
            if hd_td.total_seconds() > 0:
                hd = f"-{hd}"
            writer.writerow([
                "",
                fe,
                fs,
                it.ubicacion_label or "",
                descanso_str,
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


def generar_pdf(intervalos, tipo_periodo: str, modo_conteo: str):
    """
    Genera un PDF usando la plantilla informe_pdf.html,
    agrupado por usuario e incluyendo extra/defecto según el modo.
    """

    sections = _build_user_sections(intervalos, modo_conteo)
    company = CompanyInfo.query.first()

    html = render_template(
        "informe_pdf.html",
        sections=sections,
        tipo_periodo=tipo_periodo,
        modo_conteo=modo_conteo,
        company=company,
        formatear_timedelta=formatear_timedelta,
    )
    return render_pdf(HTML(string=html))
