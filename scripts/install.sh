#!/usr/bin/env bash
set -e

# Configuración
APP_NAME="hyperdrive"
INSTALL_DIR="/opt/$APP_NAME"
SERVICE_FILE="/etc/systemd/system/$APP_NAME.service"
USER="root"

# Colores
GREEN='\033[0;32m'
NC='\033[0m'

# Determinar rutas relativas
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

echo -e "${GREEN}=== Instalador de HyperDrive ===${NC}"

# 1. Verificar permisos de root
if [[ $EUID -ne 0 ]]; then
   echo "Este script debe ejecutarse como root (sudo)." 
   exit 1
fi

# 2. Verificar dependencias
if ! command -v rsync &> /dev/null; then
    echo "Instalando rsync..."
    apt-get update && apt-get install -y rsync
fi

# 3. Crear directorio de instalación
echo "Instalando en $INSTALL_DIR..."
mkdir -p "$INSTALL_DIR"

# 4. Copiar archivos desde la raíz del proyecto
echo "Copiando archivos..."
rsync -av --exclude='.venv' --exclude='__pycache__' --exclude='.git' --exclude='scripts' "$PROJECT_ROOT/" "$INSTALL_DIR/"

# Copiar solo el desinstalador para emergencias
cp "$SCRIPT_DIR/uninstall.sh" "$INSTALL_DIR/"

# 5. Crear entorno virtual
echo "Configurando entorno Python..."
if [ ! -d "$INSTALL_DIR/.venv" ]; then
    python3 -m venv "$INSTALL_DIR/.venv"
fi

# 6. Instalar dependencias
echo "Instalando dependencias..."
"$INSTALL_DIR/.venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt"

# 7. Crear servicio Systemd
echo "Creando servicio Systemd..."
cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=HyperDrive Disk Manager
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/.venv/bin/flask --app app run --host 0.0.0.0 --port 8090 --no-debugger --no-reload
Restart=always
RestartSec=5
Environment=FLASK_APP=app.py
Environment=FLASK_ENV=production

[Install]
WantedBy=multi-user.target
EOF

# 8. Activar servicio
echo "Activando servicio..."
systemctl daemon-reload
systemctl enable "$APP_NAME"
systemctl restart "$APP_NAME"

echo -e "${GREEN}=== Instalación completada ===${NC}"
echo "Accede a la web en: http://$(hostname -I | awk '{print $1}'):8090"
