from __future__ import annotations

import json
import math
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass
from typing import Any

from flask import Flask, jsonify, redirect, render_template, request, url_for

app = Flask(__name__)


@app.context_processor
def _inject_static_versions() -> dict[str, int]:
    """Cache-busting for /static assets.

    Mobile browsers often cache aggressively; adding a version query param makes UI updates visible.
    """

    def mtime(rel_path: str) -> int:
        try:
            base_dir = os.path.dirname(__file__)
            abs_path = os.path.join(base_dir, rel_path)
            return int(os.path.getmtime(abs_path))
        except Exception:
            return int(time.time())

    return {
        "static_v_css": mtime("static/css/app.css"),
        "static_v_js": mtime("static/js/app.js"),
    }


DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9]+$")
UUID_RE = re.compile(r"^[A-Fa-f0-9-]+$")

# Solo mostramos como “gestionables” los montajes típicos de discos de usuario.
ALLOWED_MOUNT_PREFIXES = ("/mnt/", "/media/")

# Montajes/paths del sistema que no deberían aparecer en el gestor.
SYSTEM_MOUNTPOINTS = {"/", "/boot", "/boot/efi"}
SYSTEM_PREFIXES = ("/proc", "/sys", "/dev", "/run", "/snap", "/var/lib/snapd")


def _parse_size_to_bytes(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        if s.isdigit():
            try:
                n = int(s, 10)
            except ValueError:
                return None
            return n if n >= 0 else None
        m = re.match(r"^(\d+(?:\.\d+)?)([KMGTP]?)B?$", s, re.IGNORECASE)
        if not m:
            return None
        num = float(m.group(1))
        unit = (m.group(2) or "").upper()
        mul = {
            "": 1,
            "K": 1024,
            "M": 1024**2,
            "G": 1024**3,
            "T": 1024**4,
            "P": 1024**5,
        }.get(unit)
        if mul is None:
            return None
        out = int(num * mul)
        return out if out >= 0 else None
    return None


def _is_noise_partition(p: dict[str, Any]) -> bool:
    # Evita mostrar particiones pequeñas “técnicas” (p.ej. MSR de Windows ~16MB)
    # que suelen venir sin UUID y sin fstype y no son gestionables desde esta UI.
    if (p.get("type") or "") != "part":
        return False
    fstype = (p.get("fstype") or "").strip().lower()
    uuid = (p.get("uuid") or "").strip()
    if fstype or uuid:
        return False
    size_b = _parse_size_to_bytes(p.get("size"))
    if size_b is None:
        return False
    return size_b <= 64 * 1024 * 1024


def _root_physical_disk(parts: list[dict[str, Any]]) -> str | None:
    """Devuelve el nombre del disco físico que contiene '/'.

    Soporta casos donde '/' está sobre LVM/crypt: resolvemos la cadena PKNAME
    hasta llegar al disco superior (p.ej. ubuntu-lv -> sda3 -> sda).
    """

    root_name: str | None = None
    for p in parts:
        if (p.get("mountpoint") or "") == "/":
            root_name = p.get("name") or None
            break
    if not root_name:
        return None

    name_to_parent: dict[str, str] = {}
    for p in parts:
        n = (p.get("name") or "").strip()
        parent = (p.get("pkname") or "").strip()
        if n and parent:
            name_to_parent[n] = parent

    cur = root_name
    seen: set[str] = set()
    while True:
        parent = name_to_parent.get(cur)
        if not parent:
            return cur
        if parent in seen:
            return parent
        seen.add(parent)
        cur = parent


def _run(args: list[str], *, timeout_s: int = 10) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        # Devolvemos un CompletedProcess “sintético” para que el caller pueda
        # manejar el timeout sin que Flask devuelva un HTML 500.
        stdout = e.stdout or ""
        stderr = e.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")

        detail = f"Timeout tras {timeout_s}s: {' '.join(args)}"
        merged = (stderr or "").strip()
        if merged:
            detail = f"{detail}\n{merged}"
        return subprocess.CompletedProcess(args=args, returncode=124, stdout=stdout, stderr=detail)


def _kernel_filesystems() -> set[str]:
    fs: set[str] = set()
    try:
        with open("/proc/filesystems", "r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                line = raw.strip()
                if not line:
                    continue
                # Formato típico: "nodev\tproc" o "ext4"
                parts = line.split()
                fs.add(parts[-1])
    except Exception:
        return set()
    return fs


def _try_modprobe(module: str) -> None:
    # Best-effort: si no existe modprobe o no hay permisos, no hacemos nada.
    if not module:
        return
    if not shutil.which("modprobe"):
        return
    _run(["modprobe", module], timeout_s=8)


def _mount_ntfs(dev_path: str, mountpoint: str) -> subprocess.CompletedProcess:
    """Monta NTFS intentando el driver disponible en el sistema.

    Orden:
    1) ntfs3 (kernel) si está disponible (o tras modprobe)
    2) ntfs-3g si existe mount.ntfs-3g
    3) fallback: mount auto
    """

    uid, gid = _default_uid_gid()
    options = f"defaults,nofail,uid={uid},gid={gid}"

    fs = _kernel_filesystems()
    if "ntfs3" not in fs:
        _try_modprobe("ntfs3")
        fs = _kernel_filesystems()
    if "ntfs3" in fs:
        cp = _run(["mount", "-t", "ntfs3", "-o", options, dev_path, mountpoint], timeout_s=25)
        if cp.returncode == 0:
            return cp

    # ntfs-3g depende del helper mount.ntfs-3g (paquete ntfs-3g)
    if shutil.which("mount.ntfs-3g"):
        cp = _run(["mount", "-t", "ntfs-3g", "-o", options, dev_path, mountpoint], timeout_s=25)
        if cp.returncode == 0:
            return cp

    return _run(["mount", "-o", options, dev_path, mountpoint], timeout_s=25)


def _truncate(text: str, *, limit: int = 1200) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n…(truncado)"


def _require_root() -> tuple[bool, str]:
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        return (
            False,
            "Permiso denegado: ejecuta la app como root (ej: sudo python app.py) para montar/desmontar o editar /etc/fstab/Samba.",
        )
    return True, ""


def _safe_share_name(text: str) -> str:
    text = (text or "").strip().lower()
    text = re.sub(r"\s+", "_", text)
    text = re.sub(r"[^a-z0-9._-]", "", text)
    return text or "share"


def _safe_mount_dir(text: str) -> str:
    # Para evitar sorpresas, limitamos a un nombre de carpeta simple.
    text = (text or "").strip().lower()
    text = re.sub(r"\s+", "_", text)
    text = re.sub(r"[^a-z0-9._-]", "", text)
    return text or "disk"


def _looks_safe_mountpoint(path: str) -> bool:
    # No montamos fuera de estos prefijos por seguridad.
    return bool(path) and (path.startswith("/mnt/") or path.startswith("/media/"))


def _is_system_mountpoint(path: str) -> bool:
    if not path:
        return False
    if path in SYSTEM_MOUNTPOINTS:
        return True
    return any(path == p or path.startswith(p + "/") for p in SYSTEM_PREFIXES)


def _is_user_mountpoint(path: str) -> bool:
    return bool(path) and any(path.startswith(p) for p in ALLOWED_MOUNT_PREFIXES)


def _default_uid_gid() -> tuple[int, int]:
    """UID/GID por defecto para sistemas de ficheros tipo Windows (NTFS/exFAT).

    Para un servidor casero, mantenemos un comportamiento simple y compatible:
    - uid: el usuario que invoca sudo (SUDO_UID) o 1000.
    - gid: fijo a 1 (como el usuario lo usa en fstab).
    """

    def to_int(v: str | None) -> int | None:
        if v is None:
            return None
        v = v.strip()
        if not v:
            return None
        try:
            n = int(v, 10)
        except ValueError:
            return None
        if n < 0:
            return None
        return n

    uid = to_int(os.environ.get("SUDO_UID"))
    return (uid if uid is not None else 1000, 1)


def _default_server_user() -> str:
    # Cuando la app corre como root vía sudo, queremos el usuario real del servidor.
    # (root no es un usuario útil para Samba.)
    u = (os.environ.get("SUDO_USER") or os.environ.get("USER") or "").strip()
    return u or "root"


def _cleanup_mount_dir(path: str) -> None:
    # Solo limpiamos directorios de montaje “de usuario” y solo si están vacíos.
    # Si no está vacío o hay error, no hacemos nada.
    if not _looks_safe_mountpoint(path):
        return
    try:
        if os.path.isdir(path) and not os.listdir(path):
            os.rmdir(path)
    except Exception:
        return


def _remove_diskmanager_fstab_block(lines: list[str], entry_idx: int) -> None:
    """Elimina una entrada de fstab y limpia el comentario '# diskmanager' huérfano.

    La app inserta:
      \n# diskmanager\n<entry>\n
    Así que al borrar la entry también borramos el comentario y, si existe, el \n previo.
    """
    if entry_idx < 0 or entry_idx >= len(lines):
        return

    # Primero, borramos la entry.
    del lines[entry_idx]

    # Luego intentamos borrar el comentario anterior.
    comment_idx = entry_idx - 1
    if 0 <= comment_idx < len(lines):
        if lines[comment_idx].strip().lower() == "# diskmanager":
            del lines[comment_idx]
            # Y si justo antes hay una línea en blanco, también la limpiamos.
            blank_idx = comment_idx - 1
            if 0 <= blank_idx < len(lines) and not lines[blank_idx].strip():
                del lines[blank_idx]


def _apply_samba_reload() -> None:
    # En algunas distros `reload` no aplica cambios o el unit name es distinto.
    # Intentamos reload y si falla, hacemos fallback a restart.
    if not shutil.which("systemctl"):
        return

    units = ["smbd", "smb", "nmbd"]

    reloaded_any = False
    for u in units:
        cp = _run(["systemctl", "reload", u], timeout_s=15)
        if cp.returncode == 0:
            reloaded_any = True

    if reloaded_any:
        return

    # Fallback: restart (más agresivo, pero más fiable para aplicar smb.conf)
    for u in units:
        _run(["systemctl", "restart", u], timeout_s=25)


def _norm_path(p: str) -> str:
    p = (p or "").strip()
    if p.endswith("/") and len(p) > 1:
        p = p.rstrip("/")
    return p


def _samba_enabled_share_for_path(path: str) -> bool:
    target = _norm_path(path)
    if not target:
        return False
    for s in samba_shares():
        if not s.get("enabled"):
            continue
        if _norm_path(s.get("path") or "") == target:
            return True
    return False


def _samba_share_exists_for_path(path: str) -> bool:
    target = _norm_path(path)
    if not target:
        return False
    for s in samba_shares():
        if _norm_path(s.get("path") or "") == target:
            return True
    return False


def _write_samba_conf_lines(lines: list[str]) -> tuple[bool, str, str]:
    """Escribe smb.conf de forma segura. Returns (ok, backup_path, error_details)."""

    smb_conf = "/etc/samba/smb.conf"
    if not os.path.exists(smb_conf):
        return (False, "", "No existe /etc/samba/smb.conf")

    backup = f"/etc/samba/smb.conf.bak.diskmanager.{int(time.time())}"
    tmp_path = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            delete=False,
            dir=os.path.dirname(smb_conf),
            prefix=".smb.conf.diskmanager.",
        ) as tf:
            tf.writelines(lines)
            tmp_path = tf.name

        if shutil.which("testparm"):
            cp = _run(["testparm", "-s", tmp_path], timeout_s=25)
            if cp.returncode != 0:
                err = _truncate((cp.stderr or cp.stdout or ""))
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass
                return (False, "", f"Configuración Samba inválida (testparm):\n{err}")

        shutil.copy2(smb_conf, backup)
        try:
            st = os.stat(smb_conf)
            os.chmod(tmp_path, st.st_mode)
        except Exception:
            pass
        os.replace(tmp_path, smb_conf)
        tmp_path = ""
        return (True, backup, "")

    except Exception as e:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
        if os.path.exists(backup):
            try:
                shutil.copy2(backup, smb_conf)
            except Exception:
                pass
        return (False, "", _truncate(str(e)))


def _find_share_block_by_path(lines: list[str], target_path: str) -> tuple[int, int] | None:
    """Encuentra el bloque [share] cuyo 'path' coincide con `target_path`.

    Robustez:
    - No depende del espaciado ("path=/x" vs "path = /x")
    - Normaliza trailing slashes
    """

    tp = _norm_path(target_path)
    if not tp:
        return None

    start: int | None = None
    name: str | None = None

    def block_has_path(a: int, b: int) -> bool:
        for raw in lines[a:b]:
            s = raw.strip()
            if not s or s.startswith("#") or s.startswith(";"):
                continue
            if "=" not in s:
                continue
            k, v = [p.strip() for p in s.split("=", 1)]
            if k.lower() == "path":
                return _norm_path(v) == tp
        return False

    for i, raw in enumerate(lines):
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            if start is not None and name and name.lower() != "global":
                if block_has_path(start, i):
                    return (start, i)
            start = i
            name = line[1:-1].strip()

    if start is not None and name and name.lower() != "global":
        if block_has_path(start, len(lines)):
            return (start, len(lines))

    return None


def _set_share_available_by_path(target_path: str, enable: bool, *, restart: bool = True) -> tuple[bool, str]:
    """Activa/desactiva un share existente por su path usando 'available = yes/no'.

    Returns: (changed, details). If no share exists for the path, changed=False.
    """

    smb_conf = "/etc/samba/smb.conf"
    if not os.path.exists(smb_conf):
        return (False, "Samba no instalado (no smb.conf).")

    try:
        with open(smb_conf, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except Exception as e:
        return (False, f"No se pudo leer smb.conf: {e}")

    block = _find_share_block_by_path(lines, target_path)
    if not block:
        return (False, "No hay share existente para ese path.")

    a, b = block
    desired = "yes" if enable else "no"
    changed = False

    # Buscar y actualizar (o insertar) la línea 'available'.
    for i in range(a + 1, b):
        raw = lines[i]
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith(";"):
            continue
        if "=" not in stripped:
            continue
        k, v = [p.strip() for p in stripped.split("=", 1)]
        if k.lower() == "available":
            if v.lower() == desired:
                return (False, f"Share ya estaba available={desired}.")
            indent = re.match(r"^\s*", raw).group(0)
            lines[i] = f"{indent}available = {desired}\n"
            changed = True
            break

    if not changed:
        # Insertar justo después del header del share.
        lines.insert(a + 1, f"   available = {desired}\n")
        changed = True

    ok, backup, err = _write_samba_conf_lines(lines)
    if not ok:
        return (False, f"No se aplicaron cambios en Samba. {err}")

    if restart:
        _apply_samba_restart()
    return (True, f"Share actualizado (available={desired}). Backup: {backup}")


def _remove_share_block_by_path(target_path: str) -> tuple[bool, str]:
    """Elimina el bloque del share que apunta a `path = <target_path>`.

    Returns: (changed, details). If no share exists for the path, changed=False.
    """

    smb_conf = "/etc/samba/smb.conf"
    if not os.path.exists(smb_conf):
        return (False, "Samba no instalado (no smb.conf).")

    try:
        with open(smb_conf, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except Exception as e:
        return (False, f"No se pudo leer smb.conf: {e}")

    block = _find_share_block_by_path(lines, target_path)
    if not block:
        return (False, "No había share para ese path.")

    a, b = block
    del lines[a:b]

    ok, backup, err = _write_samba_conf_lines(lines)
    if not ok:
        return (False, f"No se aplicaron cambios en Samba. {err}")

    _apply_samba_restart()
    return (True, f"Share eliminado. Backup: {backup}")


def _apply_samba_restart() -> None:
    # Reinicio best-effort: intentamos varios unit names.
    if not shutil.which("systemctl"):
        return
    for u in ["smbd", "smb", "nmbd"]:
        _run(["systemctl", "restart", u], timeout_s=25)


def _format_commands(fstype: str, dev_path: str, label: str | None) -> list[str] | None:
    fs = (fstype or "").strip().lower()
    safe_label = (label or "").strip()
    if safe_label:
        safe_label = re.sub(r"[^A-Za-z0-9._-]", "_", safe_label)[:32]

    if fs == "ext4":
        if not shutil.which("mkfs.ext4"):
            return None
        args = ["mkfs.ext4", "-F"]
        if safe_label:
            args += ["-L", safe_label]
        return args + [dev_path]

    if fs == "xfs":
        if not shutil.which("mkfs.xfs"):
            return None
        args = ["mkfs.xfs", "-f"]
        if safe_label:
            args += ["-L", safe_label]
        return args + [dev_path]

    if fs in {"exfat", "exfatprogs"}:
        # exfatprogs suele exponer mkfs.exfat
        if not shutil.which("mkfs.exfat"):
            return None
        args = ["mkfs.exfat"]
        if safe_label:
            args += ["-n", safe_label]
        return args + [dev_path]

    if fs in {"vfat", "fat32"}:
        if not shutil.which("mkfs.vfat"):
            return None
        args = ["mkfs.vfat", "-F", "32"]
        if safe_label:
            args += ["-n", safe_label[:11]]
        return args + [dev_path]

    if fs == "ntfs":
        # mkfs.ntfs suele venir con ntfs-3g.
        if not shutil.which("mkfs.ntfs"):
            return None
        # Usamos quick format (-Q) para que no tarde muchísimo en discos grandes.
        # (-F) fuerza incluso si ya hay firma previa.
        args = ["mkfs.ntfs", "-Q", "-F"]
        if safe_label:
            args += ["-L", safe_label]
        return args + [dev_path]

    return None


def _wipe_and_single_partition(disk_name: str, *, msftdata: bool = False) -> tuple[bool, str, str]:
    """Borra tabla de particiones del disco y crea una única partición.

    Returns: (ok, new_partition_path, details)
    """

    disk_name = (disk_name or "").strip()
    if not disk_name or not DEVICE_ID_RE.match(disk_name):
        return (False, "", "Disco inválido.")

    disk_path = f"/dev/{disk_name}"
    try:
        st = os.stat(disk_path)
        if not stat.S_ISBLK(st.st_mode):
            return (False, "", f"{disk_path} no es un dispositivo de bloque.")
    except Exception as e:
        return (False, "", f"No se pudo validar {disk_path}: {e}")

    # Herramientas: preferimos sgdisk (zap-all) y parted (mklabel/mkpart).
    has_parted = bool(shutil.which("parted"))
    has_sgdisk = bool(shutil.which("sgdisk"))
    has_sfdisk = bool(shutil.which("sfdisk"))
    if not has_parted and not has_sfdisk:
        return (
            False,
            "",
            "Falta herramienta para particionar. Instala 'parted' (recomendado) o usa 'sfdisk'.",
        )

    steps: list[str] = []

    # Intentamos desmontar cualquier cosa colgada del disco (best-effort, debería estar desmontado ya).
    _run(["umount", "-A", disk_path], timeout_s=10)

    # Limpiar firmas/metadata.
    if shutil.which("wipefs"):
        cp = _run(["wipefs", "-a", disk_path], timeout_s=60)
        if cp.returncode == 0:
            steps.append("wipefs: OK")
        else:
            steps.append("wipefs: fallo (continuando)")

    if has_sgdisk:
        cp = _run(["sgdisk", "--zap-all", disk_path], timeout_s=120)
        if cp.returncode == 0:
            steps.append("sgdisk --zap-all: OK")
        else:
            steps.append("sgdisk --zap-all: fallo (continuando)")

    # Crear tabla GPT + partición única (1MiB..100%).
    if has_parted:
        cp1 = _run(["parted", "-s", disk_path, "mklabel", "gpt"], timeout_s=60)
        cp2 = _run(["parted", "-s", "-a", "optimal", disk_path, "mkpart", "primary", "1MiB", "100%"], timeout_s=90)
        if cp1.returncode != 0 or cp2.returncode != 0:
            err = _truncate(((cp1.stderr or cp1.stdout or "") + "\n" + (cp2.stderr or cp2.stdout or "")).strip())
            return (False, "", f"Error particionando con parted:\n{err}")
        steps.append("parted: GPT + 1 partición: OK")

        # Para discos que quieres usar en Windows, marcar como "Microsoft basic data".
        # Si se queda como tipo Linux, Windows a veces no asigna letra.
        if msftdata:
            cp3 = _run(["parted", "-s", disk_path, "set", "1", "msftdata", "on"], timeout_s=30)
            if cp3.returncode == 0:
                steps.append("parted: msftdata=on: OK")
            else:
                steps.append("parted: msftdata=on: fallo (continuando)")
    else:
        # Fallback sfdisk: GPT + una partición ocupando todo.
        script = "label: gpt\n,\n"
        cp = subprocess.run(
            ["sfdisk", disk_path],
            input=script,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=90,
            check=False,
        )
        if cp.returncode != 0:
            err = _truncate((cp.stderr or cp.stdout or "").strip())
            return (False, "", f"Error particionando con sfdisk:\n{err}")
        steps.append("sfdisk: GPT + 1 partición: OK")

        if msftdata and has_sgdisk:
            # gdisk typecode 0700 = Microsoft basic data
            cp3 = _run(["sgdisk", "-t", "1:0700", disk_path], timeout_s=30)
            if cp3.returncode == 0:
                steps.append("sgdisk: type 1=0700 (msftdata): OK")
            else:
                steps.append("sgdisk: type 1=0700: fallo (continuando)")

    # Pedir al kernel que relea la tabla.
    if shutil.which("partprobe"):
        _run(["partprobe", disk_path], timeout_s=30)
    if shutil.which("udevadm"):
        _run(["udevadm", "settle"], timeout_s=30)

    # Encontrar la nueva partición: la única child con pkname=disk_name.
    parts = lsblk_partitions()
    candidates = [p for p in parts if (p.get("type") == "part") and ((p.get("pkname") or "").strip() == disk_name)]
    if not candidates:
        # Reintento corto: a veces udev tarda.
        if shutil.which("udevadm"):
            _run(["udevadm", "settle"], timeout_s=30)
        parts = lsblk_partitions()
        candidates = [p for p in parts if (p.get("type") == "part") and ((p.get("pkname") or "").strip() == disk_name)]

    if not candidates:
        return (False, "", f"No se encontró la partición nueva en {disk_path}.")

    # Elegir la partición más grande (por seguridad si el tool creó algo extra).
    def size_key(p: dict[str, Any]) -> int:
        # lsblk size viene tipo "489G"; reusamos el parser existente.
        return _parse_size_to_bytes(p.get("size") or "0")

    candidates.sort(key=size_key, reverse=True)
    new_part = candidates[0]
    new_path = (new_part.get("path") or "").strip() or f"/dev/{new_part.get('name') or ''}"
    if not new_path.startswith("/dev/"):
        return (False, "", "No se pudo determinar el path de la nueva partición.")

    return (True, new_path, "\n".join(steps))


def _available_format_options() -> list[dict[str, str]]:
    # Lista de formatos soportados + disponible si existe mkfs correspondiente.
    opts: list[dict[str, str]] = []

    def add(fstype: str, label: str, tool: str) -> None:
        if shutil.which(tool):
            opts.append({"fstype": fstype, "label": label})

    add("ext4", "ext4 (Linux)", "mkfs.ext4")
    add("xfs", "xfs (Linux)", "mkfs.xfs")
    add("exfat", "exFAT (Windows/macOS/Linux)", "mkfs.exfat")
    add("vfat", "FAT32 (vfat)", "mkfs.vfat")
    add("ntfs", "NTFS (Windows)", "mkfs.ntfs")

    return opts


def _fstab_fields_for_fstype(fstype: str) -> tuple[str, str, str, str]:
    """Devuelve (fstype, options, dump, passno) para /etc/fstab."""
    fs = (fstype or "").strip().lower()

    if fs in {"ntfs", "ntfs3"}:
        uid, gid = _default_uid_gid()
        options = f"defaults,nofail,uid={uid},gid={gid}"
        # Elegir el mejor fstype disponible en este host.
        kfs = _kernel_filesystems()
        if "ntfs3" in kfs:
            return ("ntfs3", options, "0", "0")
        if shutil.which("mount.ntfs-3g"):
            return ("ntfs-3g", options, "0", "0")
        return ("ntfs", options, "0", "0")

    return (fstype or "auto", "defaults,nofail", "0", "2")


@dataclass(frozen=True)
class FstabEntry:
    spec: str
    mountpoint: str
    fstype: str
    options: str
    dump: str
    passno: str
    raw: str

    @property
    def uuid(self) -> str | None:
        if self.spec.startswith("UUID="):
            return self.spec.split("=", 1)[1]
        return None


def read_fstab_text() -> str:
    try:
        with open("/etc/fstab", "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except FileNotFoundError:
        return "# /etc/fstab no encontrado\n"
    except PermissionError:
        return "# Sin permisos para leer /etc/fstab\n"


def parse_fstab() -> list[FstabEntry]:
    entries: list[FstabEntry] = []
    try:
        with open("/etc/fstab", "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                raw = line.rstrip("\n")
                stripped = raw.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                # /etc/fstab es whitespace-separated. Ignoramos columnas extra.
                parts = stripped.split()
                if len(parts) < 6:
                    continue
                spec, mountpoint, fstype, options, dump, passno = parts[:6]
                entries.append(
                    FstabEntry(
                        spec=spec,
                        mountpoint=mountpoint,
                        fstype=fstype,
                        options=options,
                        dump=dump,
                        passno=passno,
                        raw=raw,
                    )
                )
    except FileNotFoundError:
        return []
    except PermissionError:
        # Aun sin permisos para escribir, queremos que la UI pueda arrancar.
        return []
    return entries


def fstab_rows(entries: list[FstabEntry]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for e in entries:
        status = "OK"
        if e.uuid:
            # Si existe el symlink, el disco está presente.
            if not os.path.exists(f"/dev/disk/by-uuid/{e.uuid}"):
                status = "FALTA_DISCO"
        elif e.spec.startswith("/dev/"):
            if not os.path.exists(e.spec):
                status = "FALTA_DISCO"
        rows.append(
            {
                "spec": e.spec,
                "mountpoint": e.mountpoint,
                "fstype": e.fstype,
                "options": e.options,
                "dump": e.dump,
                "passno": e.passno,
                "status": status,
            }
        )
    return rows


def lsblk_partitions() -> list[dict[str, Any]]:
    # Usamos JSON para no depender de parsing frágil.
    cp = _run(
        [
            "lsblk",
            "-J",
            "-o",
            "NAME,PATH,PKNAME,SIZE,FSTYPE,LABEL,UUID,MOUNTPOINT,TYPE,RM,HOTPLUG,TRAN",
        ],
        timeout_s=10,
    )
    if cp.returncode != 0:
        return []
    try:
        payload = json.loads(cp.stdout)
    except json.JSONDecodeError:
        return []

    parts: list[dict[str, Any]] = []

    def walk(node: dict[str, Any]) -> None:
        t = node.get("type")
        if t in {"disk", "part", "crypt", "lvm"}:
            parts.append(node)
        for ch in node.get("children") or []:
            walk(ch)

    for dev in payload.get("blockdevices") or []:
        walk(dev)

    return parts


def samba_shares() -> list[dict[str, Any]]:
    smb_conf = "/etc/samba/smb.conf"
    if not os.path.exists(smb_conf):
        return []
    try:
        with open(smb_conf, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except PermissionError:
        return []

    shares: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith(";") or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            name = line[1:-1].strip()
            # Ignorar sección global y recursos de sistema (impresoras, IPC, etc.)
            if name.lower() in {"global", "printers", "print$", "ipc$"}:
                current = None
                continue
            current = {"name": name, "path": "", "public": False, "read_only": False, "enabled": True}
            shares.append(current)
            continue
        if current is None:
            continue
        if "=" in line:
            k, v = [p.strip() for p in line.split("=", 1)]
            k_low = k.lower()
            v_low = v.lower()
            if k_low == "path":
                current["path"] = v
            elif k_low in {"guest ok", "public"}:
                current["public"] = v_low in {"yes", "true", "1"}
            elif k_low in {"read only", "writable"}:
                if k_low == "read only":
                    current["read_only"] = v_low in {"yes", "true", "1"}
                else:
                    current["read_only"] = not (v_low in {"yes", "true", "1"})
            elif k_low == "available":
                # Si está deshabilitado, mantenemos el bloque pero no se sirve.
                current["enabled"] = v_low in {"yes", "true", "1"}
    return shares


def _human_size(size_bytes: int) -> str:
    if size_bytes == 0:
        return "0B"
    size_name = ("B", "KB", "MB", "GB", "TB", "PB")
    i = int(math.floor(math.log(size_bytes, 1024)))
    p = math.pow(1024, i)
    s = round(size_bytes / p, 2)
    return f"{s} {size_name[i]}"


def _get_usage(path: str) -> dict[str, Any] | None:
    try:
        total, used, free = shutil.disk_usage(path)
        percent = (used / total) * 100 if total > 0 else 0
        return {
            "total_bytes": total,
            "used_bytes": used,
            "free_bytes": free,
            "total": _human_size(total),
            "used": _human_size(used),
            "free": _human_size(free),
            "percent": round(percent, 1),
        }
    except Exception:
        return None


def disks_view() -> list[dict[str, Any]]:
    entries = parse_fstab()
    fstab_by_uuid: dict[str, FstabEntry] = {e.uuid: e for e in entries if e.uuid}
    fstab_by_dev: dict[str, FstabEntry] = {e.spec: e for e in entries if e.spec.startswith("/dev/")}
    shares = samba_shares()
    share_paths = {_norm_path(s.get("path") or "") for s in shares if s.get("path") and s.get("enabled")}

    disks: list[dict[str, Any]] = []
    seen_uuids: set[str] = set()

    parts = lsblk_partitions()
    root_disk = _root_physical_disk(parts)

    def is_external_disk(node: dict[str, Any]) -> bool:
        # Solo mostramos discos "en crudo" si parecen externos/hotplug.
        # (evita exponer discos internos sin particiones por accidente).
        try:
            if int(node.get("rm") or 0) == 1:
                return True
            if int(node.get("hotplug") or 0) == 1:
                return True
        except Exception:
            pass
        tran = (node.get("tran") or "").strip().lower()
        return tran == "usb"

    def is_manageable_partition(p: dict[str, Any]) -> bool:
        t = (p.get("type") or "").strip().lower()

        # Permitir discos sin particiones (solo externos) para poder formatearlos.
        if t == "disk":
            # Nunca mostrar el disco del sistema.
            if root_disk and (p.get("name") == root_disk):
                return False
            # Si tiene hijos/particiones, lo gestionamos a nivel de particiones, no a nivel de disco.
            if p.get("children"):
                return False
            return is_external_disk(p)

        if _is_noise_partition(p):
            return False

        # Nunca mostrar ni gestionar particiones del disco del sistema (donde está /).
        if root_disk and (p.get("pkname") == root_disk):
            return False

        mountpoint = p.get("mountpoint") or ""
        mounted = bool(mountpoint)
        fstype = (p.get("fstype") or "").lower()
        if fstype == "swap":
            return False
        # No tocar montajes del sistema.
        if mounted and _is_system_mountpoint(mountpoint):
            return False
        # Si está montado, solo consideramos “de usuario” lo montado bajo /mnt o /media.
        if mounted and not _is_user_mountpoint(mountpoint):
            return False
        return True

    for p in parts:
        name = p.get("name") or ""
        uuid = p.get("uuid") or ""
        fstype = p.get("fstype") or ""
        label = p.get("label") or ""
        size = p.get("size") or ""
        mountpoint = p.get("mountpoint") or ""
        mounted = bool(mountpoint)
        dev_path = p.get("path") or f"/dev/{name}"
        kind = (p.get("type") or "").strip().lower() or "part"

        if not is_manageable_partition(p):
            continue

        persistent = False
        if uuid and uuid in fstab_by_uuid:
            persistent = True
        elif dev_path in fstab_by_dev:
            persistent = True

        samba_enabled = bool(mountpoint and _norm_path(mountpoint) in share_paths)

        usage = None
        if mounted:
            usage = _get_usage(mountpoint)

        disks.append(
            {
                "id": name,
                "label": label or f"Disco {name}",
                "size": size,
                "fstype": fstype or "-",
                "uuid": uuid or "-",
                "mounted": mounted,
                "mountpoint": mountpoint or "",
                "persistent": persistent,
                "samba": samba_enabled,
                "kind": kind,
                "usage": usage,
            }
        )
        if uuid:
            seen_uuids.add(uuid)

    # Agregamos “discos faltantes” que están en fstab por UUID pero no existen ahora.
    for e in entries:
        if not e.uuid:
            continue
        if e.uuid in seen_uuids:
            continue
        if os.path.exists(f"/dev/disk/by-uuid/{e.uuid}"):
            continue
        # Solo mostrar como gestionable si su mountpoint es típico de usuario.
        if not _is_user_mountpoint(e.mountpoint):
            continue
        disks.append(
            {
                "id": "-",
                "label": "Disco no disponible",
                "size": "-",
                "fstype": e.fstype,
                "uuid": e.uuid,
                "mounted": False,
                "mountpoint": e.mountpoint,
                "persistent": True,
                "samba": False,
                "missing": True,
            }
        )

    return disks


def manageable_partition_by_name(dev_id: str) -> dict[str, Any] | None:
    dev_id = (dev_id or "").strip()
    if not DEVICE_ID_RE.match(dev_id):
        return None
    parts = lsblk_partitions()
    root_disk = _root_physical_disk(parts)

    for p in parts:
        if (p.get("name") or "") != dev_id:
            continue

        # Si es el disco del sistema, nunca permitir.
        if root_disk and dev_id == root_disk:
            return None

        t = (p.get("type") or "").strip().lower()

        # Permitir "disk" sin particiones para formateo.
        if t == "disk":
            if p.get("children"):
                return None
            # Solo permitir discos externos/hotplug.
            try:
                if int(p.get("rm") or 0) == 0 and int(p.get("hotplug") or 0) == 0:
                    tran = (p.get("tran") or "").strip().lower()
                    if tran != "usb":
                        return None
            except Exception:
                tran = (p.get("tran") or "").strip().lower()
                if tran != "usb":
                    return None
            return p

        if _is_noise_partition(p):
            return None

        # Nunca permitir operaciones sobre particiones del disco del sistema.
        if root_disk and (p.get("pkname") == root_disk):
            return None

        mountpoint = p.get("mountpoint") or ""
        mounted = bool(mountpoint)
        fstype = (p.get("fstype") or "").lower()
        if fstype == "swap":
            return None
        if mounted and _is_system_mountpoint(mountpoint):
            return None
        if mounted and not _is_user_mountpoint(mountpoint):
            return None
        return p
    return None


def _is_mountpoint_mounted(mountpoint: str) -> bool:
    mp = (mountpoint or "").strip()
    if not mp:
        return False
    try:
        with open("/proc/mounts", "r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                parts = raw.split()
                if len(parts) >= 2 and parts[1] == mp:
                    return True
    except Exception:
        return False
    return False


def _device_present_for_fstab_entry(e: "FstabEntry") -> bool:
    try:
        if e.uuid:
            return os.path.exists(f"/dev/disk/by-uuid/{e.uuid}")
        if e.spec.startswith("/dev/"):
            return os.path.exists(e.spec)
    except Exception:
        return False
    return False


def _automount_persistent_user_mounts() -> tuple[list[str], list[dict[str, str]]]:
    """Best-effort: monta entradas persistentes (fstab) bajo /mnt o /media si el disco está presente.

    Esto resuelve el caso de hot-unplug/hot-plug: al reconectar, quedan como 'permanente'
    pero no montadas hasta que alguien ejecute `mount <mountpoint>`.
    """

    if os.geteuid() != 0:
        return ([], [])

    # Evitar carreras al reconectar: esperar a udev.
    if shutil.which("udevadm"):
        _run(["udevadm", "settle"], timeout_s=20)

    entries = [e for e in parse_fstab() if _is_user_mountpoint(e.mountpoint)]

    mounted: list[str] = []
    failed: list[dict[str, str]] = []

    # Para reiniciar Samba una sola vez si hace falta.
    shares = samba_shares()
    share_paths = {s.get("path") for s in shares if s.get("path") and s.get("enabled")}
    mounted_share_paths = False

    for e in entries:
        mp = (e.mountpoint or "").strip()
        if not mp:
            continue
        if not _looks_safe_mountpoint(mp):
            continue
        if _is_mountpoint_mounted(mp):
            continue
        if not _device_present_for_fstab_entry(e):
            continue

        # Si el fstype es ntfs3, a veces conviene cargar el módulo antes.
        if (e.fstype or "").strip().lower() == "ntfs3":
            _try_modprobe("ntfs3")

        os.makedirs(mp, exist_ok=True)

        # Reintentos cortos por si el dispositivo aún se está enumerando.
        last_err = ""
        for _ in range(3):
            cp = _run(["mount", mp], timeout_s=30)
            if cp.returncode == 0:
                mounted.append(mp)
                if mp in share_paths:
                    mounted_share_paths = True
                last_err = ""
                break
            last_err = _truncate((cp.stderr or cp.stdout or "").strip())
            if shutil.which("udevadm"):
                _run(["udevadm", "settle"], timeout_s=10)
            time.sleep(1)

        if last_err:
            failed.append({"mountpoint": mp, "error": last_err})

    if mounted_share_paths:
        _apply_samba_restart()

    return (mounted, failed)


def stats(disks: list[dict[str, Any]]) -> dict[str, int]:
    detected = len(disks)
    mounted = sum(1 for d in disks if d.get("mounted"))
    missing = sum(1 for d in disks if d.get("missing"))
    persistent = sum(1 for d in disks if d.get("persistent"))
    samba = sum(1 for d in disks if d.get("samba"))
    return dict(detected=detected, mounted=mounted, missing=missing, persistent=persistent, samba=samba)


# ---- Rutas UI ----
@app.get("/")
def home():
    return redirect(url_for("dashboard"))

@app.get("/dashboard")
def dashboard():
    disks = disks_view()
    return render_template("dashboard.html", stats=stats(disks), disks=disks)

@app.get("/disks")
def disks():
    return render_template("disks.html", disks=disks_view())

@app.get("/fstab")
def fstab():
    # Vista amigable: solo mostramos entradas "de usuario" (p.ej. /mnt o /media).
    entries = [e for e in parse_fstab() if _is_user_mountpoint(e.mountpoint)]
    return render_template("fstab.html", rows=fstab_rows(entries))

@app.get("/samba")
def samba():
    return render_template("samba.html", shares=samba_shares(), disks=disks_view())


@app.post("/api/mount")
def api_mount():
    ok, msg = _require_root()
    if not ok:
        return jsonify({"ok": False, "message": msg}), 403

    data = request.json or {}
    dev_id = (data.get("id") or "").strip()
    if not DEVICE_ID_RE.match(dev_id):
        return jsonify({"ok": False, "message": "ID de dispositivo inválido."}), 400

    dev_path = f"/dev/{dev_id}"
    if not os.path.exists(dev_path):
        return jsonify({"ok": False, "message": f"No existe {dev_path}."}), 404

    part = manageable_partition_by_name(dev_id)
    if not part:
        return (
            jsonify(
                {
                    "ok": False,
                    "message": "Este dispositivo parece ser del sistema o no es gestionable desde esta UI.",
                }
            ),
            400,
        )

    disks = disks_view()
    d = next((x for x in disks if x.get("id") == dev_id), None)
    if d and d.get("mounted"):
        return jsonify({"ok": True, "message": "Ya estaba montado."})

    # Si está en fstab, montamos por mountpoint (mount <dir>) para usar opciones.
    entries = parse_fstab()
    target_mountpoint: str | None = None
    disk_uuid = d.get("uuid") if d else None
    if disk_uuid and disk_uuid != "-":
        for e in entries:
            if e.spec == f"UUID={disk_uuid}":
                target_mountpoint = e.mountpoint
                break

    mounted_mp = ""
    if target_mountpoint:
        if not _looks_safe_mountpoint(target_mountpoint):
            return jsonify({"ok": False, "message": "Punto de montaje inseguro; solo /mnt o /media."}), 400
        os.makedirs(target_mountpoint, exist_ok=True)
        if shutil.which("udevadm"):
            _run(["udevadm", "settle"], timeout_s=15)
        cp = _run(["mount", target_mountpoint], timeout_s=45)
        mounted_mp = target_mountpoint
    else:
        # Montaje directo a /mnt/<label|id>
        requested = (data.get("mount_dir") or "").strip()
        if requested:
            mp = f"/mnt/{_safe_mount_dir(requested)}"
        else:
            label = (d.get("label") if d else "") or (part.get("label") or dev_id)
            mp = f"/mnt/{_safe_mount_dir(label)}"
        if not _looks_safe_mountpoint(mp):
            return jsonify({"ok": False, "message": "Punto de montaje inseguro."}), 400
        os.makedirs(mp, exist_ok=True)

        fs = (part.get("fstype") or "").strip().lower()
        if fs in {"ntfs", "ntfs3"}:
            cp = _mount_ntfs(dev_path, mp)
        else:
            if shutil.which("udevadm"):
                _run(["udevadm", "settle"], timeout_s=10)
            cp = _run(["mount", dev_path, mp], timeout_s=30)
        mounted_mp = mp

    if cp.returncode != 0:
        err = (cp.stderr or cp.stdout or "").strip()
        if "unknown filesystem type 'ntfs-3g'" in err.lower() or "unknown filesystem type \"ntfs-3g\"" in err.lower():
            err += "\nSugerencia: instala el paquete 'ntfs-3g' o habilita el driver kernel 'ntfs3' (modprobe ntfs3)."
        return jsonify({"ok": False, "message": f"Error montando: {err}"}), 500

    # Si ya existe un share habilitado apuntando a este mountpoint, refrescamos Samba
    # para evitar que el usuario tenga que reiniciar smbd manualmente.
    try:
        if mounted_mp and os.path.exists("/etc/samba/smb.conf"):
            # Si el share existía y estaba deshabilitado (available=no), lo re-habilitamos.
            _set_share_available_by_path(mounted_mp, True, restart=False)
            # Y en cualquier caso, si hay share habilitado para ese path, reiniciar hace que quede accesible.
            if _samba_enabled_share_for_path(mounted_mp):
                _apply_samba_restart()
    except Exception:
        pass
    return jsonify({"ok": True, "message": "Montado correctamente."})


@app.get("/api/disks")
def api_disks_view():
    return jsonify({"ok": True, "disks": disks_view()})


@app.post("/api/reconnect")
def api_reconnect():
    """Monta automáticamente discos persistentes que fueron desconectados y han vuelto.

    - Solo considera entradas de fstab bajo /mnt o /media.
    - No crea/modifica fstab.
    - Si ya existe un share Samba para ese mountpoint (aunque estuviera 'available = no'), lo re-habilita.
    """

    ok, msg = _require_root()
    if not ok:
        return jsonify({"ok": False, "message": msg}), 403

    # Esperar a udev por si se acaba de reconectar en caliente.
    if shutil.which("udevadm"):
        _run(["udevadm", "settle"], timeout_s=25)

    entries = [e for e in parse_fstab() if _is_user_mountpoint(e.mountpoint)]
    needs_samba_restart = False

    mounted: list[str] = []
    skipped: list[str] = []
    failed: list[dict[str, str]] = []

    for e in entries:
        mp = e.mountpoint
        if not mp:
            continue
        if not _looks_safe_mountpoint(mp):
            skipped.append(f"{mp} (inseguro)")
            continue

        # Solo intentamos si el disco/uuid está presente.
        if not _device_present_for_fstab_entry(e):
            continue

        if _is_mountpoint_mounted(mp):
            skipped.append(f"{mp} (ya montado)")
            continue

        # Si el fstype es ntfs3, intentar cargar módulo (best-effort).
        if (e.fstype or "").strip().lower() == "ntfs3":
            _try_modprobe("ntfs3")

        os.makedirs(mp, exist_ok=True)
        last_err = ""
        for _ in range(3):
            cp = _run(["mount", mp], timeout_s=30)
            if cp.returncode == 0:
                last_err = ""
                break
            last_err = _truncate((cp.stderr or cp.stdout or "").strip())
            if shutil.which("udevadm"):
                _run(["udevadm", "settle"], timeout_s=10)
            time.sleep(1)
        if last_err:
            failed.append({"mountpoint": mp, "error": last_err})
            continue

        mounted.append(mp)

        # Si existe un share para este path y estaba deshabilitado, lo re-habilitamos.
        if _samba_share_exists_for_path(mp) and not _samba_enabled_share_for_path(mp):
            _set_share_available_by_path(mp, True, restart=False)
            needs_samba_restart = True

    if needs_samba_restart:
        _apply_samba_restart()

    details_lines: list[str] = []
    if mounted:
        details_lines.append("Montados: " + ", ".join(mounted))
    if skipped:
        details_lines.append("Ignorados: " + ", ".join(skipped))
    if failed:
        details_lines.append("Fallos: " + ", ".join(f"{x['mountpoint']}: {x['error']}" for x in failed))

    return jsonify(
        {
            "ok": True,
            "message": f"Reconectar: {len(mounted)} montado(s), {len(failed)} fallo(s).",
            "details": "\n".join(details_lines),
        }
    )

@app.post("/api/unmount")
def api_unmount():
    ok, msg = _require_root()
    if not ok:
        return jsonify({"ok": False, "message": msg}), 403

    data = request.json or {}
    dev_id = (data.get("id") or "").strip()
    if not DEVICE_ID_RE.match(dev_id):
        return jsonify({"ok": False, "message": "ID de dispositivo inválido."}), 400

    disks = disks_view()
    d = next((x for x in disks if x.get("id") == dev_id), None)
    mountpoint = (d or {}).get("mountpoint") or ""
    if not mountpoint:
        return jsonify({"ok": True, "message": "Ya estaba desmontado."})
    if not _looks_safe_mountpoint(mountpoint):
        return jsonify({"ok": False, "message": "Punto de montaje inseguro; solo /mnt o /media."}), 400

    cp = _run(["umount", mountpoint], timeout_s=20)
    if cp.returncode != 0:
        err = (cp.stderr or cp.stdout or "").strip()
        return jsonify({"ok": False, "message": f"Error desmontando: {err}"}), 500

    # Limpieza: si el directorio quedó vacío, lo borramos para no dejar huérfanos.
    _cleanup_mount_dir(mountpoint)
    return jsonify({"ok": True, "message": "Desmontado correctamente."})


@app.post("/api/format")
def api_format():
    ok, msg = _require_root()
    if not ok:
        return jsonify({"ok": False, "message": msg}), 403

    data = request.json or {}
    dev_id = (data.get("id") or "").strip()
    if not DEVICE_ID_RE.match(dev_id):
        return jsonify({"ok": False, "message": "ID de dispositivo inválido."}), 400

    fstype = (data.get("fstype") or "").strip().lower()
    label = (data.get("label") or "").strip()

    # Confirmación fuerte (servidor): requiere escribir exactamente FORMATEAR.
    expected = "FORMATEAR"
    confirm_text = (data.get("confirm_text") or "").strip()
    if confirm_text != expected:
        return (
            jsonify(
                {
                    "ok": False,
                    "message": "Confirmación inválida.",
                    "details": f"Escribe exactamente: {expected}",
                }
            ),
            400,
        )

    # Reutilizamos las protecciones: no permitir sistema/noise/etc.
    part = manageable_partition_by_name(dev_id)
    if not part:
        return (
            jsonify(
                {
                    "ok": False,
                    "message": "Este dispositivo no es gestionable desde esta UI.",
                }
            ),
            400,
        )

    # Seguridad extra: solo permitir formatear si NO está montado, no persistente, no compartido.
    disks = disks_view()
    d = next((x for x in disks if x.get("id") == dev_id), None)
    if d and d.get("mounted"):
        return jsonify({"ok": False, "message": "Desmonta el disco antes de formatear."}), 400
    if d and d.get("persistent"):
        return jsonify({"ok": False, "message": "Quita la persistencia (fstab) antes de formatear."}), 400
    if d and d.get("samba"):
        return jsonify({"ok": False, "message": "Quita la compartición Samba antes de formatear."}), 400

    # Importante: al formatear queremos limpiar el DISCO ENTERO, no solo la partición.
    # - Si el usuario seleccionó una partición (sde1), usamos su pkname (sde)
    # - Si seleccionó el disco en crudo (sde), usamos el propio nombre.
    part_type = (part.get("type") or "").strip().lower()
    disk_name = (part.get("name") or "").strip() if part_type == "disk" else (part.get("pkname") or "").strip()
    if not disk_name:
        return (
            jsonify(
                {
                    "ok": False,
                    "message": "No se pudo determinar el disco físico para este dispositivo.",
                }
            ),
            500,
        )

    msftdata = fstype in {"ntfs", "exfat", "vfat", "fat32"}
    ok2, new_part_path, details = _wipe_and_single_partition(disk_name, msftdata=msftdata)
    if not ok2:
        return (
            jsonify(
                {
                    "ok": False,
                    "message": "No se pudo preparar el disco (borrar/particionar).",
                    "details": _truncate(details),
                }
            ),
            500,
        )

    cmd = _format_commands(fstype, new_part_path, label)
    if not cmd:
        return (
            jsonify(
                {
                    "ok": False,
                    "message": "Formato no soportado o herramienta no instalada.",
                    "details": "Soportados: ext4, xfs, exfat, vfat(fat32), ntfs (requiere mkfs.*).",
                }
            ),
            400,
        )

    # El formateo puede tardar bastante (especialmente NTFS). Hacemos el timeout
    # configurable para evitar un 500 silencioso en el frontend.
    format_timeout_s = int(os.getenv("DISKMANAGER_FORMAT_TIMEOUT_S", "1800"))
    cp = _run(cmd, timeout_s=format_timeout_s)
    if cp.returncode != 0:
        err = _truncate((cp.stderr or cp.stdout or ""))
        if cp.returncode == 124:
            return (
                jsonify(
                    {
                        "ok": False,
                        "message": "El formateo tardó demasiado y se canceló.",
                        "details": f"Aumenta DISKMANAGER_FORMAT_TIMEOUT_S (actual: {format_timeout_s}s) o revisa el estado del disco.\n{err}",
                    }
                ),
                504,
            )
        return jsonify({"ok": False, "message": "Error formateando.", "details": err}), 500

    return jsonify(
        {
            "ok": True,
            "message": f"Disco formateado como {fstype}.",
            "details": _truncate(details),
        }
    )


@app.get("/api/format/options")
def api_format_options():
    return jsonify({"ok": True, "options": _available_format_options()})

@app.post("/api/persist")
def api_persist():
    ok, msg = _require_root()
    if not ok:
        return jsonify({"ok": False, "message": msg}), 403

    data = request.json or {}
    dev_id = (data.get("id") or "").strip()
    if not DEVICE_ID_RE.match(dev_id):
        return jsonify({"ok": False, "message": "ID de dispositivo inválido."}), 400

    disks = disks_view()
    d = next((x for x in disks if x.get("id") == dev_id), None)
    if not d:
        return jsonify({"ok": False, "message": "Disco no encontrado."}), 404
    uuid = d.get("uuid")
    if not uuid or uuid == "-":
        return jsonify({"ok": False, "message": "No se puede hacer persistente: UUID no disponible."}), 400

    fstab_path = "/etc/fstab"
    try:
        with open(fstab_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except Exception as e:
        return jsonify({"ok": False, "message": f"No se pudo leer /etc/fstab: {e}"}), 500

    uuid_spec = f"UUID={uuid}"
    existing_idx = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.split()[0] == uuid_spec:
            existing_idx = i
            break

    enable = data.get("enable", None)
    if enable is not None and not isinstance(enable, bool):
        return jsonify({"ok": False, "message": "Campo 'enable' inválido (usa true/false)."}), 400

    modified = False

    if enable is True:
        if existing_idx is not None:
            return jsonify({"ok": True, "message": "Entrada fstab ya existía."})
        # crear
        existing_idx = None

    if enable is False:
        if existing_idx is None:
            return jsonify({"ok": True, "message": "Entrada fstab ya estaba eliminada."})
        _remove_diskmanager_fstab_block(lines, existing_idx)
        action = "eliminada"
        modified = True

    elif existing_idx is not None:
        # Modo legacy: toggle -> quitar entrada
        _remove_diskmanager_fstab_block(lines, existing_idx)
        action = "eliminada"
        modified = True

    else:
        mountpoint = d.get("mountpoint") or ""
        if not mountpoint:
            requested = (data.get("mount_dir") or "").strip()
            if requested:
                mountpoint = f"/mnt/{_safe_mount_dir(requested)}"
            else:
                mountpoint = f"/mnt/{_safe_mount_dir(d.get('label') or dev_id)}"
        if not _looks_safe_mountpoint(mountpoint):
            return jsonify({"ok": False, "message": "Punto de montaje inseguro; solo /mnt o /media."}), 400
        os.makedirs(mountpoint, exist_ok=True)
        fstype = d.get("fstype") or "auto"
        fstype_out, options_out, dump_out, passno_out = _fstab_fields_for_fstype(fstype)
        # Marcar la entrada como "gestionada" para permitir reconexión segura vía mount -a -O.
        if options_out and "x-diskmanager" not in options_out.split(","):
            options_out = options_out + ",x-diskmanager"
        new_line = f"{uuid_spec}\t{mountpoint}\t{fstype_out}\t{options_out}\t{dump_out}\t{passno_out}\n"
        lines.append("\n# diskmanager\n" + new_line)
        action = "añadida"
        modified = True

    if not modified:
        return jsonify({"ok": True, "message": "Sin cambios en fstab."})

    # Backup antes de escribir
    backup = f"/etc/fstab.bak.diskmanager.{int(time.time())}"
    try:
        shutil.copy2(fstab_path, backup)
        with open(fstab_path, "w", encoding="utf-8") as f:
            f.writelines(lines)
    except Exception as e:
        return jsonify({"ok": False, "message": f"No se pudo escribir /etc/fstab: {e}"}), 500

    return jsonify({"ok": True, "message": f"Entrada fstab {action}. (Backup: {backup})"})

@app.post("/api/samba")
def api_samba_toggle():
    ok, msg = _require_root()
    if not ok:
        return jsonify({"ok": False, "message": msg}), 403

    data = request.json or {}
    dev_id = (data.get("id") or "").strip()
    if not DEVICE_ID_RE.match(dev_id):
        return jsonify({"ok": False, "message": "ID de dispositivo inválido."}), 400

    enable = data.get("enable", None)
    if enable is not None and not isinstance(enable, bool):
        return jsonify({"ok": False, "message": "Campo 'enable' inválido (usa true/false)."}), 400

    # Para borrar shares, permitimos indicar el path explícitamente aunque el disco no esté montado.
    mountpoint_override = (data.get("path") or "").strip()

    disks = disks_view()
    d = next((x for x in disks if x.get("id") == dev_id), None)

    mountpoint = mountpoint_override
    if not mountpoint:
        if not d or not d.get("mounted"):
            return jsonify({"ok": False, "message": "El disco debe estar montado para compartir por Samba."}), 400
        mountpoint = d.get("mountpoint") or ""
    if not _looks_safe_mountpoint(mountpoint):
        return jsonify({"ok": False, "message": "Punto de montaje inseguro; solo /mnt o /media."}), 400

    smb_conf = "/etc/samba/smb.conf"
    if not os.path.exists(smb_conf):
        return jsonify({"ok": False, "message": "No existe /etc/samba/smb.conf (¿Samba instalado?)."}), 404

    try:
        with open(smb_conf, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except Exception as e:
        return jsonify({"ok": False, "message": f"No se pudo leer smb.conf: {e}"}), 500

    # Detectar si ya hay un share con ese path; si existe, lo quitamos.
    def find_share_block_by_path(target_path: str) -> tuple[int, int] | None:
        start = None
        name = None
        for i, raw in enumerate(lines):
            line = raw.strip()
            if line.startswith("[") and line.endswith("]"):
                # cerrar anterior
                if start is not None and name and name.lower() != "global":
                    # buscar path dentro del bloque anterior
                    block = "".join(lines[start:i]).lower()
                    if f"path = {target_path}".lower() in block:
                        return (start, i)
                start = i
                name = line[1:-1].strip()
        # último bloque
        if start is not None and name and name.lower() != "global":
            block = "".join(lines[start:]).lower()
            if f"path = {target_path}".lower() in block:
                return (start, len(lines))
        return None

    block = find_share_block_by_path(mountpoint)
    action = ""

    def create_share_block() -> str:
        server_user = _default_server_user()
        share_name = _safe_share_name(os.path.basename(mountpoint) or ((d or {}).get("label") or dev_id))
        return (
            "\n"
            f"[{share_name}]\n"
            f"   path = {mountpoint}\n"
            "   browseable = yes\n"
            "   read only = no\n"
            "   guest ok = yes\n"
            "   public = yes\n"
            f"   force user = {server_user}\n"
            "   create mask = 0775\n"
            "   directory mask = 0775\n"
            f"   dfree command = /bin/df -P {mountpoint}\n"
        )

    if enable is True:
        if block:
            action = "ya existía"
        else:
            lines.append(create_share_block())
            action = "creado"
    elif enable is False:
        if block:
            a, b = block
            del lines[a:b]
            action = "eliminado"
        else:
            action = "ya estaba eliminado"
    else:
        # Modo legacy: toggle
        if block:
            a, b = block
            del lines[a:b]
            action = "eliminado"
        else:
            lines.append(create_share_block())
            action = "creado"

    # Escribimos a un temporal, validamos con testparm, y solo entonces reemplazamos smb.conf.
    backup = f"/etc/samba/smb.conf.bak.diskmanager.{int(time.time())}"
    tmp_path = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            delete=False,
            dir=os.path.dirname(smb_conf),
            prefix=".smb.conf.diskmanager.",
        ) as tf:
            tf.writelines(lines)
            tmp_path = tf.name

        # Validar antes de aplicar cambios.
        if shutil.which("testparm"):
            cp = _run(["testparm", "-s", tmp_path], timeout_s=25)
            if cp.returncode != 0:
                err = _truncate((cp.stderr or cp.stdout or ""))
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass
                return (
                    jsonify(
                        {
                            "ok": False,
                            "message": "Configuración Samba inválida (testparm). No se aplicaron cambios.",
                            "details": err,
                        }
                    ),
                    400,
                )

        # Backup y reemplazo atómico.
        shutil.copy2(smb_conf, backup)
        try:
            st = os.stat(smb_conf)
            os.chmod(tmp_path, st.st_mode)
        except Exception:
            pass
        os.replace(tmp_path, smb_conf)
        tmp_path = ""

    except Exception as e:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
        # Si ya hicimos backup pero falló después, intentamos rollback.
        if os.path.exists(backup):
            try:
                shutil.copy2(backup, smb_conf)
            except Exception:
                pass
        return (
            jsonify(
                {
                    "ok": False,
                    "message": "No se aplicaron cambios en Samba.",
                    "details": _truncate(str(e)),
                }
            ),
            500,
        )

    # En algunos hosts `reload` devuelve OK pero el share nuevo no queda accesible.
    # Usamos restart para aplicar cambios de forma fiable.
    _apply_samba_restart()
    return jsonify({"ok": True, "message": f"Share Samba {action}. (Backup: {backup})"})


@app.post("/api/samba/restart")
def api_samba_restart():
    ok, msg = _require_root()
    if not ok:
        return jsonify({"ok": False, "message": msg}), 403

    if not shutil.which("systemctl"):
        return jsonify({"ok": False, "message": "systemctl no disponible en este sistema."}), 500

    cp = _run(["systemctl", "restart", "smbd"], timeout_s=25)
    if cp.returncode != 0:
        err = _truncate((cp.stderr or cp.stdout or ""))
        return (
            jsonify({"ok": False, "message": "No se pudo reiniciar smbd.", "details": err}),
            500,
        )

    return jsonify({"ok": True, "message": "smbd reiniciado."})


@app.post("/api/samba/path")
def api_samba_path_toggle():
    ok, msg = _require_root()
    if not ok:
        return jsonify({"ok": False, "message": msg}), 403

    data = request.json or {}
    path = (data.get("path") or "").strip()
    enable = data.get("enable", None)
    if not path:
        return jsonify({"ok": False, "message": "Path requerido."}), 400
    if enable is None or not isinstance(enable, bool):
        return jsonify({"ok": False, "message": "Campo 'enable' inválido (usa true/false)."}), 400

    changed, details = _set_share_available_by_path(path, enable)
    # Si no existía el share, no es error: el objetivo es que sea seguro desconectar.
    return jsonify(
        {
            "ok": True,
            "message": "Share actualizado." if changed else "Sin cambios en Samba.",
            "details": details,
        }
    )


@app.post("/api/missing/remove")
def api_missing_remove():
    """Elimina configuración de un disco "no disponible": fstab + share Samba (si existe).

    - No requiere que el disco esté conectado.
    - Se basa en UUID (fstab) y mountpoint (para Samba).
    """

    ok, msg = _require_root()
    if not ok:
        return jsonify({"ok": False, "message": msg}), 403

    data = request.json or {}
    uuid = (data.get("uuid") or "").strip()
    mountpoint = (data.get("mountpoint") or "").strip()

    if not uuid or not UUID_RE.match(uuid):
        return jsonify({"ok": False, "message": "UUID inválido."}), 400
    if mountpoint and not _looks_safe_mountpoint(mountpoint):
        return jsonify({"ok": False, "message": "Punto de montaje inseguro; solo /mnt o /media."}), 400

    fstab_path = "/etc/fstab"
    try:
        with open(fstab_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except Exception as e:
        return jsonify({"ok": False, "message": f"No se pudo leer /etc/fstab: {e}"}), 500

    uuid_spec = f"UUID={uuid}"
    to_remove: list[int] = []
    for i, raw in enumerate(lines):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split()
        if len(parts) < 2:
            continue
        if parts[0] != uuid_spec:
            continue
        if mountpoint and parts[1] != mountpoint:
            continue
        to_remove.append(i)

    if not to_remove and mountpoint:
        # Si el usuario no pasó UUID bien, pero sí mountpoint, no hacemos nada.
        return jsonify({"ok": True, "message": "No se encontró entrada en fstab para eliminar."})

    changed_fstab = False
    # Borrar de atrás hacia delante para no desplazar índices.
    for idx in sorted(to_remove, reverse=True):
        _remove_diskmanager_fstab_block(lines, idx)
        changed_fstab = True

    backup_fstab = ""
    if changed_fstab:
        backup_fstab = f"/etc/fstab.bak.diskmanager.{int(time.time())}"
        try:
            shutil.copy2(fstab_path, backup_fstab)
            with open(fstab_path, "w", encoding="utf-8") as f:
                f.writelines(lines)
        except Exception as e:
            return jsonify({"ok": False, "message": f"No se pudo escribir /etc/fstab: {e}"}), 500

    samba_details = ""
    if mountpoint and os.path.exists("/etc/samba/smb.conf"):
        _changed, samba_details = _remove_share_block_by_path(mountpoint)

    details_parts: list[str] = []
    if changed_fstab:
        details_parts.append(f"fstab: eliminado (backup: {backup_fstab})")
    else:
        details_parts.append("fstab: sin cambios")
    if samba_details:
        details_parts.append(f"samba: {samba_details}")

    return jsonify({"ok": True, "message": "Configuración eliminada.", "details": "\n".join(details_parts)})


@app.post("/api/samba/share")
def api_samba_share_toggle():
    ok, msg = _require_root()
    if not ok:
        return jsonify({"ok": False, "message": msg}), 403

    data = request.json or {}
    name = (data.get("name") or "").strip()
    enable = data.get("enable", None)
    if not name:
        return jsonify({"ok": False, "message": "Nombre de share requerido."}), 400
    if enable is None or not isinstance(enable, bool):
        return jsonify({"ok": False, "message": "Campo 'enable' inválido (usa true/false)."}), 400

    smb_conf = "/etc/samba/smb.conf"
    if not os.path.exists(smb_conf):
        return jsonify({"ok": False, "message": "No existe /etc/samba/smb.conf (¿Samba instalado?)."}), 404

    try:
        with open(smb_conf, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except Exception as e:
        return jsonify({"ok": False, "message": f"No se pudo leer smb.conf: {e}"}), 500

    # Encontrar el bloque por nombre (case-insensitive), ignorando [global].
    def find_share_block_by_name(target_name: str) -> tuple[int, int] | None:
        start = None
        sec_name = None
        for i, raw in enumerate(lines):
            line = raw.strip()
            if line.startswith("[") and line.endswith("]"):
                # cerrar sección anterior
                if start is not None and sec_name and sec_name.lower() != "global":
                    if sec_name.lower() == target_name.lower():
                        return (start, i)
                start = i
                sec_name = line[1:-1].strip()
        if start is not None and sec_name and sec_name.lower() != "global":
            if sec_name.lower() == target_name.lower():
                return (start, len(lines))
        return None

    block = find_share_block_by_name(name)
    if not block:
        return jsonify({"ok": False, "message": "Share no encontrado en smb.conf."}), 404

    a, b = block
    desired = "yes" if enable else "no"

    # Buscar y actualizar (o insertar) la línea 'available'.
    changed = False
    inserted = False
    for i in range(a + 1, b):
        raw = lines[i]
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith(";"):
            continue
        if "=" not in stripped:
            continue
        k, _v = [p.strip() for p in stripped.split("=", 1)]
        if k.lower() == "available":
            indent = re.match(r"^\s*", raw).group(0)
            lines[i] = f"{indent}available = {desired}\n"
            changed = True
            break

    if not changed:
        # Insertar justo después del header del share para mantenerlo simple.
        lines.insert(a + 1, f"   available = {desired}\n")
        inserted = True
        changed = True

    if not changed:
        return jsonify({"ok": True, "message": "Sin cambios."})

    # Escribimos a un temporal, validamos con testparm, y solo entonces reemplazamos smb.conf.
    backup = f"/etc/samba/smb.conf.bak.diskmanager.{int(time.time())}"
    tmp_path = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            delete=False,
            dir=os.path.dirname(smb_conf),
            prefix=".smb.conf.diskmanager.",
        ) as tf:
            tf.writelines(lines)
            tmp_path = tf.name

        if shutil.which("testparm"):
            cp = _run(["testparm", "-s", tmp_path], timeout_s=25)
            if cp.returncode != 0:
                err = _truncate((cp.stderr or cp.stdout or ""))
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass
                return (
                    jsonify(
                        {
                            "ok": False,
                            "message": "Configuración Samba inválida (testparm). No se aplicaron cambios.",
                            "details": err,
                        }
                    ),
                    400,
                )

        shutil.copy2(smb_conf, backup)
        try:
            st = os.stat(smb_conf)
            os.chmod(tmp_path, st.st_mode)
        except Exception:
            pass
        os.replace(tmp_path, smb_conf)
        tmp_path = ""

    except Exception as e:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
        if os.path.exists(backup):
            try:
                shutil.copy2(backup, smb_conf)
            except Exception:
                pass
        return (
            jsonify({"ok": False, "message": "No se aplicaron cambios en Samba.", "details": _truncate(str(e))}),
            500,
        )

    # Aplicar cambios: para que se refleje siempre, preferimos restart.
    _apply_samba_restart()
    action = "habilitado" if enable else "deshabilitado"
    return jsonify({"ok": True, "message": f"Share {name} {action}. (Backup: {backup})"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8090, debug=True)
