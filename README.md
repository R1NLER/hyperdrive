# HyperDrive 🚀

**HyperDrive** es una interfaz web moderna y ligera para gestionar discos duros y particiones en servidores Linux. Diseñada para facilitar tareas de almacenamiento sin necesidad de tocar la terminal.

![HyperDrive UI](https://via.placeholder.com/800x400?text=HyperDrive+Dashboard+Preview)

## ✨ Características

*   **🔌 Gestión de Discos:** Monta y desmonta particiones con un solo clic.
*   **💾 Persistencia:** Configura montajes automáticos al inicio (`/etc/fstab`) fácilmente.
*   **📂 Samba Integrado:** Comparte tus discos en red local directamente desde la interfaz.
*   **🛠️ Formateo:** Herramienta visual para formatear discos (ext4, ntfs, exfat, etc.).
*   **📊 Monitoreo:** Visualización de espacio usado/libre y estado de los discos en tiempo real.
*   **🌑 Modo Oscuro:** Interfaz moderna y agradable basada en Bootstrap 5.
*   **🔄 Auto-Discovery:** Detecta automáticamente nuevos dispositivos conectados (USB/SATA).

## 🚀 Instalación

HyperDrive está diseñado para funcionar como un servicio del sistema en Ubuntu/Debian.

1.  **Clona el repositorio:**
    ```bash
    git clone https://github.com/r1nler/hyperdrive.git
    cd hyperdrive
    sudo chmod +x ./scripts/*
    ```

2.  **Ejecuta el instalador:**
    ```bash
    sudo ./scripts/install.sh
    ```
    *El script instalará las dependencias, creará un entorno virtual y configurará el servicio systemd.*

3.  **Accede a la web:**
    Abre tu navegador y ve a: `http://<IP-DE-TU-SERVIDOR>:8090`

## ⚙️ Gestión del Servicio

Una vez instalado, HyperDrive funciona como cualquier servicio de Linux:

*   **Ver estado:** `sudo systemctl status hyperdrive`
*   **Reiniciar:** `sudo systemctl restart hyperdrive`
*   **Ver logs:** `sudo journalctl -u hyperdrive -f`

### Actualización
Para descargar la última versión desde GitHub y aplicarla automáticamente:
```bash
sudo ./scripts/update.sh
```

### Desinstalación
Para eliminar la aplicación y limpiar el sistema:
```bash
sudo ./scripts/uninstall.sh
```

## 📋 Requisitos

*   Linux (Probado en Ubuntu/Debian)
*   Python 3.8+
*   Permisos de `root` (necesarios para montar/desmontar discos y configurar Samba).
*   Paquetes del sistema (se instalan automáticamente si faltan): `rsync`, `ntfs-3g` (opcional para NTFS).

## 🔒 Seguridad

La aplicación se ejecuta con privilegios elevados para poder gestionar el hardware. Se recomienda:
*   Usarla solo en redes locales confiables (LAN).
*   No exponer el puerto 8090 directamente a Internet.

---
*Creado con ❤️ para simplificar la vida del sysadmin.*
