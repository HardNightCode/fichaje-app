from datetime import datetime, time
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

from .extensions import db


class CompanyInfo(db.Model):
    __tablename__ = "company_info"

    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(255), nullable=True)
    cif = db.Column(db.String(50), nullable=True)
    direccion = db.Column(db.String(255), nullable=True)
    telefono = db.Column(db.String(50), nullable=True)
    email = db.Column(db.String(120), nullable=True)
    web = db.Column(db.String(255), nullable=True)
    logo_path = db.Column(db.String(255), nullable=True)
    descripcion = db.Column(db.Text, nullable=True)


class User(UserMixin, db.Model):
    __tablename__ = "user"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), default="empleado")  # 'admin', 'empleado', 'kiosko', 'kiosko_admin'

    # Obliga a cambiar la contraseña en el siguiente inicio de sesión
    must_change_password = db.Column(db.Boolean, default=False, nullable=False)

    # Relación antigua (ubicación única)
    location_id = db.Column(db.Integer, db.ForeignKey("location.id"), nullable=True)
    location = db.relationship("Location", backref=db.backref("users_single", lazy=True))

    # Relación muchos-a-muchos (ubicaciones múltiples)
    locations_multi = db.relationship(
        "Location",
        secondary="user_location",
        backref=db.backref("users_multi", lazy="dynamic"),
    )

    # Relación muchos-a-muchos con horarios
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
   
    # Historial de ediciones (ordenado de más reciente a más antigua)
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


class RegistroJustificacion(db.Model):
    """
    Motivo asociado a un fichaje de salida cuando hay horas extra.
    """
    __tablename__ = "registro_justificacion"

    id = db.Column(db.Integer, primary_key=True)
    registro_id = db.Column(db.Integer, db.ForeignKey("registro.id"), nullable=False, unique=True)
    motivo = db.Column(db.String(120), nullable=False)
    detalle = db.Column(db.Text, nullable=True)
    creado_en = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    registro = db.relationship("Registro", backref=db.backref("justificacion", uselist=False))


class QRToken(db.Model):
    """
    Token de acceso por QR para app móvil.
    """
    __tablename__ = "qr_token"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    token = db.Column(db.String(255), unique=True, nullable=False)
    domain = db.Column(db.String(255), nullable=True)
    expires_at = db.Column(db.DateTime, nullable=True)
    revoked = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    user = db.relationship("User", backref=db.backref("qr_tokens", cascade="all, delete-orphan"))


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

    # ¿Usa configuración por días?
    use_per_day = db.Column(db.Boolean, default=False, nullable=False)

    # Días asociados (0=lunes ... 6=domingo)
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


class Kiosk(db.Model):
    """
    Kiosko físico/lógico.

    - name: nombre del kiosko (ej: 'Kiosko Recepción')
    - description: texto opcional
    - owner_id: usuario que administra este kiosko (rol 'admin' o 'kiosko_admin')
    - kiosk_account_id: usuario que se usa para iniciar sesión en el kiosko (rol 'kiosko')
    """
    __tablename__ = "kiosk"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False, unique=True)
    description = db.Column(db.String(255))

    owner_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    kiosk_account_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)

    owner = db.relationship(
        "User",
        foreign_keys=[owner_id],
        backref=db.backref("kioskos_propios", lazy="dynamic"),
    )
    kiosk_account = db.relationship(
        "User",
        foreign_keys=[kiosk_account_id],
        backref=db.backref("kioskos_como_cuenta", lazy="dynamic"),
    )


class KioskUser(db.Model):
    """
    Asociación kiosko <-> usuario que ficha en ese kiosko.

    - pin_hash: hash del PIN de 4 dígitos para este usuario en este kiosko
    - close_session_after_punch: flag que podrás usar en el frontend
      para decidir si, tras fichar, "se cierra" la sesión visual de ese usuario
      en el kiosko o se queda seleccionado.
    """
    __tablename__ = "kiosk_user"

    id = db.Column(db.Integer, primary_key=True)
    kiosk_id = db.Column(db.Integer, db.ForeignKey("kiosk.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)

    pin_hash = db.Column(db.String(255), nullable=False)
    close_session_after_punch = db.Column(db.Boolean, default=True, nullable=False)

    kiosk = db.relationship(
        "Kiosk",
        backref=db.backref("kiosk_users", cascade="all, delete-orphan", lazy="dynamic"),
    )
    user = db.relationship(
        "User",
        backref=db.backref("kiosk_links", cascade="all, delete-orphan", lazy="dynamic"),
    )

    __table_args__ = (
        db.UniqueConstraint("kiosk_id", "user_id", name="uq_kiosk_user"),
    )
