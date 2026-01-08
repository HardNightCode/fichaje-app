from sqlalchemy import inspect, text

from .extensions import db
from .models import User, Location, CompanyInfo, QRToken


def crear_tablas():
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

    def _add_col(table, col_name, ddl):
        try:
            cols = [c["name"] for c in inspector.get_columns(table)]
        except Exception:
            return
        if col_name in cols:
            return
        try:
            with engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {ddl}"))
        except Exception:
            pass

    _add_col("schedule", "break_optional", "break_optional BOOLEAN NOT NULL DEFAULT 0")
    _add_col("schedule", "break_paid", "break_paid BOOLEAN NOT NULL DEFAULT 0")
    _add_col("schedule_day", "break_optional", "break_optional BOOLEAN NOT NULL DEFAULT 0")
    _add_col("schedule_day", "break_paid", "break_paid BOOLEAN NOT NULL DEFAULT 0")
