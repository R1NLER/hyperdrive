function escapeHtml(s){
  return String(s)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function toast(msg){
  const area = document.getElementById("toastArea");
  const el = document.createElement("div");
  el.className = "toast align-items-center text-bg-dark border-0";
  el.role = "alert";
  el.ariaLive = "assertive";
  el.ariaAtomic = "true";
  const safe = escapeHtml(msg).replaceAll("\n", "<br>");
  el.innerHTML = `
    <div class="d-flex">
      <div class="toast-body">${safe}</div>
      <button type="button" class="btn-close btn-close-white me-2 m-auto" data-bs-dismiss="toast"></button>
    </div>`;
  area.appendChild(el);
  const t = new bootstrap.Toast(el, { delay: 1800 });
  t.show();
  el.addEventListener("hidden.bs.toast", () => el.remove());
}

function msgFromResponse(res){
  if (!res) return "Error";
  const base = res.message || "OK";
  const details = (res.details || "").trim();
  return details ? `${base}\n${details}` : base;
}

function getRememberedMountDir(id){
  return (localStorage.getItem(`diskmanager.mount_dir.${id}`) || "").trim();
}

function rememberMountDir(id, mountDir){
  const v = (mountDir || "").trim();
  if (v) localStorage.setItem(`diskmanager.mount_dir.${id}`, v);
}

function ensureMountDir(id){
  const existing = getRememberedMountDir(id);
  if (existing) return existing;
  const name = prompt("Nombre (una sola vez) para /mnt/<nombre>, fstab y Samba:", "");
  if (name === null) return null;
  const v = (name || "").trim();
  if (v) rememberMountDir(id, v);
  return v;
}

async function postJSON(url, data){
  try {
    const r = await fetch(url, {
      method: "POST",
      headers: {"Content-Type":"application/json"},
      body: JSON.stringify(data)
    });

    const ct = (r.headers.get("content-type") || "").toLowerCase();
    if (ct.includes("application/json")) {
      const j = await r.json();
      if (!r.ok && j && typeof j.ok === "undefined") {
        j.ok = false;
      }
      return j;
    }

    const text = await r.text();
    return {
      ok: false,
      message: `HTTP ${r.status}`,
      details: (text || "").slice(0, 1200)
    };
  } catch (e) {
    return { ok: false, message: "Error de red", details: String(e || "") };
  }
}

async function getJSON(url){
  try {
    const r = await fetch(url, { method: "GET" });
    const ct = (r.headers.get("content-type") || "").toLowerCase();
    if (ct.includes("application/json")) return await r.json();
    const text = await r.text();
    return { ok: false, message: `HTTP ${r.status}`, details: (text || "").slice(0, 1200) };
  } catch (e) {
    return { ok: false, message: "Error de red", details: String(e || "") };
  }
}

function findDiskRow(id){
  return document.querySelector(`tr[data-disk-id="${CSS.escape(String(id))}"]`);
}

function findDiskCard(id){
  return document.querySelector(`div[data-disk-card-id="${CSS.escape(String(id))}"]`);
}

function setBadge(el, {text, cls}){
  if (!el) return;
  el.className = `badge ${cls}`;
  el.textContent = text;
}

function buildActionsHtml(d){
  const u = String((d && d.uuid) ? d.uuid : "");
  const mp = String((d && d.mountpoint) ? d.mountpoint : "");

  const dropdownStart = `
    <div class="dropdown">
      <button class="btn btn-sm btn-outline-secondary dropdown-toggle" type="button" data-bs-toggle="dropdown" aria-expanded="false">Acciones</button>
      <ul class="dropdown-menu dropdown-menu-end">
  `;

  const dropdownEnd = `
      </ul>
    </div>
  `;

  if (!d) {
    return `${dropdownStart}<li><button class="dropdown-item" type="button" disabled>No disponible</button></li>${dropdownEnd}`;
  }

  if (d.missing) {
    if (u && u !== "-") {
      return `${dropdownStart}
        <li><button class="dropdown-item text-danger" type="button" onclick="removeMissingDisk('${escapeHtml(u)}', '${escapeHtml(mp)}')">Eliminar</button></li>
      ${dropdownEnd}`;
    }
    return `${dropdownStart}<li><button class="dropdown-item" type="button" disabled>No disponible</button></li>${dropdownEnd}`;
  }

  let items = "";

  if (d.mounted) {
    items += `
      <li><button class="dropdown-item" type="button" onclick="disconnectDisk('${escapeHtml(d.id)}', '${escapeHtml(mp)}')">Desconectar</button></li>
      <li><button class="dropdown-item text-danger" type="button" onclick="unmountDisk('${escapeHtml(d.id)}', '${escapeHtml(mp)}')">Desmontar</button></li>
    `;
  } else {
    if (String(d.kind || "") === "disk") {
      items += `
        <li><button class="dropdown-item" type="button" disabled title="El disco no tiene particiones. Formatea para crear una partición.">Sin partición</button></li>
      `;
    } else if (d.persistent) {
      items += `
        <li><button class="dropdown-item" type="button" onclick="reconnectDisk('${escapeHtml(d.id)}')">Reconectar</button></li>
      `;
    } else {
      items += `
        <li><button class="dropdown-item" type="button" onclick="mountDisk('${escapeHtml(d.id)}')">Montar</button></li>
      `;
    }
  }

  items += `<li><hr class="dropdown-divider"></li>`;

  const canFormat = (!d.mounted) && (!d.persistent) && (!d.samba);
  if (canFormat) {
    items += `
      <li><button class="dropdown-item text-danger" type="button" onclick="openFormatModal('${escapeHtml(d.id)}')">Formatear</button></li>
    `;
  } else {
    items += `
      <li><button class="dropdown-item text-danger" type="button" disabled title="Requiere: desmontado, no persistente y sin Samba">Formatear</button></li>
    `;
  }

  return `${dropdownStart}${items}${dropdownEnd}`;
}

function updateDiskRow(d){
  if (!d || !d.id || d.id === "-" || d.missing) return;
  const row = findDiskRow(d.id);
  if (!row) return;

  const mountedBadge = row.querySelector('[data-field="mountedBadge"]');
  const mountpointEl = row.querySelector('[data-field="mountpoint"]');
  const persistentEl = row.querySelector('[data-field="persistent"]');
  const sambaEl = row.querySelector('[data-field="samba"]');
  const actionsEl = row.querySelector('[data-field="actions"]');

  if (d.mounted) {
    setBadge(mountedBadge, { text: "Montado", cls: "text-bg-success" });
    if (mountpointEl) mountpointEl.textContent = d.mountpoint || "";
  } else {
    setBadge(mountedBadge, { text: "No montado", cls: "text-bg-secondary" });
    if (mountpointEl) mountpointEl.textContent = "";
  }

  if (d.persistent) {
    setBadge(persistentEl, { text: "Sí", cls: "text-bg-primary" });
  } else {
    setBadge(persistentEl, { text: "No", cls: "text-bg-light text-dark" });
  }

  if (d.samba) {
    setBadge(sambaEl, { text: "Compartido", cls: "text-bg-success" });
  } else {
    setBadge(sambaEl, { text: "No", cls: "text-bg-light text-dark" });
  }

  if (actionsEl) {
    actionsEl.innerHTML = buildActionsHtml(d);
  }
}

function updateDiskCard(d){
  if (!d || !d.id || d.id === "-" || d.missing) return;
  const card = findDiskCard(d.id);
  if (!card) return;

  const mountedBadge = card.querySelector('[data-field="mountedBadge"]');
  const mountpointEl = card.querySelector('[data-field="mountpoint"]');
  const persistentEl = card.querySelector('[data-field="persistent"]');
  const sambaEl = card.querySelector('[data-field="samba"]');
  const actionsEl = card.querySelector('[data-field="actions"]');

  if (d.mounted) {
    setBadge(mountedBadge, { text: "Montado", cls: "text-bg-success" });
    if (mountpointEl) mountpointEl.textContent = d.mountpoint || "";
  } else {
    setBadge(mountedBadge, { text: "No montado", cls: "text-bg-secondary" });
    if (mountpointEl) mountpointEl.textContent = "";
  }

  if (d.persistent) {
    setBadge(persistentEl, { text: "Sí", cls: "text-bg-primary" });
  } else {
    setBadge(persistentEl, { text: "No", cls: "text-bg-light text-dark" });
  }

  if (d.samba) {
    setBadge(sambaEl, { text: "Compartido", cls: "text-bg-success" });
  } else {
    setBadge(sambaEl, { text: "No", cls: "text-bg-light text-dark" });
  }

  if (actionsEl) {
    actionsEl.innerHTML = buildActionsHtml(d);
  }
}

async function refreshDiskRow(id){
  const res = await getJSON("/api/disks");
  if (!res || !res.ok) return;
  const d = (res.disks || []).find(x => String(x.id) === String(id));
  if (d) {
    updateDiskRow(d);
    updateDiskCard(d);
  }
}

let _formatOptionsCache = null;

async function _loadFormatOptions(){
  if (_formatOptionsCache) return _formatOptionsCache;
  const res = await getJSON("/api/format/options");
  if (!res || !res.ok) {
    _formatOptionsCache = [];
    return _formatOptionsCache;
  }
  _formatOptionsCache = Array.isArray(res.options) ? res.options : [];
  return _formatOptionsCache;
}

async function openFormatModal(id){
  const diskId = String(id);
  const modalEl = document.getElementById("formatModal");
  const idEl = document.getElementById("formatDiskId");
  const expectedEl = document.getElementById("formatExpected");
  const fstypeEl = document.getElementById("formatFstype");
  const labelEl = document.getElementById("formatLabel");
  const confirmEl = document.getElementById("formatConfirm");

  if (!modalEl || !idEl || !expectedEl || !fstypeEl || !labelEl || !confirmEl) {
    toast("UI de formateo no disponible.");
    return;
  }

  idEl.value = diskId;
  labelEl.value = "";
  confirmEl.value = "";
  const expected = "FORMATEAR";
  expectedEl.textContent = expected;

  const submitBtn = document.getElementById("formatSubmitBtn");
  if (submitBtn) {
    submitBtn.disabled = false;
    submitBtn.textContent = "Formatear";
  }

  fstypeEl.innerHTML = `<option value="">Cargando…</option>`;
  const opts = await _loadFormatOptions();
  if (!opts.length) {
    fstypeEl.innerHTML = `<option value="">No hay formatos disponibles (faltan mkfs.*)</option>`;
  } else {
    fstypeEl.innerHTML = `<option value="">Selecciona formato…</option>` +
      opts.map(o => `<option value="${escapeHtml(o.fstype)}">${escapeHtml(o.label || o.fstype)}</option>`).join("");
  }

  const modal = bootstrap.Modal.getOrCreateInstance(modalEl);
  modal.show();
  setTimeout(() => fstypeEl.focus(), 100);
}

async function submitFormatModal(){
  const modalEl = document.getElementById("formatModal");
  const idEl = document.getElementById("formatDiskId");
  const expectedEl = document.getElementById("formatExpected");
  const fstypeEl = document.getElementById("formatFstype");
  const labelEl = document.getElementById("formatLabel");
  const confirmEl = document.getElementById("formatConfirm");

  const id = (idEl && idEl.value) ? String(idEl.value) : "";
  const fstype = (fstypeEl && fstypeEl.value) ? String(fstypeEl.value).trim().toLowerCase() : "";
  const label = (labelEl && labelEl.value) ? String(labelEl.value).trim() : "";
  const expected = expectedEl ? String(expectedEl.textContent || "") : "FORMATEAR";
  const typed = (confirmEl && confirmEl.value) ? String(confirmEl.value).trim() : "";

  if (!id || !fstype) {
    toast("Selecciona un formato.");
    return;
  }
  if (!expected || typed !== expected) {
    toast("Confirmación incorrecta.");
    return;
  }

  const submitBtn = document.getElementById("formatSubmitBtn");
  const prevText = submitBtn ? submitBtn.textContent : "";
  if (submitBtn) {
    submitBtn.disabled = true;
    submitBtn.textContent = "Formateando…";
  }
  toast("Formateando… esto puede tardar varios minutos.");

  try {
    const res = await postJSON("/api/format", { id, fstype, label, confirm_text: expected });
    toast(msgFromResponse(res));

    if (res && res.ok && modalEl) {
      const modal = bootstrap.Modal.getOrCreateInstance(modalEl);
      modal.hide();
      // Después de formatear cambian fstype/uuid/label: recargamos la página.
      setTimeout(() => window.location.reload(), 700);
    }
  } finally {
    if (submitBtn) {
      submitBtn.disabled = false;
      submitBtn.textContent = prevText || "Formatear";
    }
  }
}

async function enableSamba(id){
  return await postJSON("/api/samba", {id, enable: true});
}

async function disableSamba(id, mountpoint){
  return await postJSON("/api/samba", {id, enable: false, path: mountpoint || ""});
}

async function restartSamba(){
  const res = await postJSON("/api/samba/restart", {});
  toast(msgFromResponse(res));
}

async function toggleSambaShare(name, enable){
  const res = await postJSON("/api/samba/share", {name, enable: !!enable});
  toast(msgFromResponse(res));
  if (res && res.ok) {
    // Refrescar para reflejar el estado (enabled) en la lista.
    setTimeout(() => window.location.reload(), 250);
  }
}

function toggleSambaShareFromEl(el){
  const name = (el && el.dataset && el.dataset.share) ? String(el.dataset.share) : "";
  return toggleSambaShare(name, !!(el && el.checked));
}

function forgetMountDir(id){
  localStorage.removeItem(`diskmanager.mount_dir.${id}`);
}

async function mountDisk(id){
  const mount_dir = ensureMountDir(id);
  if (mount_dir === null) return;
  const res = await postJSON("/api/mount", {id, mount_dir});
  toast(msgFromResponse(res));

  if (res.ok) {
    const per = await postJSON("/api/persist", {id, mount_dir, enable: true});
    toast(msgFromResponse(per));

    const smb = await enableSamba(id);
    toast(msgFromResponse(smb));

    // Actualizar solo la fila (sin recargar la página).
    setTimeout(() => refreshDiskRow(id), 250);
  }
}

// Re-monta un disco persistente (fstab) sin tocar Samba ni volver a pedir nombre.
async function reconnectDisk(id){
  const res = await postJSON("/api/mount", { id });
  toast(msgFromResponse(res));
  if (res && res.ok) {
    setTimeout(() => refreshDiskRow(id), 250);
  }
}
async function unmountDisk(id, mountpoint){
  const res = await postJSON("/api/unmount", {id});
  toast(msgFromResponse(res));

  if (res.ok) {
    const smb = await disableSamba(id, mountpoint);
    toast(msgFromResponse(smb));

    const per = await postJSON("/api/persist", {id, enable: false});
    toast(msgFromResponse(per));

    forgetMountDir(id);

    // Actualizar solo la fila (sin recargar la página).
    setTimeout(() => refreshDiskRow(id), 250);
  }
}

// Desconectar = desmontar de forma segura pero manteniendo la configuración (fstab + share).
// Para evitar que Samba escriba en el directorio vacío del rootfs, marcamos el share como unavailable.
async function disconnectDisk(id, mountpoint){
  const mp = String(mountpoint || "");
  if (mp) {
    const s = await postJSON("/api/samba/path", { path: mp, enable: false });
    toast(msgFromResponse(s));
  }

  const res = await postJSON("/api/unmount", { id });
  toast(msgFromResponse(res));
  if (res && res.ok) {
    setTimeout(() => refreshDiskRow(id), 250);
  }
}

let lastDisksState = null;

async function pollDisksState() {
  const path = window.location.pathname;
  // Check if we are on dashboard or disks page.
  // Dashboard is usually "/" or "/dashboard" (if configured).
  // Disks is "/disks".
  // We can also check for existence of specific DOM elements.
  const isDashboard = !!document.querySelector(".card-soft"); // Dashboard has cards
  const isDisksPage = !!document.querySelector("table"); // Disks page has table

  if (!isDashboard && !isDisksPage) return;

  try {
    const res = await getJSON("/api/disks");
    if (res && res.ok && res.disks) {
      // Sort to ensure stable signature
      const sorted = res.disks.sort((a, b) => (a.id || "").localeCompare(b.id || ""));
      // Create signature based on relevant fields
      const currentSignature = JSON.stringify(sorted.map(d => ({
        id: d.id,
        label: d.label,
        size: d.size,
        fstype: d.fstype,
        uuid: d.uuid,
        mounted: d.mounted,
        mountpoint: d.mountpoint,
        persistent: d.persistent,
        samba: d.samba
      })));

      if (lastDisksState === null) {
        lastDisksState = currentSignature;
      } else if (lastDisksState !== currentSignature) {
        console.log("Disk state changed.");
        
        // Stop polling to avoid loops
        if (pollInterval) clearInterval(pollInterval);
        
        // Update UI to ask for refresh
        const statusEl = document.getElementById("monitorStatus");
        const btn = document.getElementById("refreshBtn");
        
        if (statusEl) statusEl.classList.add("d-none");
        if (btn) {
          btn.classList.remove("d-none");
          // Optional: Animate or highlight
          btn.classList.add("animate__animated", "animate__pulse");
        } else {
          // Fallback if elements not found (e.g. dashboard)
          toast("Cambios detectados en los discos. Recarga la página.");
        }
      }
    }
  } catch (e) {
    console.error("Polling error", e);
  }
}

// Start polling
let pollInterval = setInterval(pollDisksState, 10000);
// Initial check
pollDisksState();

async function removeMissingDisk(uuid, mountpoint){
  const u = String(uuid || "").trim();
  const mp = String(mountpoint || "").trim();
  if (!u) {
    toast("UUID no disponible.");
    return;
  }
  if (!confirm(`Eliminar configuración del disco no disponible?\n\nUUID=${u}\nMontaje=${mp || "-"}\n\nSe quitará de fstab y de Samba (si existe).`)) {
    return;
  }
  const res = await postJSON("/api/missing/remove", { uuid: u, mountpoint: mp });
  toast(msgFromResponse(res));
  if (res && res.ok) {
    setTimeout(() => window.location.reload(), 600);
  }
}
