# fichaje-app

Documentacion tecnica del modulo (aplicacion Flask de fichajes) para que cualquier desarrollador pueda entender el funcionamiento (mas o menos).
## Stack y arranque rapido
- Python + Flask, Flask-Login, Flask-SQLAlchemy, WeasyPrint (PDF), SQLite por defecto.
- Ejecutar en local: `python app.py` (usa `instance/fichaje.db` si no hay `DATABASE_URL`). Se crea usuario admin `admin/admin123` y la ubicacion especial `Flexible`.
- Variables relevantes: `SECRET_KEY`, `DATABASE_URL` (puede ser PostgreSQL), rutas de plantillas/estaticos se fijan en `app_core/__init__.py`.
- Logs rotativos en `logs/app.log` cuando `app.debug` es False.

## Arquitectura (archivo -> responsabilidad)
- `app.py`: punto de entrada, instancia Flask via `create_app`.
- `app_core/__init__.py`: configura Flask, base dir, filtros Jinja (`to_local`), secret key, DB URI, logging, registra rutas, crea tablas al arrancar.
- `app_core/extensions.py`: singletons `db` y `login_manager` para evitar ciclos de import.
- `app_core/config.py`: zona horaria Europe/Madrid, helpers `to_local` y `local_to_utc_naive` para convertir datetimes naive UTC <-> hora local.
- `app_core/db_setup.py`: `crear_tablas()` hace `db.create_all()`, crea admin por defecto, asegura ubicacion `Flexible`, inicializa `CompanyInfo` unica.
- `app_core/auth.py`: hooks Flask-Login (`user_loader`) y decoradores `admin_required`, `kiosko_admin_required`.
- `app_core/models.py`: modelos SQLAlchemy (detallados abajo).
- `app_core/logic.py`: logica de negocio: horarios, descansos, agrupacion de fichajes en intervalos, calculo de horas extra/defecto, deteccion de ubicacion por coordenadas.
- `app_core/reporting.py`: construye CSV/PDF con WeasyPrint usando `templates/informe_pdf.html`.
- `app_core/routes/`: blueprint-less rutas registradas manualmente; cubren auth, dashboard, fichajes, administracion (usuarios, ubicaciones, horarios, kioskos, registros, empresa), healthcheck y panel de kiosko.
- `services_fichaje.py`: version simplificada/legacy de validaciones y calculos de horas (la logica principal usa `app_core/logic.py`).
- `geo_utils.py`: distancia Haversine y verificacion de radio para geolocalizacion.
- `templates/`, `static/`: interfaz HTML/CSS para dashboard, panel de kiosko y pantallas de administracion.

## Modelo de datos (resumen)
- `User`: `username`, `password_hash`, `role` (`admin`, `empleado`, `kiosko`, `kiosko_admin`), flag `must_change_password`. Ubicaciones: legado `location` (FK) y esquema actual M2M `locations_multi` via `UserLocation`. Horarios: M2M via `UserSchedule`. Config individual en `UserScheduleSettings` (enforce, margen, deteccion futura).
- `Location`: nombre, latitud, longitud, radio en metros. La ubicacion `Flexible` permite fichar desde cualquier coordenada (se crea/normaliza en `db_setup`).
- `Registro`: accion (`entrada`, `salida`, `descanso_inicio`, `descanso_fin`), `momento` (UTC naive), lat/lon opcional. Relacion a `User`, historial `RegistroEdicion`, justificacion `RegistroJustificacion` (motivo si hay horas extra).
- Horarios: `Schedule` (modo simple start/end/break o por dias `use_per_day`); `ScheduleDay` define franjas y descansos por dia. `UserSchedule` es tabla intermedia.
- `UserScheduleSettings`: enforcement de horario y margen en minutos por usuario.
- Kioskos: `Kiosk` (propietario, cuenta de kiosko para login), `KioskUser` (usuario autorizado con `pin_hash` y flag `close_session_after_punch`). `CompanyInfo` almacena datos corporativos y `logo_path`.

## Flujo de autenticacion y roles
- Flask-Login con `UserMixin` en `User`; `login_manager.user_loader` lee por id.
- Decoradores: `admin_required` (solo admin), `kiosko_admin_required` (admin o kiosko_admin).
- Roles: `kiosko` solo accede al panel `/kiosko`; `empleado` usa dashboard normal; `kiosko_admin` administra kioskos propios; `admin` todo.

## Logica de fichaje y horarios (app_core/logic.py + routes/fichajes.py)
1) El usuario envia accion (`entrada`, `salida`, `descanso_inicio`, `descanso_fin`) con coordenadas.
2) Validaciones:
   - Secuencia correcta (`validar_secuencia_fichaje`) y que haya intervalo abierto para descansos.
   - Si `UserScheduleSettings.enforce_schedule` esta activo: comprueba que la hora actual este dentro del horario asignado mas margen.
   - Ubicacion: si no tiene `Flexible`, verifica que las coordenadas esten dentro de alguna `Location` asignada (`is_within_radius` -> `determinar_ubicacion_por_coordenadas`).
   - Descanso manual bloqueado si el horario del dia tiene descanso fijo.
3) Se crea `Registro` (accion + UTC ahora + lat/lon). En `salida`, calcula trabajado del dia vs jornada teorica (`calcular_jornada_teorica`) y exige `RegistroJustificacion` si hay extra sin motivo.
4) Agrupacion y calculos:
   - `agrupar_registros_en_intervalos` arma pares entrada/salida por usuario, resolviendo casos incompletos y cruces de medianoche.
   - `calcular_descanso_intervalo_para_usuario` y `calcular_descanso_intervalos` calculan descansos reales con `descanso_*`.
   - `calcular_extra_y_defecto_intervalo` calcula horas extra/defecto comparando con horario (global o por dias) y descansos (real vs teorico).
   - `obtener_trabajo_y_esperado_por_periodo` resume por dia/semana/mes.

## Panel usuario (routes/dashboard.py)
- Lista intervalos del usuario autenticado, muestra descansos en curso/consumidos, horas extra/defecto y total trabajado (`formatear_timedelta`).
- Habilita o bloquea botones de entrada/salida/descanso segun ultimo registro y configuracion de horario/descanso.
- Redirige cuentas `kiosko` al panel de kiosko.

## Modo kiosko (routes/kiosko.py y routes/fichajes.py)
- Las cuentas `kiosko` solo acceden a `/kiosko`: ven usuarios autorizados en ese kiosko (`KioskUser`), ultimos seleccionados via `session["kiosk_last_user_id"]`.
- Para fichar, la cuenta `kiosko` selecciona usuario y PIN; se valida con `KioskUser.pin_hash`. Luego se reutiliza el mismo flujo de validacion de ubicacion/horario descrito arriba.

## Administracion (routes/)
- Usuarios (`admin_users.py`): asigna ubicaciones (M2M), roles, borra usuarios (no si estan ligados a kioskos), resetea contrasena y flag `must_change_password`, ficha individual para horarios y ajustes (`UserScheduleSettings`).
- Ubicaciones (`admin_locations.py`): CRUD excepto `Flexible`; evita borrar si hay usuarios (modelo legado) ligados.
- Horarios (`admin_schedules.py`): crea/edita horarios simples o por dia con descansos fijos/flexibles; evita eliminar si esta asignado.
- Kioskos (`admin_kioskos.py`): admin ve todos; kiosko_admin solo propios. Configura propietario (solo admin), cuenta de kiosko, usuarios habilitados con PIN y flag de cierre de sesion tras fichar.
- Registros (`admin_registros.py`): filtros por usuario, periodo (rango, semana, mes, historico), ubicacion, modo de conteo; agrupa intervalos, calcula extra/defecto y descansos, exporta CSV/PDF, edita/crea intervalos con auditoria `RegistroEdicion` y descensos manuales.
- Empresa (`company.py`): administra `CompanyInfo`, sube logo a `static/uploads` (extensiones permitidas en `ALLOWED_LOGOS`).
- Healthcheck (`health.py`): `/health` responde 200 sin login.

## Reportes (app_core/reporting.py)
- Construye secciones por usuario desde intervalos (usa logica de calculo anterior), normaliza horas extra/defecto/descansos.
- Exporta:
  - `generar_csv`: separador `;`, adjunta trabajo/esperado/extra/defecto y filas por intervalo.
  - `generar_pdf`: via `flask_weasyprint`, plantilla `templates/informe_pdf.html`, incluye datos de empresa y ediciones (`RegistroEdicion`).

## Consideraciones para desarrollo
- No hay migraciones (Alembic). Al cambiar modelos, deberas manejar alteraciones de esquema manualmente.
- Todos los `momento` en BD son naive/UTC; usa `to_local` y `local_to_utc_naive` para UI/inputs.
- La ubicacion `Flexible` es reservada y se normaliza en `db_setup`; no permitir crear/editar/borrar desde UI.
- Si añades nuevos roles, rutas o acciones de fichaje, revisa:
  - Validaciones en `routes/fichajes.py`.
  - Calculos en `logic.py` (extra/defecto, descansos, ubicaciones).
  - Formularios/plantillas en `templates/`.
- Para reportes personalizados, extiende `app_core/reporting.py` y la plantilla `informe_pdf.html`.
