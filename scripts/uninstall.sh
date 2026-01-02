#!/usr/bin/env bash
set -e

APP_NAME="hyperdrive"
INSTALL_DIR="/opt/$APP_NAME"
SERVICE_FILE="/etc/systemd/system/$APP_NAME.service"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

echo -e "${RED}=== Desinstalador de HyperDrive ===${NC}"

if [[ $EUID -ne 0 ]]; then
   echo "Este script debe ejecutarse como root (sudo)." 
   exit 1
fi

echo "Deteniendo servicio..."
systemctl stop "$APP_NAME" || true
systemctl disable "$APP_NAME" || true

if [ -f "$SERVICE_FILE" ]; then
    echo "Eliminando servicio..."
    rm "$SERVICE_FILE"
    systemctl daemon-reload
fi

if [ -d "$INSTALL_DIR" ]; then
    echo "Eliminando archivos..."
    rm -rf "$INSTALL_DIR"
fi

echo -e "${GREEN}=== Desinstalación completada ===${NC}"
