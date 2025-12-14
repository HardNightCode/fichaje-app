from .admin_kioskos import register_admin_kiosk_routes
from .admin_locations import register_admin_location_routes
from .admin_registros import register_admin_registro_routes
from .admin_schedules import register_admin_schedule_routes
from .admin_users import register_admin_user_routes
from .company import register_company_routes
from .auth_routes import register_auth_routes
from .dashboard import register_dashboard_routes
from .fichajes import register_fichaje_routes
from .health import register_health_route
from .kiosko import register_kiosko_routes


def register_routes(app):
    register_auth_routes(app)
    register_dashboard_routes(app)
    register_fichaje_routes(app)
    register_admin_location_routes(app)
    register_admin_user_routes(app)
    register_admin_schedule_routes(app)
    register_admin_kiosk_routes(app)
    register_admin_registro_routes(app)
    register_kiosko_routes(app)
    register_health_route(app)
    register_company_routes(app)
