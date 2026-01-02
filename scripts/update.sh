#!/usr/bin/env bash
set -e

# Configuración
APP_NAME="hyperdrive"
INSTALL_DIR="/opt/$APP_NAME"
SERVICE_FILE="/etc/systemd/system/$APP_NAME.service"
USER="root"

# Colores
GREEN='\033[0;32m'
RED='\033[0;31m'
NC='\033[0m'

# Determinar rutas relativas
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# 1. Verificar permisos de root
if [[ $EUID -ne 0 ]]; then
   echo "Este script debe ejecutarse como root (sudo)." 
   exit 1
fi

# 1.1 Evitar ejecución desde /opt
if [[ "$PROJECT_ROOT" == "$INSTALL_DIR" ]]; then
    echo -e "${RED}Error:${NC} Estás ejecutando este script desde la carpeta de instalación ($INSTALL_DIR)."
    echo "Debes ejecutarlo desde tu carpeta de código fuente (el repositorio clonado)."
    echo "Ejemplo: cd ~/hyperdrive && sudo ./scripts/update.sh"
    exit 1
fi

# 1.5 Actualizar desde GitHub si es un repositorio
if [ -d "$PROJECT_ROOT/.git" ]; then
    REPO_URL=$(git -C "$PROJECT_ROOT" remote get-url origin 2>/dev/null || echo "desconocido")
    echo "Repositorio Git detectado ($REPO_URL). Buscando actualizaciones..."
    
    # Intentar actualizar como el usuario original para evitar problemas de permisos/SSH
    if [ -n "$SUDO_USER" ]; then
        if ! sudo -u "$SUDO_USER" git -C "$PROJECT_ROOT" pull; then
            echo "⚠️  No se pudo actualizar desde GitHub (¿conflictos locales?). Usando versión local actual."
        fi
    else
        git -C "$PROJECT_ROOT" pull || echo "⚠️  No se pudo actualizar desde GitHub. Usando versión local actual."
    fi
fi

echo "Sincronizando archivos desde $PROJECT_ROOT a $INSTALL_DIR..."

# 2. Copiar archivos nuevos (sobrescribiendo)
rsync -av --exclude='.venv' --exclude='__pycache__' --exclude='.git' --exclude='scripts' "$PROJECT_ROOT/" "$INSTALL_DIR/"

# Limpiar carpeta scripts antigua si existe en destino
if [ -d "$INSTALL_DIR/scripts" ]; then
    rm -rf "$INSTALL_DIR/scripts"
fi

# Copiar solo el desinstalador actualizado
cp "$SCRIPT_DIR/uninstall.sh" "$INSTALL_DIR/"

# 3. Actualizar dependencias
echo "Verificando dependencias..."
"$INSTALL_DIR/.venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt"

# 3.5 Actualizar definición del servicio (por si cambiamos a Gunicorn, etc.)
echo "Actualizando configuración del servicio..."
cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=HyperDrive Disk Manager
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/.venv/bin/gunicorn --workers 3 --bind 0.0.0.0:8090 --access-logfile - --error-logfile - app:app
Restart=always
RestartSec=5
Environment=FLASK_APP=app.py
Environment=FLASK_ENV=production

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload

# 4. Reiniciar servicio
echo "Reiniciando servicio..."
systemctl restart "$APP_NAME"

echo -e "${GREEN}=== Actualización completada ===${NC}"
