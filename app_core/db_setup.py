from sqlalchemy import inspect, text

from .extensions import db
from .models import User, Location, CompanyInfo, QRToken


def crear_tablas():
    engine = db.engine
    lock_acquired = True

    if engine.dialect.name == "postgresql":
        lock_acquired = False
        try:
            with engine.begin() as conn:
                lock_acquired = conn.execute(
                    text("SELECT pg_try_advisory_lock(19770628)")
                ).scalar()
        except Exception:
            lock_acquired = True

    if not lock_acquired:
        return

    try:
        _crear_tablas_base()
    finally:
        if engine.dialect.name == "postgresql" and lock_acquired:
            try:
                with engine.begin() as conn:
                    conn.execute(text("SELECT pg_advisory_unlock(19770628)"))
            except Exception:
                pass


def _crear_tablas_base():
    db.create_all()
    _asegurar_columnas_descanso()

    # Si no hay ningún usuario, creamos uno admin de ejemplo
    if User.query.count() == 0:
        admin = User(username="admin", role="admin")
        admin.set_password("admin123")  # cámbialo después
        db.session.add(admin)
        db.session.commit()

    # Gestión robusta de la ubicación "Flexible"
    flexibles = Location.query.filter_by(name="Flexible").all()

    if not flexibles:
        flexible = Location(
            name="Flexible",
            latitude=0.0,
            longitude=0.0,
            radius_meters=0.0,
        )
        db.session.add(flexible)
        db.session.commit()

    elif len(flexibles) > 1:
        principal = flexibles[0]
        sobrantes = flexibles[1:]

        # Esquema nuevo MANY-TO-MANY (User.locations_multi, backref="users_multi")
        if hasattr(Location, "users_multi"):
            for extra in sobrantes:
                for u in list(extra.users_multi):
                    if principal not in u.locations_multi:
                        u.locations_multi.append(principal)
                db.session.delete(extra)

        # Esquema antiguo ONE-TO-MANY (User.location, backref="users_single")
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

    # Aseguramos registro de empresa único
    if CompanyInfo.query.count() == 0:
        db.session.add(CompanyInfo(nombre="Mi Empresa", cif=""))
        db.session.commit()


def _asegurar_columnas_descanso():
    engine = db.engine
    inspector = inspect(engine)
    dialect = engine.dialect.name

    def _add_col(table, col_name, col_type_sql):
        try:
            cols = [c["name"] for c in inspector.get_columns(table)]
        except Exception:
            return
        if col_name in cols:
            return
        try:
            if dialect == "postgresql":
                stmt = f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col_name} {col_type_sql}"
            else:
                stmt = f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type_sql}"
            with engine.begin() as conn:
                conn.execute(text(stmt))
        except Exception:
            # Evitamos romper el arranque si la BD no permite DDL.
            pass

    col_type = "BOOLEAN NOT NULL DEFAULT FALSE"
    _add_col("schedule", "break_optional", col_type)
    _add_col("schedule", "break_paid", col_type)
    _add_col("schedule_day", "break_optional", col_type)
    _add_col("schedule_day", "break_paid", col_type)
    _add_col("user", "email", "VARCHAR(120)")
