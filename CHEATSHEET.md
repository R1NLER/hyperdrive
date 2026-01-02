# Comandos de Control de HyperDrive

Al instalar la aplicación como servicio, puedes usar los comandos nativos de Linux para controlarla:

## Control Básico
- **Reiniciar:** `sudo systemctl restart hyperdrive`
  *(Úsalo si la app se queda pillada o tras actualizar)*
- **Iniciar:** `sudo systemctl start hyperdrive`
- **Detener:** `sudo systemctl stop hyperdrive`
- **Ver estado:** `sudo systemctl status hyperdrive`

## Logs y Depuración
- **Ver logs en tiempo real:** `sudo journalctl -u hyperdrive -f`
- **Ver últimos 50 logs:** `sudo journalctl -u hyperdrive -n 50`

## Arranque Automático
- **Activar inicio con el sistema:** `sudo systemctl enable hyperdrive`
- **Desactivar inicio con el sistema:** `sudo systemctl disable hyperdrive`
