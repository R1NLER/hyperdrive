# HyperDrive 🚀

**HyperDrive** es una interfaz web moderna y ligera para gestionar discos duros y particiones en servidores Linux. Diseñada para facilitar tareas de almacenamiento sin necesidad de tocar la terminal.

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
    ```

2.  **Ejecuta el instalador:**
    ```bash
    sudo ./scripts/install.sh
    ```
    *El script instalará las dependencias, creará un entorno virtual y configurará el servicio systemd.*

3.  **Accede a la web:**
    Abre tu navegador y ve a: `http://<IP-DE-TU-SERVIDOR>:8090`

## 📖 Guía de Uso

### 1. Panel Principal (Dashboard)
Vista general del estado del sistema. Muestra alertas si hay discos configurados que faltan y un resumen de los volúmenes montados con su uso de espacio en tiempo real.

### 2. Gestión de Discos
En la sección "Discos" puedes ver todos los dispositivos físicos conectados.

*   **Montar:** Hace accesible una partición. Te pedirá un nombre para crear la carpeta en `/mnt/nombre`. Al hacerlo, el disco se vuelve **persistente** (se montará solo al reiniciar).
*   **Desmontar:** Libera el disco y **borra su configuración** de persistencia y Samba. Úsalo si quieres quitar el disco para siempre.
*   **Desconectar:** Desmonta el disco pero **mantiene su configuración** (punto de montaje y Samba) guardada. Ideal si vas a apagar el disco un momento y volverlo a encender luego, o para extracción segura temporal.
*   **Reconectar:** Vuelve a montar un disco que estaba "Desconectado" o que se ha detectado de nuevo tras un reinicio, recuperando su configuración previa automáticamente.
*   **Formatear:** Borra todos los datos y crea un nuevo sistema de archivos (ext4, ntfs, etc.). *Solo disponible si el disco está desmontado y sin configuración.*

### 3. Persistencia (Fstab)
HyperDrive gestiona automáticamente el archivo `/etc/fstab` para asegurar que tus discos sobrevivan a los reinicios.
*   La sección **Fstab** de la web te ofrece una **vista de solo lectura** de este archivo.
*   Es útil para verificar qué discos están configurados para arrancar con el sistema y detectar posibles errores o UUIDs huérfanos.

### 4. Compartir en Red (Samba)
Puedes compartir cualquier disco montado con la red local (Windows/Mac/Linux) sin editar archivos de configuración.
*   Ve a la pestaña **Samba**.
*   Activa el interruptor "Compartir" en el disco deseado.
*   Opcionalmente, puedes hacerlo "Público" (sin contraseña) o "Solo lectura".
*   HyperDrive se encarga de reconfigurar Samba y reiniciar el servicio por ti de forma transparente.

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
