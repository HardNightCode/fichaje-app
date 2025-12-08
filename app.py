from datetime import datetime, timedelta, time
from functools import wraps
from pathlib import Path
import os
import logging
import csv
from io import StringIO
from logging.handlers import RotatingFileHandler

from flask import Flask, Response, render_template, request, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy
from flask_weasyprint import HTML, render_pdf
from flask_login import (
    LoginManager,
    UserMixin,
    login_user,
    login_required,
    logout_user,
    current_user,
)
from werkzeug.security import generate_password_hash, check_password_hash

from geo_utils import is_within_radius
from services_fichaje import (
    validar_secuencia_fichaje,
    formatear_timedelta,
)

from typing import Optional
from collections import defaultdict
from types import SimpleNamespace

# ======================================================
# Configuración básica de Flask
# ======================================================

app = Flask(__name__)

# SECRET_KEY configurable por entorno (para cada instancia).
app.config["SECRET_KEY"] = os.getenv(
    "SECRET_KEY",
    "cambia-esta-clave-por-una-mas-segura",
)

# === Configuración de base de datos ===
# 1) Ruta por defecto: instance/fichaje.db (sqlite local)
BASE_DIR = Path(__file__).resolve().parent
instance_dir = BASE_DIR / "instance"
instance_dir.mkdir(exist_ok=True)
default_sqlite_path = instance_dir / "fichaje.db"
default_sqlite_uri = f"sqlite:///{default_sqlite_path}"

# 2) Si hay DATABASE_URL en el entorno (caso instancias con PostgreSQL), la usamos.
#    Esto es lo que el panel escribe en el .env de cada /home/<instancia>/app
app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv(
    "DATABASE_URL",
    default_sqlite_uri,
)

app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

# ======================================================
# Flask-Login
# ======================================================

login_manager = LoginManager()
login_manager.login_view = "login"
login_manager.init_app(app)


# ======================================================
# Logging a fichero en producción
# ======================================================

if not app.debug:
    log_dir = os.path.join(app.root_path, "logs")
    os.makedirs(log_dir, exist_ok=True)
    file_handler = RotatingFileHandler(
        os.path.join(log_dir, "app.log"), maxBytes=1_000_000, backupCount=5
    )
    file_handler.setLevel(logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s"
    )
    file_handler.setFormatter(formatter)
    app.logger.addHandler(file_handler)
    app.logger.setLevel(logging.INFO)


# ======================================================
# Modelos
# ======================================================

class User(UserMixin, db.Model):
    __tablename__ = "user"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), default="empleado")  # 'admin' o 'empleado'

    # Relación antigua (ubicación única)
    location_id = db.Column(db.Integer, db.ForeignKey("location.id"), nullable=True)
    location = db.relationship("Location", backref=db.backref("users_single", lazy=True))

    # NUEVO: relación muchos-a-muchos (ubicaciones múltiples)
    locations_multi = db.relationship(
        "Location",
        secondary="user_location",
        backref=db.backref("users_multi", lazy="dynamic"),
    )

    # NUEVO: relación muchos-a-muchos con horarios
    schedules = db.relationship(
        "Schedule",
        secondary="user_schedule",
        backref=db.backref("users", lazy="dynamic"),
    )

    def set_password(self, password: str):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)


class Location(db.Model):
    __tablename__ = "location"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    latitude = db.Column(db.Float, nullable=False)
    longitude = db.Column(db.Float, nullable=False)
    radius_meters = db.Column(db.Float, nullable=False, default=100.0)


class Registro(db.Model):
    __tablename__ = "registro"

    id = db.Column(db.Integer, primary_key=True)
    usuario_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    accion = db.Column(db.String(20), nullable=False)  # 'entrada' o 'salida'
    momento = db.Column(db.DateTime, default=datetime.utcnow)

    # Coordenadas en el momento del fichaje
    latitude = db.Column(db.Float, nullable=True)
    longitude = db.Column(db.Float, nullable=True)

    usuario = db.relationship("User", backref=db.backref("registros", lazy=True))
   
    # NUEVO: historial de ediciones (ordenado de más reciente a más antigua)
    ediciones = db.relationship(
        "RegistroEdicion",
        backref="registro",
        lazy="dynamic",
        order_by="RegistroEdicion.edit_time.desc()",
        cascade="all, delete-orphan",
    )

class RegistroEdicion(db.Model):
    __tablename__ = "registro_edicion"

    id = db.Column(db.Integer, primary_key=True)
    registro_id = db.Column(db.Integer, db.ForeignKey("registro.id"), nullable=False)
    editor_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)

    edit_time = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    editor_ip = db.Column(db.String(45))  # IPv4/IPv6

    # Valores antiguos (antes de la edición)
    old_accion = db.Column(db.String(20))
    old_momento = db.Column(db.DateTime)
    old_latitude = db.Column(db.Float)
    old_longitude = db.Column(db.Float)

    # Relación con el usuario que edita
    editor = db.relationship("User", backref=db.backref("registros_editados", lazy=True))

# NUEVO: tabla intermedia usuario <-> ubicación
class UserLocation(db.Model):
    __tablename__ = "user_location"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    location_id = db.Column(db.Integer, db.ForeignKey("location.id"), nullable=False)

class Schedule(db.Model):
    __tablename__ = "schedule"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)

    # Horario "global" (modo simple)
    start_time = db.Column(db.Time, nullable=True)  # puede ser null si se usa modo por días
    end_time = db.Column(db.Time, nullable=True)

    # Descanso global: 'none' | 'fixed' | 'flexible'
    break_type = db.Column(db.String(20), nullable=False, default="none")
    break_start = db.Column(db.Time, nullable=True)
    break_end = db.Column(db.Time, nullable=True)
    break_minutes = db.Column(db.Integer, nullable=True)

    # NUEVO: ¿usa configuración por días?
    use_per_day = db.Column(db.Boolean, default=False, nullable=False)

    # NUEVO: días asociados (0=lunes ... 6=domingo)
    days = db.relationship(
        "ScheduleDay",
        backref="schedule",
        cascade="all, delete-orphan",
        lazy="select",
    )

class ScheduleDay(db.Model):
    __tablename__ = "schedule_day"

    id = db.Column(db.Integer, primary_key=True)
    schedule_id = db.Column(db.Integer, db.ForeignKey("schedule.id"), nullable=False)

    # 0 = lunes, 6 = domingo
    day_of_week = db.Column(db.Integer, nullable=False)

    start_time = db.Column(db.Time, nullable=False)
    end_time = db.Column(db.Time, nullable=False)

    break_type = db.Column(db.String(20), nullable=False, default="none")
    break_start = db.Column(db.Time, nullable=True)
    break_end = db.Column(db.Time, nullable=True)
    break_minutes = db.Column(db.Integer, nullable=True)

class UserSchedule(db.Model):
    """
    Tabla intermedia usuario <-> horario.
    De momento no añadimos más campos; si un día quieres marcar "principal",
    se puede añadir un boolean aquí.
    """
    __tablename__ = "user_schedule"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    schedule_id = db.Column(db.Integer, db.ForeignKey("schedule.id"), nullable=False)


class UserScheduleSettings(db.Model):
    """
    Configuración de horario por usuario:
      - enforce_schedule: si se le fuerza a trabajar dentro de su horario
      - margin_minutes: margen (antes / después) permitidos
      - detect_schedule: si se intenta detectar automáticamente el horario
        en base a la hora de fichaje (cuando tenga varios horarios asignados).
    """
    __tablename__ = "user_schedule_settings"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), unique=True, nullable=False)

    enforce_schedule = db.Column(db.Boolean, default=False)
    margin_minutes = db.Column(db.Integer, default=0)
    detect_schedule = db.Column(db.Boolean, default=False)

    user = db.relationship(
        "User",
        backref=db.backref("schedule_settings", uselist=False),
    )

# ======================================================
# Carga de usuario para Flask-Login
# ======================================================

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


def admin_required(view_func):
    @wraps(view_func)
    @login_required
    def wrapped_view(*args, **kwargs):
        if current_user.role != "admin":
            flash("No tienes permisos para acceder a esta sección.", "error")
            return redirect(url_for("index"))
        return view_func(*args, **kwargs)

    return wrapped_view


# ======================================================
# Inicialización de la BD
# ======================================================

def crear_tablas():
    db.create_all()

    # Si no hay ningún usuario, creamos uno admin de ejemplo
    if User.query.count() == 0:
        admin = User(username="admin", role="admin")
        admin.set_password("admin123")  # cámbialo después
        db.session.add(admin)
        db.session.commit()

    # --- Gestión robusta de la ubicación "Flexible" ---
    flexibles = Location.query.filter_by(name="Flexible").all()

    if not flexibles:
        # No existe ninguna -> la creamos
        flexible = Location(
            name="Flexible",
            latitude=0.0,
            longitude=0.0,
            radius_meters=0.0,  # no se usa para radio en modo flexible
        )
        db.session.add(flexible)
        db.session.commit()

    elif len(flexibles) > 1:
        # Hay duplicadas -> nos quedamos con la primera y fusionamos relaciones
        principal = flexibles[0]
        sobrantes = flexibles[1:]

        # Caso 1: esquema nuevo MANY-TO-MANY (User.locations_multi, backref="users_multi")
        if hasattr(Location, "users_multi"):
            for extra in sobrantes:
                for u in list(extra.users_multi):
                    if principal not in u.locations_multi:
                        u.locations_multi.append(principal)
                db.session.delete(extra)

        # Caso 2: esquema antiguo ONE-TO-MANY (User.location, backref="users_single")
        elif hasattr(Location, "users_single"):
            for extra in sobrantes:
                for u in list(extra.users_single):
                    u.location_id = principal.id
                db.session.delete(extra)

        else:
            for extra in sobrantes:
                db.session.delete(extra)

        db.session.commit()
    # Si hay exactamente una "Flexible", no hacemos nada más

def init_app():
    """
    Se ejecuta al importar el módulo (gunicorn, etc.).
    Crea tablas y usuario admin si la BD está vacía.
    """
    with app.app_context():
        crear_tablas()


# ======================================================
# Rutas
# ======================================================

@app.route("/admin/generar_informe", methods=["POST"])
@login_required
def generar_informe():
    # Recibir datos del formulario
    usuario_id = request.form.get("usuario_id")
    fecha_desde = request.form.get("fecha_desde")
    fecha_hasta = request.form.get("fecha_hasta")

    # Convertir las fechas a objetos datetime
    try:
        fecha_desde = datetime.strptime(fecha_desde, "%Y-%m-%d")
        fecha_hasta = datetime.strptime(fecha_hasta, "%Y-%m-%d") + timedelta(days=1)  # Incluye todo el día hasta las 23:59:59
    except ValueError:
        flash("Las fechas no son válidas.", "error")
        return redirect(url_for("admin_registros"))

    # Filtrar registros por usuario y fechas
    query = Registro.query.filter(Registro.momento >= fecha_desde, Registro.momento <= fecha_hasta)

    if usuario_id != "all":
        query = query.filter(Registro.usuario_id == int(usuario_id))

    registros = query.all()

    # Si no se encontraron registros, mostramos un mensaje de error
    if not registros:
        flash("No se encontraron registros para este periodo y usuario.", "error")
        return redirect(url_for("admin_registros"))

    # Preparar los datos del informe
    resumen_horas = calcular_horas_trabajadas(registros)

    # Generar el PDF con los datos
    try:
        html = render_template("informe_pdf.html", registros=registros, resumen_horas=resumen_horas, tipo_periodo="rango")
        return render_pdf(HTML(string=html))
    except Exception as e:
        app.logger.error(f"Error al generar el PDF: {e}")
        flash("Hubo un problema generando el informe PDF.", "error")
        return redirect(url_for("admin_registros"))

@app.route("/")
@login_required
def index():
    # Registros del usuario actual (para tabla y resumen)
    registros_usuario = (
        Registro.query.filter_by(usuario_id=current_user.id)
        .order_by(Registro.momento.asc())
        .all()
    )

    # Intervalos Entrada/Salida SOLO del usuario actual
    intervalos_usuario = agrupar_registros_en_intervalos(registros_usuario)

    # --- Anotar descanso por intervalo ---
    for it in intervalos_usuario:
        if it.usuario and it.entrada_momento:
            descanso_td, en_curso, inicio = calcular_descanso_intervalo_para_usuario(
                it.usuario.id,
                it.entrada_momento,
                it.salida_momento,
            )
        else:
            descanso_td, en_curso, inicio = timedelta(0), False, None

        it.descanso_td = descanso_td
        it.descanso_en_curso = en_curso
        it.descanso_inicio_iso = inicio.isoformat() if inicio else ""

        if en_curso:
            it.descanso_label = "Descansando"
        elif descanso_td.total_seconds() > 0:
            it.descanso_label = formatear_timedelta(descanso_td)
        else:
            it.descanso_label = "Sin descanso"

    # Resumen: total de horas del usuario actual (mismo criterio que en los intervalos)
    total_trabajo = timedelta(0)
    for it in intervalos_usuario:
        extra_td, defecto_td = calcular_extra_y_defecto_intervalo(it)
        it.horas_extra = extra_td
        it.horas_defecto = defecto_td
        trabajo_real = getattr(it, "trabajo_real", timedelta(0))
        if trabajo_real.total_seconds() > 0:
            total_trabajo += trabajo_real

    resumen_horas = formatear_timedelta(total_trabajo)

    # Ubicaciones múltiples del usuario (para el formulario y mensajes)
    ubicaciones_usuario = obtener_ubicaciones_usuario(current_user)
    tiene_ubicaciones = len(ubicaciones_usuario) > 0
    tiene_flexible = usuario_tiene_flexible(current_user)

    # --- Última ENTRADA/SALIDA (ignorando descansos) ---
    ultimo_trabajo = (
        Registro.query.filter(
            Registro.usuario_id == current_user.id,
            Registro.accion.in_(["entrada", "salida"]),
        )
        .order_by(Registro.momento.desc())
        .first()
    )

    if ultimo_trabajo is None:
        # No hay fichajes de trabajo aún
        bloquear_entrada = False
        bloquear_salida = True
    else:
        if ultimo_trabajo.accion == "entrada":
            # Hay entrada abierta -> solo se permite SALIDA
            bloquear_entrada = True
            bloquear_salida = False
        else:
            # Último fue salida -> toca ENTRADA
            bloquear_entrada = False
            bloquear_salida = True

    # --- ¿Tiene descanso configurado hoy según su horario? ---
    hoy = datetime.now().date()
    schedule = obtener_horario_aplicable(current_user, hoy)
    tiene_descanso = False

    if schedule:
        if schedule.use_per_day:
            dow = hoy.weekday()
            dia = next((d for d in schedule.days if d.day_of_week == dow), None)
            if dia and dia.break_type in ("fixed", "flexible"):
                tiene_descanso = True
        else:
            if schedule.break_type in ("fixed", "flexible"):
                tiene_descanso = True

    # --- ¿Hay entrada abierta? (para habilitar descanso) ---
    ultimo_entrada = (
        Registro.query.filter(
            Registro.usuario_id == current_user.id,
            Registro.accion == "entrada",
        )
        .order_by(Registro.momento.desc())
        .first()
    )
    ultimo_salida = (
        Registro.query.filter(
            Registro.usuario_id == current_user.id,
            Registro.accion == "salida",
        )
        .order_by(Registro.momento.desc())
        .first()
    )

    entrada_abierta = False
    if ultimo_entrada:
        # Entrada abierta si no hay salida posterior
        if not ultimo_salida or ultimo_entrada.momento > ultimo_salida.momento:
            entrada_abierta = True

    # --- ¿Hay descanso en curso? (global, para el botón) ---
    ultimo_descanso_inicio = (
        Registro.query.filter(
            Registro.usuario_id == current_user.id,
            Registro.accion == "descanso_inicio",
        )
        .order_by(Registro.momento.desc())
        .first()
    )
    ultimo_descanso_fin = (
        Registro.query.filter(
            Registro.usuario_id == current_user.id,
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

    # --- Bloqueo del botón de descanso ---
    if not entrada_abierta:
        bloquear_descanso = True
    else:
        bloquear_descanso = False

    return render_template(
        "index.html",
        intervalos_usuario=intervalos_usuario,
        resumen_horas=resumen_horas,
        ubicaciones_usuario=ubicaciones_usuario,
        tiene_ubicaciones=tiene_ubicaciones,
        tiene_flexible=tiene_flexible,
        bloquear_entrada=bloquear_entrada,
        bloquear_salida=bloquear_salida,
        tiene_descanso=tiene_descanso,
        descanso_en_curso=descanso_en_curso,
        bloquear_descanso=bloquear_descanso,
    )

@app.route("/admin/ubicaciones", methods=["GET", "POST"])
@admin_required
def admin_ubicaciones():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        lat = request.form.get("latitude", "").strip()
        lon = request.form.get("longitude", "").strip()
        radius = request.form.get("radius_meters", "").strip()

        if not name or not lat or not lon or not radius:
            flash("Todos los campos son obligatorios.", "error")
            return redirect(url_for("admin_ubicaciones"))

        # Impedimos crear manualmente otra ubicación llamada "Flexible"
        if name.lower() == "flexible":
            flash("La ubicación 'Flexible' es gestionada por el sistema y no puede crearse ni modificarse desde aquí.", "error")
            return redirect(url_for("admin_ubicaciones"))

        # Aceptar coma o punto
        lat = lat.replace(",", ".")
        lon = lon.replace(",", ".")
        radius = radius.replace(",", ".")

        try:
            lat_f = float(lat)
            lon_f = float(lon)
            radius_f = float(radius)
        except ValueError:
            flash("Latitud, longitud y radio deben ser numéricos.", "error")
            return redirect(url_for("admin_ubicaciones"))

        loc = Location(
            name=name,
            latitude=lat_f,
            longitude=lon_f,
            radius_meters=radius_f,
        )
        db.session.add(loc)
        db.session.commit()
        flash("Ubicación creada correctamente.", "success")
        return redirect(url_for("admin_ubicaciones"))

    # No mostramos la ubicación especial "Flexible" en el listado
    ubicaciones = (
        Location.query
        .filter(Location.name != "Flexible")
        .order_by(Location.name)
        .all()
    )
    return render_template("admin_ubicaciones.html", ubicaciones=ubicaciones)

@app.route("/admin/ubicaciones/<int:loc_id>/editar", methods=["GET", "POST"])
@admin_required
def editar_ubicacion(loc_id):
    loc = Location.query.get_or_404(loc_id)

    # La ubicación "Flexible" es especial: no se puede editar
    if (loc.name or "").lower() == "flexible":
        flash("La ubicación 'Flexible' es especial del sistema y no puede editarse.", "error")
        return redirect(url_for("admin_ubicaciones"))

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        lat = request.form.get("latitude", "").strip()
        lon = request.form.get("longitude", "").strip()
        radius = request.form.get("radius_meters", "").strip()

        if not name or not lat or not lon or not radius:
            flash("Todos los campos son obligatorios.", "error")
            return redirect(url_for("editar_ubicacion", loc_id=loc.id))

        # Impedimos crear manualmente otra ubicación llamada "Flexible"
        if name.lower() == "flexible":
            flash("La ubicación 'Flexible' es gestionada por el sistema y no puede crearse ni modificarse desde aquí.", "error")
            return redirect(url_for("admin_ubicaciones"))

        lat = lat.replace(",", ".")
        lon = lon.replace(",", ".")
        radius = radius.replace(",", ".")

        try:
            loc.latitude = float(lat)
            loc.longitude = float(lon)
            loc.radius_meters = float(radius)
        except ValueError:
            flash("Latitud, longitud y radio deben ser numéricos.", "error")
            return redirect(url_for("editar_ubicacion", loc_id=loc.id))

        loc.name = name
        db.session.commit()
        flash("Ubicación actualizada correctamente.", "success")
        return redirect(url_for("admin_ubicaciones"))

    return render_template("admin_ubicacion_editar.html", loc=loc)


@app.route("/admin/ubicaciones/<int:loc_id>/eliminar", methods=["POST"])
@admin_required
def eliminar_ubicacion(loc_id):
    loc = Location.query.get_or_404(loc_id)

    # La ubicación "Flexible" es especial: no se puede eliminar
    if (loc.name or "").lower() == "flexible":
        flash("La ubicación 'Flexible' es especial del sistema y no puede eliminarse.", "error")
        return redirect(url_for("admin_ubicaciones"))

    # Comprobar si hay usuarios usando esta ubicación
    usuarios_con_loc = User.query.filter_by(location_id=loc.id).first()
    if usuarios_con_loc:
        flash(
            "No se puede eliminar la ubicación porque está asignada a uno o más usuarios.",
            "error",
        )
        return redirect(url_for("admin_ubicaciones"))

    db.session.delete(loc)
    db.session.commit()
    flash("Ubicación eliminada correctamente.", "success")
    return redirect(url_for("admin_ubicaciones"))


@app.route("/admin/usuarios", methods=["GET", "POST"])
@admin_required
def admin_usuarios():
    usuarios = User.query.order_by(User.username).all()
    ubicaciones = Location.query.order_by(Location.name).all()
    flexible = Location.query.filter_by(name="Flexible").first()
    flexible_location_id = flexible.id if flexible else None

    if request.method == "POST":
        for user in usuarios:
            # Nombre del campo por usuario: locations_<id>[]
            field_name = f"locations_{user.id}[]"
            valores = request.form.getlist(field_name)

            # Limpiamos todas las asociaciones antiguas (multi)
            user.locations_multi.clear()
            # Y también la ubicación única antigua (ya no se gestiona desde aquí)
            user.location_id = None

            for v in valores:
                # Opción "Borrar" o vacío: no se añade nada
                if not v or v == "borrar":
                    continue
                try:
                    loc_id = int(v)
                except ValueError:
                    continue

                loc = Location.query.get(loc_id)
                if loc and loc not in user.locations_multi:
                    user.locations_multi.append(loc)

        db.session.commit()
        flash("Ubicaciones de usuarios actualizadas.", "success")
        return redirect(url_for("admin_usuarios"))

    # Mapa usuario -> lista de ubicaciones asignadas (para pintar el formulario)
    user_locations_map = {}
    for user in usuarios:
        if user.locations_multi:
            user_locations_map[user.id] = list(user.locations_multi)
        elif user.location is not None:
            # Compatibilidad: si solo tenía la ubicación antigua
            user_locations_map[user.id] = [user.location]
        else:
            user_locations_map[user.id] = []

    return render_template(
        "admin_usuarios.html",
        usuarios=usuarios,
        ubicaciones=ubicaciones,
        flexible_location_id=flexible_location_id,
        user_locations_map=user_locations_map,
    )

@app.route("/admin/horarios", methods=["GET", "POST"])
@admin_required
def admin_horarios():
    """
    Página para crear y listar horarios.
    - Puede crear:
        * Horario simple (mismas horas todos los días).
        * Horario por días (cada día con su inicio/fin/descanso).
    """
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        use_per_day = bool(request.form.get("use_per_day"))

        if not name:
            flash("El nombre del horario es obligatorio.", "error")
            return redirect(url_for("admin_horarios"))

        # Valores por defecto
        start_time = None
        end_time = None
        break_type = "none"
        break_start = None
        break_end = None
        break_minutes = None

        # --------- MODO SIMPLE (no por días) ----------
        if not use_per_day:
            start_time_str = request.form.get("start_time", "").strip()
            end_time_str = request.form.get("end_time", "").strip()
            break_type = request.form.get("break_type", "none")

            break_start_str = request.form.get("break_start", "").strip()
            break_end_str = request.form.get("break_end", "").strip()
            break_minutes_str = request.form.get("break_minutes", "").strip()

            if not start_time_str or not end_time_str:
                flash("Inicio y fin de jornada son obligatorios en modo simple.", "error")
                return redirect(url_for("admin_horarios"))

            try:
                start_time = datetime.strptime(start_time_str, "%H:%M").time()
                end_time = datetime.strptime(end_time_str, "%H:%M").time()
            except ValueError:
                flash("Las horas de inicio y fin deben tener formato HH:MM.", "error")
                return redirect(url_for("admin_horarios"))

            if break_type == "fixed":
                if not break_start_str or not break_end_str:
                    flash("Para descanso fijo debes indicar inicio y fin de descanso.", "error")
                    return redirect(url_for("admin_horarios"))
                try:
                    break_start = datetime.strptime(break_start_str, "%H:%M").time()
                    break_end = datetime.strptime(break_end_str, "%H:%M").time()
                except ValueError:
                    flash("Las horas de descanso deben tener formato HH:MM.", "error")
                    return redirect(url_for("admin_horarios"))
            elif break_type == "flexible":
                if not break_minutes_str:
                    flash("Para descanso flexible debes indicar los minutos de descanso.", "error")
                    return redirect(url_for("admin_horarios"))
                try:
                    break_minutes = int(break_minutes_str)
                except ValueError:
                    flash("Los minutos de descanso deben ser numéricos.", "error")
                    return redirect(url_for("admin_horarios"))

        # --- Compatibilidad con BD: columnas NOT NULL en modo por días ---
        if use_per_day:
            # Estos valores NO se usan realmente (el horario real está en ScheduleDay),
            # pero PostgreSQL exige que no sean NULL (NOT NULL en start_time/end_time).
            start_time = time(0, 0)
            end_time = time(23, 59)
            break_type = "none"
            break_start = None
            break_end = None
            break_minutes = None

        # Creamos el Schedule (sin días aún)
        horario = Schedule(
            name=name,
            start_time=start_time,
            end_time=end_time,
            break_type=break_type,
            break_start=break_start,
            break_end=break_end,
            break_minutes=break_minutes,
            use_per_day=use_per_day,
        )
        db.session.add(horario)
        db.session.flush()  # para tener horario.id

        # --------- MODO POR DÍAS ----------
        if use_per_day:
            dias = [
                ("mon", 0),
                ("tue", 1),
                ("wed", 2),
                ("thu", 3),
                ("fri", 4),
                ("sat", 5),
                ("sun", 6),
            ]

            tiene_algun_dia = False

            for prefix, dow in dias:
                s_str = request.form.get(f"{prefix}_start", "").strip()
                e_str = request.form.get(f"{prefix}_end", "").strip()
                if not s_str or not e_str:
                    # Día sin horario -> se interpreta como NO laborable
                    continue

                try:
                    s_time = datetime.strptime(s_str, "%H:%M").time()
                    e_time = datetime.strptime(e_str, "%H:%M").time()
                except ValueError:
                    flash(f"Hora inválida en el día {prefix.upper()} (formato HH:MM).", "error")
                    db.session.rollback()
                    return redirect(url_for("admin_horarios"))

                b_type = request.form.get(f"{prefix}_break_type", "none")
                bs = be = None
                bmin = None

                if b_type == "fixed":
                    bs_str = request.form.get(f"{prefix}_break_start", "").strip()
                    be_str = request.form.get(f"{prefix}_break_end", "").strip()
                    if not bs_str or not be_str:
                        flash("Para descanso fijo debes indicar inicio y fin de descanso en cada día.", "error")
                        db.session.rollback()
                        return redirect(url_for("admin_horarios"))
                    try:
                        bs = datetime.strptime(bs_str, "%H:%M").time()
                        be = datetime.strptime(be_str, "%H:%M").time()
                    except ValueError:
                        flash("Las horas de descanso diario deben tener formato HH:MM.", "error")
                        db.session.rollback()
                        return redirect(url_for("admin_horarios"))
                elif b_type == "flexible":
                    bmin_str = request.form.get(f"{prefix}_break_minutes", "").strip()
                    if not bmin_str:
                        flash("Para descanso flexible debes indicar los minutos de descanso en cada día.", "error")
                        db.session.rollback()
                        return redirect(url_for("admin_horarios"))
                    try:
                        bmin = int(bmin_str)
                    except ValueError:
                        flash("Los minutos de descanso diario deben ser numéricos.", "error")
                        db.session.rollback()
                        return redirect(url_for("admin_horarios"))

                dia_obj = ScheduleDay(
                    schedule_id=horario.id,
                    day_of_week=dow,
                    start_time=s_time,
                    end_time=e_time,
                    break_type=b_type,
                    break_start=bs,
                    break_end=be,
                    break_minutes=bmin,
                )
                db.session.add(dia_obj)
                tiene_algun_dia = True

            if not tiene_algun_dia:
                flash("En modo por días, al menos un día debe tener horario.", "error")
                db.session.rollback()
                return redirect(url_for("admin_horarios"))

        try:
            db.session.commit()
            flash("Horario creado correctamente.", "success")
        except Exception as e:
            app.logger.error(f"Error al crear horario: {e}")
            db.session.rollback()
            flash("Se ha producido un error al crear el horario.", "error")

        return redirect(url_for("admin_horarios"))

    # GET: listar todos los horarios
    horarios = Schedule.query.order_by(Schedule.name).all()
    return render_template("admin_horarios.html", horarios=horarios)

@app.route("/admin/horarios/<int:schedule_id>/eliminar", methods=["POST"])
@admin_required
def eliminar_horario(schedule_id):
    """
    Elimina un horario, siempre que no esté asignado a ningún usuario.
    """
    horario = Schedule.query.get_or_404(schedule_id)

    # Si el backref es dinámico, horario.users es un Query
    try:
        asignados = horario.users.count()
    except Exception:
        asignados = len(horario.users or [])

    if asignados > 0:
        flash(
            "No se puede eliminar el horario porque está asignado a uno o más usuarios.",
            "error",
        )
        return redirect(url_for("admin_horarios"))

    # Limpiamos también la tabla intermedia explícita, por si acaso
    UserSchedule.query.filter_by(schedule_id=schedule_id).delete(
        synchronize_session=False
    )

    db.session.delete(horario)
    db.session.commit()
    flash("Horario eliminado correctamente.", "success")
    return redirect(url_for("admin_horarios"))

@app.route("/admin/horarios/<int:schedule_id>/editar", methods=["GET", "POST"])
@admin_required
def editar_horario(schedule_id):
    horario = Schedule.query.get_or_404(schedule_id)

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        use_per_day = bool(request.form.get("use_per_day"))

        if not name:
            flash("El nombre del horario es obligatorio.", "error")
            return redirect(url_for("editar_horario", schedule_id=horario.id))

        horario.name = name
        horario.use_per_day = use_per_day

        # --------- MODO SIMPLE ----------
        if not use_per_day:
            start_time_str = request.form.get("start_time", "").strip()
            end_time_str = request.form.get("end_time", "").strip()
            break_type = request.form.get("break_type", "none")

            break_start_str = request.form.get("break_start", "").strip()
            break_end_str = request.form.get("break_end", "").strip()
            break_minutes_str = request.form.get("break_minutes", "").strip()

            if not start_time_str or not end_time_str:
                flash("Inicio y fin de jornada son obligatorios en modo simple.", "error")
                return redirect(url_for("editar_horario", schedule_id=horario.id))

            try:
                horario.start_time = datetime.strptime(start_time_str, "%H:%M").time()
                horario.end_time = datetime.strptime(end_time_str, "%H:%M").time()
            except ValueError:
                flash("Las horas de inicio y fin deben tener formato HH:MM.", "error")
                return redirect(url_for("editar_horario", schedule_id=horario.id))

            horario.break_type = break_type
            horario.break_start = None
            horario.break_end = None
            horario.break_minutes = None

            if break_type == "fixed":
                if not break_start_str or not break_end_str:
                    flash("Para descanso fijo debes indicar inicio y fin de descanso.", "error")
                    return redirect(url_for("editar_horario", schedule_id=horario.id))
                try:
                    horario.break_start = datetime.strptime(break_start_str, "%H:%M").time()
                    horario.break_end = datetime.strptime(break_end_str, "%H:%M").time()
                except ValueError:
                    flash("Las horas de descanso deben tener formato HH:MM.", "error")
                    return redirect(url_for("editar_horario", schedule_id=horario.id))
            elif break_type == "flexible":
                if not break_minutes_str:
                    flash("Para descanso flexible debes indicar los minutos de descanso.", "error")
                    return redirect(url_for("editar_horario", schedule_id=horario.id))
                try:
                    horario.break_minutes = int(break_minutes_str)
                except ValueError:
                    flash("Los minutos de descanso deben ser numéricos.", "error")
                    return redirect(url_for("editar_horario", schedule_id=horario.id))

            # En modo simple, limpiamos cualquier configuración por días
            horario.days.clear()

        # --------- MODO POR DÍAS ----------
        else:
            # En modo por días, el horario global no se usa realmente,
            # pero la BD exige NOT NULL en start_time/end_time.
            horario.start_time = time(0, 0)
            horario.end_time = time(23, 59)
            horario.break_type = "none"
            horario.break_start = None
            horario.break_end = None
            horario.break_minutes = None

            # Limpiamos días actuales antes de recrearlos
            horario.days.clear()

            dias = [
                ("mon", 0),
                ("tue", 1),
                ("wed", 2),
                ("thu", 3),
                ("fri", 4),
                ("sat", 5),
                ("sun", 6),
            ]

            tiene_algun_dia = False

            for prefix, dow in dias:
                s_str = request.form.get(f"{prefix}_start", "").strip()
                e_str = request.form.get(f"{prefix}_end", "").strip()
                if not s_str or not e_str:
                    continue

                try:
                    s_time = datetime.strptime(s_str, "%H:%M").time()
                    e_time = datetime.strptime(e_str, "%H:%M").time()
                except ValueError:
                    flash(f"Hora inválida en el día {prefix.upper()} (formato HH:MM).", "error")
                    return redirect(url_for("editar_horario", schedule_id=horario.id))

                b_type = request.form.get(f"{prefix}_break_type", "none")
                bs = be = None
                bmin = None

                if b_type == "fixed":
                    bs_str = request.form.get(f"{prefix}_break_start", "").strip()
                    be_str = request.form.get(f"{prefix}_break_end", "").strip()
                    if not bs_str or not be_str:
                        flash("Para descanso fijo debes indicar inicio y fin de descanso en cada día.", "error")
                        return redirect(url_for("editar_horario", schedule_id=horario.id))
                    try:
                        bs = datetime.strptime(bs_str, "%H:%M").time()
                        be = datetime.strptime(be_str, "%H:%M").time()
                    except ValueError:
                        flash("Las horas de descanso diario deben tener formato HH:MM.", "error")
                        return redirect(url_for("editar_horario", schedule_id=horario.id))
                elif b_type == "flexible":
                    bmin_str = request.form.get(f"{prefix}_break_minutes", "").strip()
                    if not bmin_str:
                        flash("Para descanso flexible debes indicar los minutos de descanso en cada día.", "error")
                        return redirect(url_for("editar_horario", schedule_id=horario.id))
                    try:
                        bmin = int(bmin_str)
                    except ValueError:
                        flash("Los minutos de descanso diario deben ser numéricos.", "error")
                        return redirect(url_for("editar_horario", schedule_id=horario.id))

                dia_obj = ScheduleDay(
                    schedule_id=horario.id,
                    day_of_week=dow,
                    start_time=s_time,
                    end_time=e_time,
                    break_type=b_type,
                    break_start=bs,
                    break_end=be,
                    break_minutes=bmin,
                )
                horario.days.append(dia_obj)
                tiene_algun_dia = True

            if not tiene_algun_dia:
                flash("En modo por días, al menos un día debe tener horario.", "error")
                return redirect(url_for("editar_horario", schedule_id=horario.id))

        db.session.commit()
        flash("Horario actualizado correctamente.", "success")
        return redirect(url_for("admin_horarios"))

    # GET: preparar mapa día_semana -> objeto ScheduleDay
    dias_map = {d.day_of_week: d for d in horario.days}
    return render_template("admin_horario_editar.html", horario=horario, dias_map=dias_map)

@app.route("/admin/usuarios/fichas")
@admin_required
def admin_usuarios_fichas():
    """
    Lista de usuarios, con enlace a su ficha de configuración
    (ubicaciones + horarios).
    """
    usuarios = User.query.order_by(User.username).all()
    return render_template("admin_usuarios_fichas.html", usuarios=usuarios)

@app.route("/admin/usuarios/<int:user_id>/ficha", methods=["GET", "POST"])
@admin_required
def admin_usuario_ficha(user_id):
    """
    Ficha individual de usuario:
      - Muestra ubicaciones asignadas (solo lectura, por ahora).
      - Permite asignar horarios.
      - Permite configurar:
          * Forzar horario (sí/no) + margen
          * Detectar horario automáticamente (sí/no)
    """
    user = User.query.get_or_404(user_id)
    horarios = Schedule.query.order_by(Schedule.name).all()
    settings = get_or_create_schedule_settings(user)

    if request.method == "POST":
        # Horarios seleccionados (pueden ser varios)
        schedule_ids = request.form.getlist("schedule_ids")

        # Limpia horarios anteriores
        user.schedules.clear()

        for sid in schedule_ids:
            try:
                sid_int = int(sid)
            except ValueError:
                continue
            h = Schedule.query.get(sid_int)
            if h and h not in user.schedules:
                user.schedules.append(h)

        # Configuración de horario
        enforce_value = request.form.get("enforce_schedule", "no")
        settings.enforce_schedule = (enforce_value == "si")

        margin_str = request.form.get("margin_minutes", "").strip() or "0"
        try:
            settings.margin_minutes = max(0, int(margin_str))
        except ValueError:
            settings.margin_minutes = 0

        settings.detect_schedule = (request.form.get("detect_schedule") == "on")

        db.session.commit()
        flash("Ficha de usuario actualizada correctamente.", "success")
        return redirect(url_for("admin_usuario_ficha", user_id=user.id))

    # GET: preparar datos
    ubicaciones_usuario = obtener_ubicaciones_usuario(user)
    horarios_usuario = list(user.schedules)

    return render_template(
        "admin_usuario_ficha.html",
        usuario=user,
        ubicaciones_usuario=ubicaciones_usuario,
        horarios=horarios,
        horarios_usuario=horarios_usuario,
        settings=settings,
    )

# ======================================================
# Helpers de ubicaciones (soporta esquema antiguo y nuevo)
# ======================================================

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

    Registros de descanso: accion in ('descanso_inicio', 'descanso_fin').
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
        # Valores por defecto
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
            # Caso raro: intervalo sin entrada, solo salida. Damos margen de 12h hacia atrás.
            inicio_ventana = it.salida_momento - timedelta(hours=12)
        else:
            # Sin entrada ni salida -> no tiene sentido calcular descanso
            continue

        if it.salida_momento:
            fin_ventana = it.salida_momento
        else:
            # Intervalo abierto (sin salida) -> hasta ahora
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

        # Si queda un descanso "abierto" dentro de la ventana:
        # - Siempre lo cerramos en fin_ventana.
        # - SOLO lo marcamos como "en curso" si NO hay salida en el intervalo.
        en_curso = False
        if ultimo_inicio is not None:
            fin = fin_ventana
            if fin < ultimo_inicio:
                fin += timedelta(days=1)
            total += (fin - ultimo_inicio)

            # Solo consideramos "descanso en curso" si el intervalo está abierto (sin salida)
            if it.salida_momento is None:
                en_curso = True

        it.descanso_total = total
        it.descanso_en_curso = en_curso

        if en_curso and total.total_seconds() > 0:
            it.descanso_label = f"Descansando ({formatear_timedelta(total)})"
        elif total.total_seconds() > 0:
            # Descanso terminado -> mostramos tiempo real
            it.descanso_label = formatear_timedelta(total)
        else:
            it.descanso_label = "Sin descanso"

def calcular_descanso_intervalo_para_usuario(usuario_id, entrada_momento, salida_momento=None):
    """
    Calcula el tiempo de descanso real dentro de un intervalo [entrada_momento, salida_momento]
    usando registros 'descanso_inicio' / 'descanso_fin' del usuario.

    Devuelve:
      - total_descanso: timedelta
      - descanso_en_curso: bool (True solo si hay descanso abierto Y el intervalo no tiene salida)
      - inicio_descanso_en_curso: datetime | None
    """
    if entrada_momento is None:
        return timedelta(0), False, None

    if salida_momento is None:
        limite_superior = datetime.utcnow()
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

    # Si queda un descanso "abierto":
    # - Si NO hay salida -> en curso hasta ahora.
    # - Si hay salida     -> cerrado en la salida, NO en curso.
    if inicio_actual is not None:
        if salida_momento is None:
            # Intervalo abierto: descanso realmente en curso
            ahora = datetime.utcnow()
            if ahora > inicio_actual:
                total += (ahora - inicio_actual)
            descanso_en_curso = True
            inicio_en_curso = inicio_actual
        else:
            # Intervalo cerrado: contamos hasta la salida, pero NO marcamos en curso
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
        .filter(Registro.usuario_id == user_id,
                Registro.accion == "salida",
                Registro.momento > ultima_entrada.momento)
        .first()
    )
    return salida_posterior is None

def calcular_extra_y_defecto_intervalo(it):
    """
    Calcula (horas_extra, horas_defecto) como timedeltas para un intervalo.

    Además deja calculado:
      - it.trabajo_real -> tiempo realmente trabajado en el intervalo
                           según horario + descansos (real/teórico).

    Reglas:
      - Duración total del intervalo: salida - entrada (corrigiendo medianoche).
      - Se descuenta un descanso "efectivo":
            descanso_efectivo = max(descanso_real, descanso_teorico)
        donde:
          * descanso_real    = tiempo fichado como descanso
          * descanso_teorico = el configurado en el horario (si lo hay)
      - Sin horario o día no laborable:
          * todo el trabajo neto (dur_real - descanso_real) se considera extra.
    """
    # Valor por defecto, por si algo va mal
    it.trabajo_real = timedelta(0)

    if not it.usuario:
        return timedelta(0), timedelta(0)

    dur_real = calcular_duracion_trabajada_intervalo(it)
    if dur_real is None:
        return timedelta(0), timedelta(0)

    user = it.usuario
    fecha = it.entrada_momento.date()

    # Descanso REAL fichado en ese intervalo
    descanso_real_td, _, _ = calcular_descanso_intervalo_para_usuario(
        user.id,
        it.entrada_momento,
        it.salida_momento,
    )

    # Horario aplicable (si lo hay)
    schedule = obtener_horario_aplicable(user, fecha)

    # ===== CASO 1: Sin horario asignado =====
    if schedule is None:
        trabajo_neto = dur_real - descanso_real_td
        if trabajo_neto.total_seconds() < 0:
            trabajo_neto = timedelta(0)
        it.trabajo_real = trabajo_neto
        return trabajo_neto, timedelta(0)

    # Jornada teórica (ya descuenta descanso teórico interno)
    dur_teorica = calcular_jornada_teorica(schedule, fecha)

    # ===== CASO 2: Día no laborable (jornada_teorica = 0) =====
    if dur_teorica.total_seconds() == 0:
        trabajo_neto = dur_real - descanso_real_td
        if trabajo_neto.total_seconds() < 0:
            trabajo_neto = timedelta(0)
        it.trabajo_real = trabajo_neto
        return trabajo_neto, timedelta(0)

    # ===== CASO 3: Día laborable con horario =====

    # Longitud bruta de la jornada según horario (sin restar descanso teórico)
    if schedule.use_per_day:
        dow = fecha.weekday()  # 0=lunes ... 6=domingo
        dia = next((d for d in schedule.days if d.day_of_week == dow), None)
        if dia is None:
            trabajo_neto = dur_real - descanso_real_td
            if trabajo_neto.total_seconds() < 0:
                trabajo_neto = timedelta(0)
            it.trabajo_real = trabajo_neto
            return trabajo_neto, timedelta(0)

        inicio_j = datetime.combine(fecha, dia.start_time)
        fin_j = datetime.combine(fecha, dia.end_time)
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

    longitud_bruta = fin_j - inicio_j  # L

    # Descanso teórico = L - jornada_teorica
    descanso_teorico_td = longitud_bruta - dur_teorica
    if descanso_teorico_td.total_seconds() < 0:
        descanso_teorico_td = timedelta(0)

    # Descanso efectivo: el mayor entre real y teórico
    descanso_efectivo = max(descanso_real_td, descanso_teorico_td)

    # Trabajo realmente computable
    trabajo_real = dur_real - descanso_efectivo
    if trabajo_real.total_seconds() < 0:
        trabajo_real = timedelta(0)

    it.trabajo_real = trabajo_real

    # Diferencia respecto a la jornada teórica
    diff = trabajo_real - dur_teorica

    if diff.total_seconds() > 0:
        return diff, timedelta(0)
    elif diff.total_seconds() < 0:
        return timedelta(0), -diff
    else:
        return timedelta(0), timedelta(0)

def determinar_ubicacion_por_coordenadas(lat, lon, ubicaciones, margen_extra_m=10.0):
    """
    Dado un par (lat, lon) y una lista de Location,
    devuelve la Location cuyo área (radio_meters) contenga ese punto.

    - Usa el radio configurado de cada ubicación (radius_meters).
    - margen_extra_m permite añadir unos metros de tolerancia para el ruido del GPS.
    - Si no coincide con ninguna, devuelve None.
    """
    if lat is None or lon is None:
        return None

    for loc in ubicaciones:
        # radio_base puede ser 0 si por error se dejó a 0
        radio_base = loc.radius_meters or 0.0
        radio_efectivo = radio_base + margen_extra_m

        # Si por lo que sea el radio total es <= 0, no tiene sentido usar esta ubicación
        if radio_efectivo <= 0:
            continue

        if is_within_radius(
            lat,
            lon,
            loc.latitude,
            loc.longitude,
            radio_efectivo,
        ):
            # Primer match que encontremos lo devolvemos.
            # Si quisieras ser más fino, aquí podrías buscar la más cercana,
            # pero normalmente con el primer match es suficiente.
            return loc

    return None

def construir_intervalo(entrada, salida, ubicaciones_definidas):
    """
    Construye un objeto 'intervalo' a partir de una posible entrada y una posible salida.
    Devuelve un SimpleNamespace con:
      - usuario
      - entrada, salida (Registros o None)
      - entrada_momento, salida_momento
      - label_entrada, label_salida, ubicacion_label (texto combinado)
      - entrada_lat, entrada_lon, salida_lat, salida_lon
      - row_id (para usar en el DOM)
    """

    usuario = None
    if entrada is not None:
        usuario = entrada.usuario
    elif salida is not None:
        usuario = salida.usuario

    def info_ubicacion(reg):
        """
        Para un Registro devuelve (label, lat, lon)
        label:
          - nombre de Location si está dentro de alguna
          - "lat, lon" si no coincide con ninguna
          - "Sin datos" si no hay coordenadas
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

    # Texto combinado para CSV/PDF: una sola etiqueta si son iguales,
    # o "Entrada - Salida" si son distintas
    if label_e and label_s:
        if label_e == label_s:
            ubicacion_label = label_e
        else:
            ubicacion_label = f"{label_e} - {label_s}"
    else:
        ubicacion_label = label_e or label_s or "Sin datos"

    # ID de fila (usamos el id de entrada si existe, si no el de salida)
    row_id = None
    if entrada is not None:
        row_id = entrada.id
    elif salida is not None:
        row_id = salida.id

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

    Devuelve una lista de objetos (SimpleNamespace) con la estructura
    generada por construir_intervalo().

    Además, hace una limpieza extra:
      - Si para un mismo usuario y día existe un intervalo COMPLETO
        (entrada y salida) y, además, intervalos "huérfanos" cuya
        entrada o salida coinciden EXACTAMENTE en fecha/hora con la
        entrada/salida del completo, esos huérfanos se descartan.
        Esto evita que, tras ediciones, aparezcan filas
        "Sin entrada" / "Sin salida" duplicadas.
    """
    intervalos = []

    # Todas las ubicaciones (excepto "Flexible") para resolver nombres
    ubicaciones_definidas = Location.query.filter(
        Location.name != "Flexible"
    ).all()

    # Agrupamos solo por usuario
    regs_por_usuario = defaultdict(list)
    for r in registros:
        if r.usuario_id is None or r.momento is None:
            continue
        regs_por_usuario[r.usuario_id].append(r)

    # --- Emparejado básico entrada/salida ---
    for uid, regs_usuario in regs_por_usuario.items():
        regs_ordenados = sorted(regs_usuario, key=lambda x: x.momento)
        entrada_actual = None

        for r in regs_ordenados:
            if r.accion == "entrada":
                if entrada_actual is None:
                    entrada_actual = r
                else:
                    # Teníamos una entrada sin salida -> intervalo huérfano
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
                    # Salida sin entrada previa en el filtro
                    intervalos.append(
                        construir_intervalo(
                            None, r, ubicaciones_definidas
                        )
                    )

        # Si al final quedan entradas sin salida, también las añadimos
        if entrada_actual is not None:
            intervalos.append(
                construir_intervalo(
                    entrada_actual, None, ubicaciones_definidas
                )
            )

    # --- Limpieza de duplicados por usuario + día ---
    # Clave: (usuario_id, fecha)
    grupos = defaultdict(list)
    for it in intervalos:
        if it.usuario is not None:
            uid = it.usuario.id
        else:
            uid = None

        if it.entrada_momento is not None:
            dia = it.entrada_momento.date()
        elif it.salida_momento is not None:
            dia = it.salida_momento.date()
        else:
            dia = None

        grupos[(uid, dia)].append(it)

    intervalos_limpios = []

    for (uid, dia), ints in grupos.items():
        # Intervalos completos (tienen entrada y salida)
        completos = [
            it for it in ints
            if it.entrada_momento is not None and it.salida_momento is not None
        ]

        # Si no hay completos, no hay nada que limpiar
        if not completos:
            intervalos_limpios.extend(ints)
            continue

        # Conjuntos de horas de entrada y salida de intervalos completos
        entradas_completas = {
            it.entrada_momento for it in completos if it.entrada_momento
        }
        salidas_completas = {
            it.salida_momento for it in completos if it.salida_momento
        }

        for it in ints:
            # Dejamos siempre los completos
            if it in completos:
                intervalos_limpios.append(it)
                continue

            descartar = False

            # Huérfano con solo entrada, que coincide
            # exactamente con una entrada de intervalo completo
            if (
                it.entrada_momento is not None
                and it.salida_momento is None
                and it.entrada_momento in entradas_completas
            ):
                descartar = True

            # Huérfano con solo salida, que coincide
            # exactamente con una salida de intervalo completo
            if (
                it.salida_momento is not None
                and it.entrada_momento is None
                and it.salida_momento in salidas_completas
            ):
                descartar = True

            if not descartar:
                intervalos_limpios.append(it)

    # Orden global por momento (entrada si hay, si no salida), descendente
    def key_intervalo(it):
        if it.entrada_momento is not None:
            return it.entrada_momento
        elif it.salida_momento is not None:
            return it.salida_momento
        else:
            return datetime.min

    intervalos_limpios.sort(key=key_intervalo, reverse=True)
    return intervalos_limpios

# Método para calcular las horas trabajadas considerando los descansos
def calcular_horas_trabajadas(registros):
    """
    Delegamos en la implementación original de services_fichaje para no
    duplicar lógica aquí y evitar errores.
    """
    from services_fichaje import calcular_horas_trabajadas as _calc
    return _calc(registros)

@app.route("/fichar", methods=["POST"])
@login_required
def fichar():
    # Obtenemos las ubicaciones (esquema nuevo o antiguo)
    ubicaciones_usuario = obtener_ubicaciones_usuario(current_user)

    if not ubicaciones_usuario:
        flash(
            "No tienes una ubicación asignada. Contacta con el administrador.",
            "error",
        )
        return redirect(url_for("index"))

    flexible_activo = usuario_tiene_flexible(current_user)

    accion = request.form.get("accion")
    if accion not in ("entrada", "salida", "descanso_inicio", "descanso_fin"):
        flash("Acción no válida", "error")
        return redirect(url_for("index"))

    ultimo_registro = (
        Registro.query.filter_by(usuario_id=current_user.id)
        .order_by(Registro.momento.desc())
        .first()
    )

    # === Validación de secuencia entrada/salida/descanso ===
    if accion in ("entrada", "salida"):
        # Validación estándar de entrada/salida
        es_valido, msg_error = validar_secuencia_fichaje(accion, ultimo_registro)
        if not es_valido:
            flash(msg_error, "error")
            return redirect(url_for("index"))
    else:
        # Reglas para descanso
        if not usuario_tiene_intervalo_abierto(current_user.id):
            flash("No puedes registrar un descanso si no has fichado la entrada.", "error")
            return redirect(url_for("index"))

        if accion == "descanso_inicio":
            # No permitir iniciar descanso si ya hay uno en curso
            ultimo_inicio = (
                Registro.query
                .filter_by(usuario_id=current_user.id, accion="descanso_inicio")
                .order_by(Registro.momento.desc())
                .first()
            )
            if ultimo_inicio:
                fin_posterior = (
                    Registro.query
                    .filter(
                        Registro.usuario_id == current_user.id,
                        Registro.accion == "descanso_fin",
                        Registro.momento > ultimo_inicio.momento,
                    )
                    .first()
                )
                if not fin_posterior:
                    flash("Ya tienes un descanso en curso.", "error")
                    return redirect(url_for("index"))

        elif accion == "descanso_fin":
            # No permitir terminar descanso si no hay uno en curso
            ultimo_inicio = (
                Registro.query
                .filter_by(usuario_id=current_user.id, accion="descanso_inicio")
                .order_by(Registro.momento.desc())
                .first()
            )
            if not ultimo_inicio:
                flash("No hay ningún descanso en curso que terminar.", "error")
                return redirect(url_for("index"))

            fin_posterior = (
                Registro.query
                .filter(
                    Registro.usuario_id == current_user.id,
                    Registro.accion == "descanso_fin",
                    Registro.momento > ultimo_inicio.momento,
                )
                .first()
            )
            if fin_posterior:
                flash("No hay ningún descanso en curso que terminar.", "error")
                return redirect(url_for("index"))

    # === Comprobación de horario (si está configurado y se fuerza) ===
    settings = getattr(current_user, "schedule_settings", None)
    if settings and settings.enforce_schedule:
        # Horarios asignados al usuario (many-to-many)
        user_schedules = list(current_user.schedules)

        if not user_schedules:
            flash(
                "No tienes ningún horario asignado. Contacta con el administrador.",
                "error",
            )
            return redirect(url_for("index"))

        margin = settings.margin_minutes or 0
        ahora = datetime.now()  # Hora local del servidor
        hoy = ahora.date()
        dow = hoy.weekday()  # 0 = lunes ... 6 = domingo

        autorizado_por_horario = False

        for sched in user_schedules:
            # --- Elegir tramo horario según use_per_day ---
            if sched.use_per_day:
                # Buscar el día concreto
                dia = next((d for d in sched.days if d.day_of_week == dow), None)
                if not dia:
                    # En este horario, hoy no es laborable
                    continue

                inicio_t = dia.start_time
                fin_t = dia.end_time
            else:
                inicio_t = sched.start_time
                fin_t = sched.end_time

            # Si por lo que sea faltan horas, nos saltamos este horario
            if not inicio_t or not fin_t:
                continue

            inicio_dt = datetime.combine(hoy, inicio_t)
            fin_dt = datetime.combine(hoy, fin_t)

            # Si el horario cruza medianoche (ej. 22:00–06:00)
            if fin_dt <= inicio_dt:
                fin_dt += timedelta(days=1)

            # Aplicamos margen
            inicio_con_margen = inicio_dt - timedelta(minutes=margin)
            fin_con_margen = fin_dt + timedelta(minutes=margin)

            if inicio_con_margen <= ahora <= fin_con_margen:
                autorizado_por_horario = True
                break

        if not autorizado_por_horario:
            flash(
                "No estás dentro de tu horario autorizado para fichar "
                f"(se tiene en cuenta un margen de {margin} minutos).",
                "error",
            )
            return redirect(url_for("index"))

    # === Coordenadas ===
    lat_str = request.form.get("lat")
    lon_str = request.form.get("lon")

    if not lat_str or not lon_str:
        flash(
            "No se recibió la ubicación del dispositivo. Comprueba los permisos de geolocalización.",
            "error",
        )
        return redirect(url_for("index"))

    try:
        lat_user = float(lat_str)
        lon_user = float(lon_str)
    except ValueError:
        flash("Coordenadas de ubicación inválidas.", "error")
        return redirect(url_for("index"))

    # Si NO está en modo Flexible, comprobamos radios
    if not flexible_activo:
        autorizado = False

        for loc in ubicaciones_usuario:
            # Por si coexistieran Flexible + fijas, ignoramos Flexible en el cálculo de radios
            if (loc.name or "").lower() == "flexible":
                continue

            if is_within_radius(
                lat_user,
                lon_user,
                loc.latitude,
                loc.longitude,
                loc.radius_meters,
            ):
                autorizado = True
                break

        if not autorizado:
            flash(
                "No estás dentro de ninguna de tus ubicaciones autorizadas. No se registra el fichaje.",
                "error",
            )
            return redirect(url_for("index"))

    # === Registro de fichaje ===
    registro = Registro(
        usuario_id=current_user.id,
        accion=accion,
        momento=datetime.utcnow(),
        latitude=lat_user,
        longitude=lon_user,
    )
    db.session.add(registro)
    db.session.commit()

    flash("Fichaje registrado correctamente", "success")
    return redirect(url_for("index"))

# ======================================================
# Gestión de usuarios
# ======================================================

@app.route("/register", methods=["GET", "POST"])
@admin_required
def register():
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")
        role = request.form.get("role", "empleado")

        if not username or not password:
            flash("Usuario y contraseña son obligatorios", "error")
            return redirect(url_for("register"))

        if User.query.filter_by(username=username).first():
            flash("Ese nombre de usuario ya existe", "error")
            return redirect(url_for("register"))

        nuevo_usuario = User(username=username, role=role)
        nuevo_usuario.set_password(password)
        db.session.add(nuevo_usuario)
        db.session.commit()

        flash("Usuario creado correctamente.", "success")
        return redirect(url_for("register"))

    return render_template("register.html")

# ======================================================
# Administración de registros (filtros, CSV y PDF)
# ======================================================

@app.route("/admin/registros", methods=["GET", "POST"])
@admin_required
def admin_registros():
    usuarios = User.query.order_by(User.username).all()
    # Todas las ubicaciones configuradas, excepto "Flexible"
    ubicaciones_definidas = (
        Location.query.filter(Location.name != "Flexible")
        .order_by(Location.name)
        .all()
    )

    # Valores por defecto (GET)
    usuario_seleccionado = "all"
    tipo_periodo = "rango"
    fecha_desde = ""
    fecha_hasta = ""
    fecha_semana = ""
    mes = None   # entero o None
    registros = []
    intervalos = []
    ubicacion_filtro = "all"

    if request.method == "POST":
        usuario_seleccionado = request.form.get("usuario_id", "all")
        tipo_periodo = request.form.get("tipo_periodo", "rango")
        fecha_desde = request.form.get("fecha_desde", "")
        fecha_hasta = request.form.get("fecha_hasta", "")
        fecha_semana = request.form.get("fecha_semana", "")
        mes_str = request.form.get("mes", "")
        accion = request.form.get("accion", "filtrar")
        ubicacion_filtro = request.form.get("ubicacion_filtro", "all")

        # mes_str -> mes (int o None)
        mes = int(mes_str) if mes_str else None

        query = Registro.query.join(User).order_by(Registro.momento.desc())

        # ---- Filtro por usuario ----
        if usuario_seleccionado != "all":
            try:
                uid = int(usuario_seleccionado)
                query = query.filter(Registro.usuario_id == uid)
            except ValueError:
                flash("Usuario no válido.", "error")

        # ---- Filtro por tipo de periodo ----
        if tipo_periodo == "rango":
            if fecha_desde:
                try:
                    dt_desde = datetime.strptime(fecha_desde, "%Y-%m-%d")
                    dt_desde = dt_desde.replace(
                        hour=0, minute=0, second=0, microsecond=0
                    )
                    query = query.filter(Registro.momento >= dt_desde)
                except ValueError:
                    flash("Fecha 'desde' no válida.", "error")

            if fecha_hasta:
                try:
                    dt_hasta = datetime.strptime(fecha_hasta, "%Y-%m-%d")
                    dt_hasta = dt_hasta.replace(
                        hour=23,
                        minute=59,
                        second=59,
                        microsecond=999999,
                    )
                    query = query.filter(Registro.momento <= dt_hasta)
                except ValueError:
                    flash("Fecha 'hasta' no válida.", "error")

        elif tipo_periodo == "semanal":
            if fecha_semana:
                try:
                    start_of_week = datetime.strptime(fecha_semana, "%Y-%m-%d")
                    start_of_week = start_of_week.replace(
                        hour=0, minute=0, second=0, microsecond=0
                    )
                    end_of_week = start_of_week + timedelta(
                        days=6, hours=23, minutes=59, seconds=59
                    )
                    query = query.filter(
                        Registro.momento >= start_of_week,
                        Registro.momento <= end_of_week,
                    )
                except ValueError:
                    flash("Fecha de semana no válida.", "error")

        elif tipo_periodo == "mensual":
            if mes:
                try:
                    hoy = datetime.today()
                    year = hoy.year
                    start_of_month = datetime(year, mes, 1, 0, 0, 0)

                    if mes == 12:
                        next_month = datetime(year + 1, 1, 1, 0, 0, 0)
                    else:
                        next_month = datetime(year, mes + 1, 1, 0, 0, 0)

                    end_of_month = next_month - timedelta(seconds=1)

                    query = query.filter(
                        Registro.momento >= start_of_month,
                        Registro.momento <= end_of_month,
                    )
                except ValueError:
                    flash("Mes no válido.", "error")

        elif tipo_periodo == "historico":
            # No se filtra por fechas: se muestran todos los registros que cumplan el filtro de usuario
            pass

        # Ejecutamos la consulta una sola vez (usuario + periodo + tiempo)
        registros = query.all()

        # ---- Filtro por ubicación (a nivel de fichaje) ----
        if ubicacion_filtro != "all":
            registros_filtrados = []
            if ubicacion_filtro == "flexible":
                # Registros fuera de cualquier ubicación conocida
                for r in registros:
                    loc_match = determinar_ubicacion_por_coordenadas(
                        r.latitude,
                        r.longitude,
                        ubicaciones_definidas,
                    )
                    if loc_match is None:
                        registros_filtrados.append(r)
            else:
                # Filtro por una ubicación concreta
                try:
                    loc_id = int(ubicacion_filtro)
                    loc_sel = Location.query.get(loc_id)
                except ValueError:
                    loc_sel = None

                if loc_sel:
                    for r in registros:
                        if r.latitude is None or r.longitude is None:
                            continue
                        if is_within_radius(
                            r.latitude,
                            r.longitude,
                            loc_sel.latitude,
                            loc_sel.longitude,
                            loc_sel.radius_meters,
                        ):
                            registros_filtrados.append(r)

            registros = registros_filtrados

        # ---- Agrupar en intervalos Entrada/Salida ----
        intervalos = agrupar_registros_en_intervalos(registros)

        # ---- Calcular horas extra / defecto por intervalo ----
        for it in intervalos:
            extra_td, defecto_td = calcular_extra_y_defecto_intervalo(it)
            it.horas_extra = extra_td
            it.horas_defecto = defecto_td

        # ---- Calcular descanso real por intervalo ----
        calcular_descanso_intervalos(intervalos, registros)
        
        # ---- Exportaciones ----
        if accion == "csv":
            return generar_csv(intervalos)
        if accion == "pdf":
            return generar_pdf(intervalos, tipo_periodo)

    else:
        # GET: mes None
        mes = None
        registros = []
        intervalos = []

    # ---- Resumen de horas trabajadas por usuario en el filtro actual ----
    horas_por_usuario_td = {}

    for it in intervalos:
        if not it.usuario:
            continue

        # Nos aseguramos de que el intervalo tenga trabajo_real calculado
        trabajo_real = getattr(it, "trabajo_real", None)
        if trabajo_real is None:
            extra_td, defecto_td = calcular_extra_y_defecto_intervalo(it)
            it.horas_extra = extra_td
            it.horas_defecto = defecto_td
            trabajo_real = getattr(it, "trabajo_real", timedelta(0))

        if trabajo_real.total_seconds() <= 0:
            continue

        username = it.usuario.username
        horas_por_usuario_td[username] = horas_por_usuario_td.get(username, timedelta()) + trabajo_real

    # Lo convertimos a texto formateado HH:mm
    horas_por_usuario = {
        username: formatear_timedelta(td)
        for username, td in horas_por_usuario_td.items()
    }

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
    )

@app.route("/admin/registros/<int:registro_id>/editar", methods=["GET", "POST"])
@admin_required
def editar_registro(registro_id):
    """
    Editor de INTERVALO (entrada + salida) a partir de un id de registro
    (puede ser el id de la entrada o de la salida).
    Permite:
      - Editar entrada y salida en la misma pantalla.
      - Crear entrada/salida si falta alguna.
      - Eliminar el intervalo completo (entrada + salida).
    """
    usuarios = User.query.order_by(User.username).all()

    if request.method == "POST":
        # --------- PARTE COMÚN: usuario y ids ocultos ----------
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

        # Si se pulsa "Eliminar", borramos todo el intervalo
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

        # --------- EDICIÓN / CREACIÓN DE ENTRADA ----------
        entrada_momento_str = request.form.get("entrada_momento", "").strip()
        entrada_lat_str = request.form.get("entrada_latitude", "").strip()
        entrada_lon_str = request.form.get("entrada_longitude", "").strip()

        entrada = Registro.query.get(int(entrada_id_str)) if entrada_id_str else None

        if entrada_momento_str:
            try:
                entrada_momento = datetime.strptime(
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

            if entrada:
                # Auditoría de la entrada
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
                # Crear nueva entrada
                entrada = Registro(
                    usuario_id=nuevo_usuario_id,
                    accion="entrada",
                    momento=entrada_momento,
                    latitude=entrada_lat,
                    longitude=entrada_lon,
                )
                db.session.add(entrada)
        # Si entrada_momento_str está vacío:
        #   - Si había entrada, la dejamos tal cual.
        #   - Si no había, seguimos sin entrada.

        # --------- EDICIÓN / CREACIÓN DE SALIDA ----------
        salida_momento_str = request.form.get("salida_momento", "").strip()
        salida_lat_str = request.form.get("salida_latitude", "").strip()
        salida_lon_str = request.form.get("salida_longitude", "").strip()

        salida = Registro.query.get(int(salida_id_str)) if salida_id_str else None

        if salida_momento_str:
            try:
                salida_momento = datetime.strptime(
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

            if salida:
                # Auditoría de la salida
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
                # Crear nueva salida
                salida = Registro(
                    usuario_id=nuevo_usuario_id,
                    accion="salida",
                    momento=salida_momento,
                    latitude=salida_lat,
                    longitude=salida_lon,
                )
                db.session.add(salida)
        # Si salida_momento_str está vacío:
        #   - Si había salida, la dejamos tal cual.
        #   - Si no había, seguimos sin salida.

        # --------- VALIDACIÓN INTERVALO COHERENTE ---------
        # Volvemos a cargar entrada y salida (por si se han creado nuevas)
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

        # --------- AJUSTE DEL DESCANSO SEGÚN EL FORMULARIO ---------
        # Solo tiene sentido si hay entrada y salida
        descanso_str = request.form.get("descanso_val") or request.form.get("descanso") or ""
        descanso_str = descanso_str.strip()

        if entrada_m and salida_m and descanso_str:
            # Formato esperado: "HH:MM"
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

            # Borrar TODOS los registros de descanso dentro del intervalo actual
            # para este usuario, y recrearlos con la nueva duración
            if total_min >= 0:
                # Eliminamos registros 'descanso_inicio' y 'descanso_fin' del tramo
                Registro.query.filter(
                    Registro.usuario_id == nuevo_usuario_id,
                    Registro.momento >= entrada_m,
                    Registro.momento <= salida_m,
                    Registro.accion.in_(["descanso_inicio", "descanso_fin"]),
                ).delete(synchronize_session=False)

                # Si el descanso es > 0, creamos un nuevo par
                if total_min > 0:
                    duracion_descanso = timedelta(minutes=total_min)

                    # Colocamos el descanso "centrado" en el intervalo
                    dur_trabajo = salida_m - entrada_m
                    if dur_trabajo.total_seconds() < duracion_descanso.total_seconds():
                        # Si el descanso es mayor que el intervalo, lo ajustamos
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

        # --------- FIN AJUSTE DESCANSO ---------

        db.session.commit()
        flash("Registro actualizado correctamente.", "success")
        return redirect(url_for("admin_registros"))

    # ------------------- GET: construir INTERVALO -------------------
    # Partimos de un registro cualquiera (entrada o salida) para encontrar su intervalo
    reg_base = Registro.query.get_or_404(registro_id)

    # Todos los registros de ese usuario
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

    # Si por lo que sea no encontramos el intervalo, montamos uno mínimo
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

    entrada_momento_val = (
        entrada.momento.strftime("%Y-%m-%dT%H:%M") if entrada and entrada.momento else ""
    )
    salida_momento_val = (
        salida.momento.strftime("%Y-%m-%dT%H:%M") if salida and salida.momento else ""
    )

    entrada_lat = f"{entrada.latitude:.6f}" if entrada and entrada.latitude is not None else ""
    entrada_lon = f"{entrada.longitude:.6f}" if entrada and entrada.longitude is not None else ""
    salida_lat = f"{salida.latitude:.6f}" if salida and salida.latitude is not None else ""
    salida_lon = f"{salida.longitude:.6f}" if salida and salida.longitude is not None else ""
    # Calcular descanso real del intervalo para mostrarlo en el formulario
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

def generar_csv(intervalos):
    """Generar un archivo CSV a partir de los intervalos Entrada/Salida."""
    output = StringIO()
    writer = csv.writer(output, delimiter=";")

    # Cabecera
    writer.writerow([
        "Usuario",
        "Fecha/hora entrada",
        "Fecha/hora salida",
        "Descanso",       # ⬅ NUEVA COLUMNA
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

        # Descanso real del intervalo (si no existe, asumimos 00:00)
        if hasattr(it, "descanso_total") and it.descanso_total:
            descanso_str = formatear_timedelta(it.descanso_total)
        else:
            descanso_str = "00:00"

        # Formatear horas extra/defecto si existen
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
            descanso_str,          # ⬅ NUEVA COLUMNA
            it.ubicacion_label or "",
            he,                    # Horas extra
            hd,                    # Horas en defecto
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

    # Aseguramos horas extra/defecto y trabajo_real en cada intervalo
    for it in intervalos:
        extra_td, defecto_td = calcular_extra_y_defecto_intervalo(it)
        it.horas_extra = extra_td
        it.horas_defecto = defecto_td

    # --- Resumen de horas trabajadas por usuario (mismas reglas que en la vista) ---
    resumen_td = {}

    for it in intervalos:
        if not it.usuario:
            continue

        trabajo_real = getattr(it, "trabajo_real", timedelta(0))
        if trabajo_real.total_seconds() <= 0:
            continue

        username = it.usuario.username
        resumen_td[username] = resumen_td.get(username, timedelta()) + trabajo_real

    # Pasamos al template un dict username -> "HH:MM"
    resumen_horas = {
        username: formatear_timedelta(td)
        for username, td in resumen_td.items()
    }

    # Renderizar el PDF
    html = render_template(
        "informe_pdf.html",
        intervalos=intervalos,
        resumen_horas=resumen_horas,
        tipo_periodo=tipo_periodo,
        formatear_timedelta=formatear_timedelta,
    )
    return render_pdf(HTML(string=html))
    
# ======================================================
# Login / Logout
# ======================================================

@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))

    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")

        user = User.query.filter_by(username=username).first()

        if user is None or not user.check_password(password):
            flash("Usuario o contraseña incorrectos", "error")
            return redirect(url_for("login"))

        login_user(user)
        flash("Has iniciado sesión correctamente", "success")
        return redirect(url_for("index"))

    return render_template("login.html")

@app.route("/logout")
@login_required
def logout():
    logout_user()
    flash("Has cerrado sesión", "success")
    return redirect(url_for("login"))

# ======================================================
# Endpoint de salud para checks HTTP desde la consola
# ======================================================

@app.route("/health")
def health():
    """
    Endpoint simple para health-checks.
    No requiere login, solo devuelve 200 OK.
    """
    return "OK", 200

# ======================================================
# Inicialización al importar (gunicorn, etc.)
# ======================================================

init_app()
if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=8000,
        debug=False,
    )
