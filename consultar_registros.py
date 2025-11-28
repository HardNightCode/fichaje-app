from datetime import datetime
from app import app, db, Registro

def consultar_registros():
    # Abrimos el contexto de la aplicación
    with app.app_context():
        # Definir las fechas de inicio y fin
        fecha_inicio = datetime(2025, 11, 1)  # Fecha de inicio de ejemplo
        fecha_fin = datetime(2025, 11, 30)   # Fecha de fin de ejemplo

        # Consultar registros dentro de este rango de fechas
        registros = Registro.query.filter(Registro.momento >= fecha_inicio, Registro.momento <= fecha_fin).all()

        # Imprimir los resultados
        for reg in registros:
            print(f"Usuario: {reg.usuario.username}, Acción: {reg.accion}, Fecha: {reg.momento}")

if __name__ == "__main__":
    consultar_registros()
