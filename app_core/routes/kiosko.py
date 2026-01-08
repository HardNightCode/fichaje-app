from datetime import datetime, timedelta

from flask import flash, redirect, render_template, url_for, session, request, jsonify
from flask_login import current_user, login_required

from werkzeug.security import check_password_hash

from ..logic import obtener_horario_aplicable, obtener_ubicaciones_usuario, usuario_tiene_flexible
from ..models import Kiosk, KioskUser, Registro, User


def register_kiosko_routes(app):
    @app.route("/kiosko", methods=["GET"])
    @login_required
    def kiosko_panel():
        """
        Panel de fichaje en modo kiosko.
        Solo accesible para usuarios con rol 'kiosko'.
        """
        if current_user.role != "kiosko":
            flash("Esta sección es solo para cuentas de kiosko.", "error")
            return redirect(url_for("index"))

        kiosk = (
            Kiosk.query
            .filter_by(kiosk_account_id=current_user.id)
            .first()
        )
        if not kiosk:
            flash("Esta cuenta de kiosko no está asociada a ningún kiosko.", "error")
            return redirect(url_for("index"))

        kiosk_users = list(kiosk.kiosk_users)
        last_user_id = session.get("kiosk_last_user_id")
        hoy = datetime.now().date()

        kiosk_cards = []
        for ku in kiosk_users:
            u = ku.user
            ubicaciones_usuario = obtener_ubicaciones_usuario(u)
            tiene_ubicaciones = len(ubicaciones_usuario) > 0
            tiene_flexible = usuario_tiene_flexible(u)

            ultimo_trabajo = (
                Registro.query.filter(
                    Registro.usuario_id == u.id,
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

            schedule = obtener_horario_aplicable(u, hoy)
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
                    Registro.usuario_id == u.id,
                    Registro.accion == "entrada",
                )
                .order_by(Registro.momento.desc())
                .first()
            )
            ultimo_salida = (
                Registro.query.filter(
                    Registro.usuario_id == u.id,
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
                    Registro.usuario_id == u.id,
                    Registro.accion == "descanso_inicio",
                )
                .order_by(Registro.momento.desc())
                .first()
            )
            ultimo_descanso_fin = (
                Registro.query.filter(
                    Registro.usuario_id == u.id,
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

            bloquear_descanso = not entrada_abierta or not descanso_es_flexible

            kiosk_cards.append({
                "user": u,
                "tiene_ubicaciones": tiene_ubicaciones,
                "tiene_flexible": tiene_flexible,
                "bloquear_entrada": bloquear_entrada,
                "bloquear_salida": bloquear_salida,
                "tiene_descanso": tiene_descanso,
                "descanso_es_flexible": descanso_es_flexible,
                "descanso_en_curso": descanso_en_curso,
                "bloquear_descanso": bloquear_descanso,
            })

        return render_template(
            "kiosko_panel.html",
            kiosk=kiosk,
            kiosk_users=kiosk_users,
            kiosk_cards=kiosk_cards,
            last_user_id=last_user_id,
        )

    @app.route("/kiosko/validar_pin", methods=["POST"])
    @login_required
    def kiosko_validar_pin():
        if current_user.role != "kiosko":
            return jsonify({"ok": False, "message": "No autorizado."}), 403

        data = request.get_json(silent=True) or request.form
        usuario_id_str = (data.get("usuario_id") or "").strip()
        pin = (data.get("pin") or "").strip()

        if not usuario_id_str or not pin:
            return jsonify({"ok": False, "message": "Usuario o PIN incompletos."}), 400

        try:
            usuario_id = int(usuario_id_str)
        except ValueError:
            return jsonify({"ok": False, "message": "Usuario no válido."}), 400

        usuario = User.query.get(usuario_id)
        if not usuario:
            return jsonify({"ok": False, "message": "Usuario no encontrado."}), 404

        kiosk = (
            Kiosk.query
            .filter_by(kiosk_account_id=current_user.id)
            .first()
        )
        if not kiosk:
            return jsonify({"ok": False, "message": "Kiosko no asociado."}), 400

        ku = (
            KioskUser.query
            .filter_by(kiosk_id=kiosk.id, user_id=usuario.id)
            .first()
        )
        if not ku:
            return jsonify({"ok": False, "message": "Usuario no autorizado."}), 403

        if not check_password_hash(ku.pin_hash, pin):
            return jsonify({"ok": False, "message": "PIN incorrecto."}), 401

        return jsonify({"ok": True})
