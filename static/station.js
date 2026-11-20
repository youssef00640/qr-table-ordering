(() => {
  const station = document.body.dataset.station;
  const cards = document.getElementById('cards');
  const NEXT = { new: ['preparing', 'Start'], preparing: ['ready', 'Ready'] };
  let orders = [];

  // ---- sound (browsers need one tap before audio may play)
  let audio = null;
  const soundBtn = document.getElementById('sound');
  soundBtn.onclick = () => {
    audio = audio || new (window.AudioContext || window.webkitAudioContext)();
    audio.resume();
    soundBtn.textContent = '🔔 Sound on';
    beep();
  };
  function beep() {
    if (!audio) return;
    [0, 0.2].forEach((offset) => {
      const osc = audio.createOscillator();
      const gain = audio.createGain();
      osc.frequency.value = 880;
      gain.gain.setValueAtTime(0.3, audio.currentTime + offset);
      gain.gain.exponentialRampToValueAtTime(0.001, audio.currentTime + offset + 0.15);
      osc.connect(gain).connect(audio.destination);
      osc.start(audio.currentTime + offset);
      osc.stop(audio.currentTime + offset + 0.15);
    });
  }

  function render() {
    cards.replaceChildren();
    document.getElementById('empty').hidden = orders.length > 0;
    orders.forEach((o) => {
      const allReady = o.items.every((i) => i.status === 'ready');
      const card = el('div', 'card order-card' + (allReady ? ' done' : ''));
      const head = el('div', 'cart-top');
      head.append(el('strong', 'table-no', `Table ${o.table}`), el('span', 'muted', `#${o.id} · ${elapsed(o.created_at)}`));
      card.append(head);
      if (o.note) card.append(el('div', 'note', 'Order note: ' + o.note));
      o.items.forEach((i) => {
        const row = el('div', 'o-item');
        const left = el('div');
        left.append(el('div', 'mi-name', `${i.qty}× ${i.name}`));
        if (i.note) left.append(el('div', 'note', i.note));
        row.append(left);
        const next = NEXT[i.status];
        if (next) {
          const b = el('button', 'btn ' + (i.status === 'new' ? 'primary' : 'ok'), next[1]);
          b.onclick = async () => {
            b.disabled = true;
            try { await api(`/api/items/${i.id}/status`, { status: next[0] }); }
            catch (e) { toast(e.message); }
            load();
          };
          row.append(b);
        } else {
          row.append(el('span', 'badge st-ready', 'Ready ✓'));
        }
        card.append(row);
      });
      cards.append(card);
    });
  }

  async function load() {
    orders = (await api(`/api/station/${station}`)).orders;
    render();
  }

  const socket = io();
  joinOnConnect(socket, { room: station });
  socket.on('connect', load);
  socket.on('order:new', () => { beep(); load(); });
  socket.on('station:update', load);
  setInterval(render, 20000); // keep elapsed times fresh
  load();
})();
