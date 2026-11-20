(() => {
  const token = document.body.dataset.token;
  const sessionId = document.body.dataset.session;
  let stale = false;

  // The manager closed this table: stop everything and ask the guest to rescan.
  function showStale(msg) {
    stale = true;
    cart.clear();
    renderCart();
    document.getElementById('orders').hidden = true;
    document.querySelectorAll('.add').forEach((b) => { b.disabled = true; });
    const box = document.getElementById('stale');
    box.textContent = msg;
    box.hidden = false;
    box.scrollIntoView({ behavior: 'smooth' });
  }
  const cart = new Map(); // id -> {name, price, qty, note}
  const $ = (id) => document.getElementById(id);
  const LABEL = { new: 'Received', preparing: 'Preparing', ready: 'Ready', served: 'Served' };

  // ---- cart
  document.querySelectorAll('.add').forEach((b) => b.addEventListener('click', () => {
    const id = Number(b.dataset.id);
    const line = cart.get(id) || { name: b.dataset.name, price: Number(b.dataset.price), qty: 0, note: '' };
    if (line.qty < 20) line.qty += 1;
    cart.set(id, line);
    renderCart();
    toast(line.name + ' added');
  }));

  function renderCart() {
    let count = 0, total = 0;
    const lines = $('cart-lines');
    lines.replaceChildren();
    cart.forEach((line, id) => {
      count += line.qty;
      total += line.qty * line.price;
      const row = el('div', 'cart-line');
      const top = el('div', 'cart-top');
      top.append(el('span', 'mi-name', line.name), el('span', null, money(line.qty * line.price)));
      const ctl = el('div', 'qty');
      const minus = el('button', 'btn', '−');
      const plus = el('button', 'btn', '+');
      minus.onclick = () => { line.qty -= 1; if (line.qty <= 0) cart.delete(id); renderCart(); };
      plus.onclick = () => { if (line.qty < 20) line.qty += 1; renderCart(); };
      ctl.append(minus, el('span', null, String(line.qty)), plus);
      const note = el('input');
      note.placeholder = 'Note (e.g. no onions)';
      note.maxLength = 200;
      note.value = line.note;
      note.oninput = () => { line.note = note.value; };
      row.append(top, ctl, note);
      lines.append(row);
    });
    const bar = $('cart-bar');
    bar.hidden = count === 0 || !$('cart-panel').hidden;
    bar.textContent = `View cart · ${count} item${count === 1 ? '' : 's'} · ${money(total)}`;
    $('place').textContent = `Place order · ${money(total)}`;
    if (count === 0) $('cart-panel').hidden = true;
  }

  $('cart-bar').onclick = () => { $('cart-panel').hidden = false; $('cart-bar').hidden = true; };
  $('cart-close').onclick = () => { $('cart-panel').hidden = true; renderCart(); };

  $('place').onclick = async () => {
    const btn = $('place');
    const errBox = $('cart-error');
    errBox.hidden = true;
    btn.disabled = true;
    try {
      const items = [...cart].map(([id, l]) => ({ id, qty: l.qty, note: l.note }));
      await api(`/t/${token}/order`, { items, note: $('order-note').value, session_id: sessionId });
      cart.clear();
      $('order-note').value = '';
      renderCart();
      toast('Order placed — thank you!');
      await loadOrders();
      $('orders').scrollIntoView({ behavior: 'smooth' });
    } catch (e) {
      if (e.code === 'stale_session') { showStale(e.message); return; }
      errBox.textContent = e.message;
      errBox.hidden = false;
    } finally {
      btn.disabled = false;
    }
  };

  // ---- live order status
  async function loadOrders() {
    if (stale) return;
    let data;
    try {
      data = await api(`/t/${token}/orders?sid=${encodeURIComponent(sessionId)}`);
    } catch (e) {
      if (e.code === 'stale_session') showStale(e.message);
      return;
    }
    const list = $('order-list');
    list.replaceChildren();
    data.orders.forEach((o) => {
      const card = el('div', 'card');
      const head = el('div', 'cart-top');
      head.append(el('strong', null, `Order #${o.id}`), el('span', 'muted', elapsed(o.created_at)));
      card.append(head);
      o.items.forEach((i) => {
        const row = el('div', 'o-item');
        const name = el('span', null, `${i.qty}× ${i.name}`);
        if (i.note) name.append(el('span', 'note', ' — ' + i.note));
        row.append(name, el('span', 'badge st-' + i.status, LABEL[i.status]));
        card.append(row);
      });
      list.append(card);
    });
    $('table-total').textContent = money(data.total);
    $('orders').hidden = data.orders.length === 0;
  }

  const socket = io();
  joinOnConnect(socket, { token });
  socket.on('connect', loadOrders);
  socket.on('orders:update', loadOrders);
  loadOrders();
})();
