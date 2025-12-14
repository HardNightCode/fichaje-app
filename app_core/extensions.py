from flask_login import LoginManager
from flask_sqlalchemy import SQLAlchemy

# Extensiones compartidas para evitar importaciones circulares
db = SQLAlchemy()
login_manager = LoginManager()
