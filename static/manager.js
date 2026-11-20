(() => {
  const $ = (id) => document.getElementById(id);
  const LABEL = { free: 'Free', ordering: 'Ordering', waiting: 'Waiting', ready: 'Ready to serve' };
  let openId = null;

  async function loadFloor() {
    const { tables } = await api('/api/tables');
    const floor = $('floor');
    floor.replaceChildren();
    tables.forEach((t) => {
      const b = el('button', 'tile ' + t.status + (t.id === openId ? ' selected' : ''));
      b.append(el('div', 'tile-no', String(t.number)), el('div', null, LABEL[t.status]),
        el('div', 'tile-total', t.total ? money(t.total) : '—'));
      b.onclick = () => { openId = t.id; loadFloor(); loadDetail(); };
      floor.append(b);
    });
  }

  async function loadDetail() {
    if (openId == null) return;
    const data = await api('/api/tables/' + openId);
    $('detail').hidden = false;
    $('d-title').textContent = `Table ${data.table.number} — ${LABEL[data.table.status]}`;
    $('d-total').textContent = 'Open total: ' + money(data.table.total);
    const box = $('d-orders');
    box.replaceChildren();
    if (!data.orders.length) box.append(el('p', 'muted', 'No open orders.'));
    data.orders.forEach((o) => {
      const wrap = el('div', 'd-order');
      const head = el('div', 'cart-top');
      head.append(el('strong', null, `Order #${o.id}`), el('span', 'muted', `${elapsed(o.created_at)} · ${money(o.total)}`));
      wrap.append(head);
      if (o.note) wrap.append(el('div', 'note', 'Order note: ' + o.note));
      o.items.forEach((i) => {
        const row = el('div', 'o-item');
        const left = el('span', null, `${i.qty}× ${i.name}`);
        if (i.note) left.append(el('span', 'note', ' — ' + i.note));
        const right = el('span', 'o-right');
        right.append(el('span', 'badge st-' + i.status, i.status));
        if (i.status === 'ready') {
          const b = el('button', 'btn ok', 'Mark served');
          b.onclick = async () => {
            b.disabled = true;
            try { await api(`/api/items/${i.id}/status`, { status: 'served' }); }
            catch (e) { toast(e.message); }
            refresh();
          };
          right.append(b);
        }
        row.append(left, right);
        wrap.append(row);
      });
      box.append(wrap);
    });
  }

  function refresh() { loadFloor(); loadDetail(); }

  $('d-hide').onclick = () => { openId = null; $('detail').hidden = true; loadFloor(); };
  $('d-close').onclick = async () => {
    if (!confirm('Close this table? Its open orders are closed and the table is set to free.')) return;
    try { await api(`/api/tables/${openId}/close`, {}); toast('Table closed'); }
    catch (e) { toast(e.message); }
    refresh();
  };

  const socket = io();
  joinOnConnect(socket, { room: 'manager' });
  socket.on('connect', refresh);
  socket.on('tables:update', refresh);
  setInterval(loadDetail, 30000);
  refresh();
})();
