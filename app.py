from datetime import datetime, date, timedelta
from functools import wraps
from pathlib import Path
import os
import logging
from logging.handlers import RotatingFileHandler

from flask import Flask, render_template, request, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy
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
    calcular_horas_trabajadas,
    formatear_timedelta,
)

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
    location_id = db.Column(db.Integer, db.ForeignKey("location.id"), nullable=True)

    location = db.relationship("Location", backref=db.backref("users", lazy=True))

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

@app.route("/")
@login_required
def index():
    # Si es admin, ve todos los registros
    if current_user.role == "admin":
        registros = Registro.query.order_by(Registro.momento.desc()).all()
    else:
        registros = (
            Registro.query.filter_by(usuario_id=current_user.id)
            .order_by(Registro.momento.desc())
            .all()
        )

    # Resumen de horas para el usuario actual
    registros_usuario = (
        Registro.query.filter_by(usuario_id=current_user.id)
        .order_by(Registro.momento.asc())
        .all()
    )
    horas_por_dia = calcular_horas_trabajadas(registros_usuario)
    resumen_horas = [
        {
            "dia": dia,
            "horas": formatear_timedelta(td),
        }
        for dia, td in sorted(horas_por_dia.items(), key=lambda x: x[0], reverse=True)
    ]

    return render_template("index.html", registros=registros, resumen_horas=resumen_horas)


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

    ubicaciones = Location.query.order_by(Location.name).all()
    return render_template("admin_ubicaciones.html", ubicaciones=ubicaciones)


@app.route("/admin/ubicaciones/<int:loc_id>/editar", methods=["GET", "POST"])
@admin_required
def editar_ubicacion(loc_id):
    loc = Location.query.get_or_404(loc_id)

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        lat = request.form.get("latitude", "").strip()
        lon = request.form.get("longitude", "").strip()
        radius = request.form.get("radius_meters", "").strip()

        if not name or not lat or not lon or not radius:
            flash("Todos los campos son obligatorios.", "error")
            return redirect(url_for("editar_ubicacion", loc_id=loc.id))

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

    if request.method == "POST":
        for user in usuarios:
            field_name = f"location_{user.id}"
            value = request.form.get(field_name, "none")

            if value in ("none", ""):
                user.location_id = None
            else:
                try:
                    user.location_id = int(value)
                except ValueError:
                    continue

        db.session.commit()
        flash("Ubicaciones de usuarios actualizadas.", "success")
        return redirect(url_for("admin_usuarios"))

    return render_template(
        "admin_usuarios.html",
        usuarios=usuarios,
        ubicaciones=ubicaciones,
    )


@app.route("/fichar", methods=["POST"])
@login_required
def fichar():
    if current_user.location is None:
        flash(
            "No tienes una ubicación asignada. Contacta con el administrador.",
            "error",
        )
        return redirect(url_for("index"))

    accion = request.form.get("accion")
    if accion not in ("entrada", "salida"):
        flash("Acción no válida", "error")
        return redirect(url_for("index"))

    # Validar secuencia entrada/salida
    ultimo_registro = (
        Registro.query.filter_by(usuario_id=current_user.id)
        .order_by(Registro.momento.desc())
        .first()
    )
    es_valido, msg_error = validar_secuencia_fichaje(accion, ultimo_registro)
    if not es_valido:
        flash(msg_error, "error")
        return redirect(url_for("index"))

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

    loc = current_user.location
    if not is_within_radius(
        lat_user,
        lon_user,
        loc.latitude,
        loc.longitude,
        loc.radius_meters,
    ):
        flash(
            "No estás dentro de tu ubicación autorizada. No se registra el fichaje.",
            "error",
        )
        return redirect(url_for("index"))

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


@app.route("/admin/registros", methods=["GET", "POST"])
@admin_required
def admin_registros():
    usuarios = User.query.order_by(User.username).all()

    registros = []
    usuario_seleccionado = "all"
    fecha_desde = ""
    fecha_hasta = ""

    if request.method == "POST":
        usuario_seleccionado = request.form.get("usuario_id", "all")
        fecha_desde = request.form.get("fecha_desde", "")
        fecha_hasta = request.form.get("fecha_hasta", "")

        query = Registro.query.join(User).order_by(Registro.momento.desc())

        if usuario_seleccionado != "all":
            try:
                uid = int(usuario_seleccionado)
                query = query.filter(Registro.usuario_id == uid)
            except ValueError:
                flash("Usuario no válido.", "error")

        if fecha_desde:
            try:
                dt_desde = datetime.strptime(fecha_desde, "%Y-%m-%d")
                query = query.filter(Registro.momento >= dt_desde)
            except ValueError:
                flash("Fecha 'desde' no válida.", "error")

        if fecha_hasta:
            try:
                dt_hasta = datetime.strptime(fecha_hasta, "%Y-%m-%d")
                dt_hasta = dt_hasta.replace(hour=23, minute=59, second=59)
                query = query.filter(Registro.momento <= dt_hasta)
            except ValueError:
                flash("Fecha 'hasta' no válida.", "error")

        registros = query.all()

    # Horas trabajadas por usuario dentro del filtro
    horas_por_usuario = {}
    for usuario in usuarios:
        regs_usuario = [r for r in registros if r.usuario_id == usuario.id]
        if not regs_usuario:
            continue
        regs_usuario_ordenados = sorted(regs_usuario, key=lambda r: r.momento)
        horas_dia = calcular_horas_trabajadas(regs_usuario_ordenados)
        total = sum(horas_dia.values(), start=timedelta())
        if total.total_seconds() > 0:
            horas_por_usuario[usuario.username] = formatear_timedelta(total)

    return render_template(
        "admin_registros.html",
        usuarios=usuarios,
        registros=registros,
        usuario_seleccionado=usuario_seleccionado,
        fecha_desde=fecha_desde,
        fecha_hasta=fecha_hasta,
        horas_por_usuario=horas_por_usuario,
    )


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
