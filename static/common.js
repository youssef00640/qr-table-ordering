const CSRF = (document.querySelector('meta[name="csrf-token"]') || {}).content || '';
const CUR = document.body.dataset.currency || '';

function money(cents) { return CUR + (cents / 100).toFixed(2); }

// Always build DOM with textContent: notes are customer input.
function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

async function api(url, body) {
  const opts = body === undefined ? {} : {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-CSRFToken': CSRF },
    body: JSON.stringify(body),
  };
  const r = await fetch(url, opts);
  if (r.status === 401) { location.href = '/login?next=' + encodeURIComponent(location.pathname); }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) {
    const e = new Error(data.error || 'Something went wrong (' + r.status + ').');
    e.code = data.code;
    throw e;
  }
  return data;
}

function elapsed(iso) {
  const m = Math.max(0, Math.floor((Date.now() - new Date(iso).getTime()) / 60000));
  return m < 1 ? 'just now' : m + ' min';
}

let toastTimer;
function toast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, 3500);
}

// Join a room now and again after every reconnect.
function joinOnConnect(socket, payload) {
  socket.on('connect', () => socket.emit('join', payload));
}
