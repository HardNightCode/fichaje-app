def register_health_route(app):
    @app.route("/health")
    def health():
        """
        Endpoint simple para health-checks.
        No requiere login, solo devuelve 200 OK.
        """
        return "OK", 200
