#!/usr/bin/env bash
set -euo pipefail

# MODE indica que operacion hacer:
#  - deploy          -> despliegue completo
#  - service_start   -> systemctl start servicio
#  - service_stop    -> systemctl stop servicio
#  - service_restart -> systemctl restart servicio
#  - reset_db        -> recrear BBDD (drop/create/grant + tablas basicas + admin)
#  - delete_db       -> hacer backup de la BBDD y luego borrarla
#  - delete          -> borrar instancia (systemd, nginx, directorio en /home)

MODE="${1:-deploy}"

echo "[*] Running deploy_fichaje_instance.sh in MODE='${MODE}'" >&2

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

valid_instance_name_re='^[a-zA-Z0-9_-]+$'
valid_db_re='^[a-zA-Z0-9_]+$'
valid_service_re='^[a-zA-Z0-9_.-]+\.service$'

TEMPLATE_APP_DIR="/home/fichaje/app"
INSTANCE_ROOT_BASE="/home"
# Directorio base para backups de BBDD (en la app de gestion)
BACKUP_BASE_DIR="/home/gestion/app/bbdd"

# ================================================================
#  MODOS SOLO DE SERVICIO: service_start / service_stop / restart
# ================================================================
if [[ "$MODE" == "service_start" || "$MODE" == "service_stop" || "$MODE" == "service_restart" ]]; then
  SYSTEMD_SERVICE_NAME="${2:-}"

  if [[ -z "$SYSTEMD_SERVICE_NAME" ]]; then
    fail "SYSTEMD_SERVICE_NAME requerido en modo ${MODE}"
  fi
  if ! [[ "$SYSTEMD_SERVICE_NAME" =~ $valid_service_re ]]; then
    fail "SYSTEMD_SERVICE_NAME invalido"
  fi

  case "$MODE" in
    service_start)
      echo "[*] systemctl start ${SYSTEMD_SERVICE_NAME}..." >&2
      systemctl start "$SYSTEMD_SERVICE_NAME"
      ;;
    service_stop)
      echo "[*] systemctl stop ${SYSTEMD_SERVICE_NAME}..." >&2
      systemctl stop "$SYSTEMD_SERVICE_NAME"
      ;;
    service_restart)
      echo "[*] systemctl restart ${SYSTEMD_SERVICE_NAME}..." >&2
      systemctl restart "$SYSTEMD_SERVICE_NAME"
      ;;
  esac

  echo "[OK] systemctl ${MODE#service_} ${SYSTEMD_SERVICE_NAME} completado" >&2
  exit 0
fi

# ===========================
#  MODO reset_db
#   reset_db SERVICE_NAME DB_NAME DB_USER [BACKUP_FILE]
# ===========================
if [[ "$MODE" == "reset_db" ]]; then
  SYSTEMD_SERVICE_NAME="${2:-}"
  DB_NAME="${3:-}"
  DB_USER="${4:-}"
  BACKUP_FILE="${5:-}"

  if [[ -z "$SYSTEMD_SERVICE_NAME" || -z "$DB_NAME" || -z "$DB_USER" ]]; then
    fail "SYSTEMD_SERVICE_NAME, DB_NAME y DB_USER requeridos en modo reset_db"
  fi
  if ! [[ "$SYSTEMD_SERVICE_NAME" =~ $valid_service_re ]]; then
    fail "SYSTEMD_SERVICE_NAME invalido"
  fi
  if ! [[ "$DB_NAME" =~ $valid_db_re ]]; then
    fail "DB_NAME invalido"
  fi
  if ! [[ "$DB_USER" =~ $valid_db_re ]]; then
    fail "DB_USER invalido"
  fi

  if [[ -n "$BACKUP_FILE" ]]; then
    if [[ ! -f "$BACKUP_FILE" ]]; then
      fail "BACKUP_FILE no existe: ${BACKUP_FILE}"
    fi
  fi

  echo "[*] Reseteando BBDD '${DB_NAME}'..." >&2

  echo "[*] Parando servicio ${SYSTEMD_SERVICE_NAME}..." >&2
  systemctl stop "${SYSTEMD_SERVICE_NAME}" || true

  sudo -u postgres psql -c "DROP DATABASE IF EXISTS \"${DB_NAME}\";"
  sudo -u postgres psql -c "CREATE DATABASE \"${DB_NAME}\";"

  if [[ -n "$BACKUP_FILE" ]]; then
    echo "[*] Restaurando backup desde '${BACKUP_FILE}'..." >&2
    sudo -u postgres psql -d "${DB_NAME}" -f "${BACKUP_FILE}"
  fi

  sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE \"${DB_NAME}\" TO \"${DB_USER}\";"

  echo "[*] Arrancando de nuevo servicio ${SYSTEMD_SERVICE_NAME}..." >&2
  systemctl restart "${SYSTEMD_SERVICE_NAME}"

  echo "[OK] BBDD '${DB_NAME}' reseteada y servicio reiniciado." >&2
  exit 0
fi


# ===========================
#  MODO delete_db (backup + drop)
# ===========================
if [[ "$MODE" == "delete_db" ]]; then
  DB_NAME="${2:-}"
  DB_USER="${3:-}"
  INSTANCE_NAME="${4:-}"
  SYSTEMD_SERVICE_NAME="${5:-}"

  if [[ -z "$DB_NAME" || -z "$INSTANCE_NAME" ]]; then
    fail "DB_NAME e INSTANCE_NAME son requeridos en modo delete_db"
  fi
  if ! [[ "$DB_NAME" =~ $valid_db_re ]]; then
    fail "DB_NAME invalido"
  fi
  if [[ -n "$DB_USER" ]] && ! [[ "$DB_USER" =~ $valid_db_re ]]; then
    fail "DB_USER invalido"
  fi
  if ! [[ "$INSTANCE_NAME" =~ $valid_instance_name_re ]]; then
    fail "INSTANCE_NAME invalido"
  fi
  if [[ -n "$SYSTEMD_SERVICE_NAME" ]] && ! [[ "$SYSTEMD_SERVICE_NAME" =~ $valid_service_re ]]; then
    fail "SYSTEMD_SERVICE_NAME invalido en modo delete_db"
  fi

  # Si tenemos servicio, lo paramos para que no siga conectado a la BBDD
  if [[ -n "$SYSTEMD_SERVICE_NAME" ]]; then
    echo "[*] Parando servicio ${SYSTEMD_SERVICE_NAME} antes de borrar la BBDD..." >&2
    systemctl stop "${SYSTEMD_SERVICE_NAME}" || echo "Aviso: no se pudo parar ${SYSTEMD_SERVICE_NAME}" >&2
  fi

  # Creamos estructura de backup: /home/gestion/app/bbdd/<INSTANCE_NAME>/
  mkdir -p "${BACKUP_BASE_DIR}"
  INSTANCE_BACKUP_DIR="${BACKUP_BASE_DIR}/${INSTANCE_NAME}"
  mkdir -p "${INSTANCE_BACKUP_DIR}"

  TS="$(date +%Y%m%d_%H%M%S)"
  BACKUP_FILE="${INSTANCE_BACKUP_DIR}/${DB_NAME}_${TS}.sql"

  if [[ -e "$BACKUP_FILE" ]]; then
    BACKUP_FILE="${INSTANCE_BACKUP_DIR}/${DB_NAME}_${TS}_$$.sql"
  fi

  LOG_FILE="${INSTANCE_BACKUP_DIR}/delete.log"

  echo "[*] Generando backup de la BBDD '${DB_NAME}' en ${BACKUP_FILE}..." >&2
  sudo -u postgres pg_dump "${DB_NAME}" > "${BACKUP_FILE}"

  echo "[*] Terminando conexiones activas a '${DB_NAME}' antes del DROP..." >&2
  sudo -u postgres psql <<EOF
SELECT pg_terminate_backend(pid)
FROM pg_stat_activity
WHERE datname = '${DB_NAME}'
  AND pid <> pg_backend_pid();
EOF

  echo "[*] Eliminando BBDD '${DB_NAME}'..." >&2
  sudo -u postgres psql -c "DROP DATABASE IF EXISTS \"${DB_NAME}\";"

  # Apuntar en log (no se sobreescribe, se añade al final)
  {
    echo "[$(date -Is)] Deleted DB '${DB_NAME}' for instance '${INSTANCE_NAME}'. Backup file: $(basename "${BACKUP_FILE}")"
  } >> "${LOG_FILE}"

  echo "[OK] BBDD '${DB_NAME}' borrada y backup almacenado en ${BACKUP_FILE}" >&2
  exit 0
fi

# ===========================
#  MODO delete (instancia)
# ===========================
if [[ "$MODE" == "delete" ]]; then
  INSTANCE_NAME="${2:-}"
  SYSTEMD_SERVICE_NAME="${3:-}"

  if [[ -z "$INSTANCE_NAME" || -z "$SYSTEMD_SERVICE_NAME" ]]; then
    fail "INSTANCE_NAME y SYSTEMD_SERVICE_NAME requeridos en modo delete"
  fi
  if ! [[ "$INSTANCE_NAME" =~ $valid_instance_name_re ]]; then
    fail "INSTANCE_NAME invalido"
  fi
  if ! [[ "$SYSTEMD_SERVICE_NAME" =~ $valid_service_re ]]; then
    fail "SYSTEMD_SERVICE_NAME invalido"
  fi

  INSTANCE_ROOT="${INSTANCE_ROOT_BASE}/${INSTANCE_NAME}"
  NGINX_AVAILABLE="/etc/nginx/sites-available/${INSTANCE_NAME}"
  NGINX_ENABLED="/etc/nginx/sites-enabled/${INSTANCE_NAME}"
  SYSTEMD_UNIT="/etc/systemd/system/${SYSTEMD_SERVICE_NAME}"

  echo "[*] Parando y deshabilitando servicio ${SYSTEMD_SERVICE_NAME}..." >&2
  systemctl stop "${SYSTEMD_SERVICE_NAME}" 2>/dev/null || true
  systemctl disable "${SYSTEMD_SERVICE_NAME}" 2>/dev/null || true

  if [ -f "${SYSTEMD_UNIT}" ]; then
    echo "[*] Eliminando unidad systemd ${SYSTEMD_UNIT}..." >&2
    rm -f "${SYSTEMD_UNIT}"
    systemctl daemon-reload
  fi

  echo "[*] Eliminando configuracion Nginx para ${INSTANCE_NAME}..." >&2
  rm -f "${NGINX_ENABLED}" || true
  rm -f "${NGINX_AVAILABLE}" || true
  nginx -t && systemctl reload nginx || echo "Aviso: Nginx no recargado correctamente" >&2

  echo "[*] Eliminando directorio de instancia ${INSTANCE_ROOT}..." >&2
  rm -rf "${INSTANCE_ROOT}"

  # Opcional: limpiar posibles logs especificos
  rm -rf "/var/log/${INSTANCE_NAME}" 2>/dev/null || true
  rm -rf "/var/log/${SYSTEMD_SERVICE_NAME%.service}" 2>/dev/null || true

  echo "[OK] Instancia ${INSTANCE_NAME} eliminada." >&2
  exit 0
fi

# ===========================
#  MODO deploy (por defecto)
# ===========================
if [[ "$MODE" != "deploy" ]]; then
  fail "MODE desconocido: ${MODE}"
fi

# Parametros por posicion:
#   1: deploy
#   2: INSTANCE_NAME
#   3: APP_PORT
#   4: NGINX_SERVER_NAME
#   5: DB_NAME
#   6: DB_USER
#   7: DB_PASSWORD
#   8: SYSTEMD_SERVICE_NAME

INSTANCE_NAME="${2:-}"
APP_PORT="${3:-}"
NGINX_SERVER_NAME="${4:-}"
DB_NAME="${5:-}"
DB_USER="${6:-}"
DB_PASSWORD="${7:-}"
SYSTEMD_SERVICE_NAME="${8:-}"

# === Validacion de requeridos para deploy ===
if [[ -z "$INSTANCE_NAME" || -z "$APP_PORT" || -z "$NGINX_SERVER_NAME" || -z "$DB_NAME" || -z "$DB_USER" || -z "$DB_PASSWORD" || -z "$SYSTEMD_SERVICE_NAME" ]]; then
  fail "Parametros insuficientes para modo deploy"
fi

if ! [[ "$INSTANCE_NAME" =~ $valid_instance_name_re ]]; then
  fail "INSTANCE_NAME invalido"
fi

if ! [[ "$APP_PORT" =~ ^[0-9]+$ ]] || [ "$APP_PORT" -lt 1024 ] || [ "$APP_PORT" -gt 65535 ]; then
  fail "APP_PORT invalido"
fi

if ! [[ "$DB_NAME" =~ $valid_db_re ]]; then
  fail "DB_NAME invalido"
fi

if ! [[ "$DB_USER" =~ $valid_db_re ]]; then
  fail "DB_USER invalido"
fi

if [[ "$DB_PASSWORD" == *"'"* ]]; then
  fail "DB_PASSWORD no puede contener comillas simples"
fi

if ! [[ "$SYSTEMD_SERVICE_NAME" =~ $valid_service_re ]]; then
  fail "SYSTEMD_SERVICE_NAME invalido"
fi

if [[ "$NGINX_SERVER_NAME" == *" "* ]] || [[ "$NGINX_SERVER_NAME" == http* ]]; then
  fail "NGINX_SERVER_NAME invalido"
fi

INSTANCE_ROOT="${INSTANCE_ROOT_BASE}/${INSTANCE_NAME}"
INSTANCE_APP_DIR="${INSTANCE_ROOT}/app"
VENV_DIR="${INSTANCE_APP_DIR}/venv"
GUNICORN_EXEC="${VENV_DIR}/bin/gunicorn"

NGINX_AVAILABLE="/etc/nginx/sites-available/${INSTANCE_NAME}"
NGINX_ENABLED="/etc/nginx/sites-enabled/${INSTANCE_NAME}"
SYSTEMD_UNIT="/etc/systemd/system/${SYSTEMD_SERVICE_NAME}"

if [ ! -d "$TEMPLATE_APP_DIR" ]; then
  fail "Directorio plantilla $TEMPLATE_APP_DIR no existe"
fi

if [ -e "$INSTANCE_ROOT" ]; then
  fail "Ya existe ${INSTANCE_ROOT}. No se continua para evitar sobrescribir."
fi

if [ -e "$SYSTEMD_UNIT" ]; then
  fail "Ya existe unidad systemd ${SYSTEMD_UNIT}"
fi

if [ -e "$NGINX_AVAILABLE" ] || [ -e "$NGINX_ENABLED" ]; then
  fail "Ya existe configuracion Nginx para ${INSTANCE_NAME}"
fi

echo "[*] Creando directorio de instancia en ${INSTANCE_ROOT}..." >&2
mkdir -p "$INSTANCE_ROOT"
mkdir -p "$INSTANCE_APP_DIR"

echo "[*] Copiando codigo de plantilla desde ${TEMPLATE_APP_DIR}..." >&2
rsync -a --exclude 'venv' "${TEMPLATE_APP_DIR}/" "${INSTANCE_APP_DIR}/"

echo "[*] Ajustando permisos para usuario fichaje..." >&2
chown -R fichaje:fichaje "$INSTANCE_ROOT"

echo "[*] Creando base de datos y usuario en Postgres (si no existen)..." >&2
sudo -u postgres psql -c "CREATE DATABASE \"${DB_NAME}\";" 2>/dev/null || echo "Aviso: la base de datos ya existia" >&2
sudo -u postgres psql -c "CREATE USER \"${DB_USER}\" WITH ENCRYPTED PASSWORD '${DB_PASSWORD}';" 2>/dev/null || echo "Aviso: el usuario ya existia" >&2
sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE \"${DB_NAME}\" TO \"${DB_USER}\";"

echo "[*] Creando entorno virtual e instalando requirements..." >&2
sudo -u fichaje bash -c "python3 -m venv '${VENV_DIR}'"
sudo -u fichaje bash -c "'${VENV_DIR}/bin/pip' install --upgrade pip"
sudo -u fichaje bash -c "'${VENV_DIR}/bin/pip' install -r '${INSTANCE_APP_DIR}/requirements.txt'"

echo "[*] Creando unidad systemd ${SYSTEMD_UNIT}..." >&2
cat > "$SYSTEMD_UNIT" <<EOF
[Unit]
Description=Instancia fichaje ${INSTANCE_NAME}
After=network.target

[Service]
User=fichaje
Group=fichaje
WorkingDirectory=${INSTANCE_APP_DIR}
Environment=PATH=${VENV_DIR}/bin
Environment=DATABASE_URL=postgresql+psycopg2://${DB_USER}:${DB_PASSWORD}@127.0.0.1:5432/${DB_NAME}
ExecStart=${GUNICORN_EXEC} -w 3 -b 127.0.0.1:${APP_PORT} app:app
Restart=on-failure

[Install]
WantedBy=multi-user.target
EOF

echo "[*] Recargando systemd y arrancando servicio..." >&2
systemctl daemon-reload
systemctl enable "${SYSTEMD_SERVICE_NAME}"
systemctl restart "${SYSTEMD_SERVICE_NAME}"

echo "[*] Creando configuracion Nginx para ${NGINX_SERVER_NAME}..." >&2
cat > "$NGINX_AVAILABLE" <<EOF
server {
    listen 80;
    server_name ${NGINX_SERVER_NAME};

    location / {
        proxy_pass http://127.0.0.1:${APP_PORT};
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
EOF

ln -s "$NGINX_AVAILABLE" "$NGINX_ENABLED"

echo "[*] Probando configuracion Nginx..." >&2
nginx -t

echo "[*] Recargando Nginx..." >&2
systemctl reload nginx

echo "[OK] Despliegue completado para instancia ${INSTANCE_NAME}" >&2
 
