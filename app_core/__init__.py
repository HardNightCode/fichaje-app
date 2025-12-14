import logging
import os
from pathlib import Path
from logging.handlers import RotatingFileHandler

from flask import Flask

from .config import to_local
from .db_setup import crear_tablas
from .extensions import db, login_manager
from .routes import register_routes


def create_app():
    base_dir = Path(__file__).resolve().parent.parent

    # Aseguramos que Flask mire a las carpetas originales de plantillas y estáticos
    app = Flask(
        __name__,
        template_folder=str(base_dir / "templates"),
        static_folder=str(base_dir / "static"),
    )
    app.jinja_env.filters["to_local"] = to_local
    app.config["SECRET_KEY"] = os.getenv(
        "SECRET_KEY",
        "cambia-esta-clave-por-una-mas-segura",
    )

    instance_dir = base_dir / "instance"
    instance_dir.mkdir(exist_ok=True)
    default_sqlite_path = instance_dir / "fichaje.db"
    default_sqlite_uri = f"sqlite:///{default_sqlite_path}"

    app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv(
        "DATABASE_URL",
        default_sqlite_uri,
    )
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    db.init_app(app)

    login_manager.login_view = "login"
    login_manager.init_app(app)

    if not app.debug:
        log_dir = os.path.join(base_dir, "logs")
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

    register_routes(app)

    with app.app_context():
        crear_tablas()

    return app


__all__ = ["create_app", "db"]
