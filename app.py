import hmac
import math
import os
import secrets
import sys
import threading
import time
from decimal import Decimal, InvalidOperation
from functools import wraps

import qrcode
import qrcode.image.svg
from flask import (Blueprint, Flask, abort, current_app, flash, jsonify, redirect,
                   render_template, request, session, url_for)
from flask_socketio import SocketIO, join_room
from flask_wtf.csrf import CSRFError, CSRFProtect
from markupsafe import Markup
from sqlalchemy import inspect, text

from models import (ITEM_FLOW, STATIONS, Category, MenuItem, Order, OrderItem, Table, db,
                    new_token)

socketio = SocketIO()
csrf = CSRFProtect()
bp = Blueprint("main", __name__)

STAFF_ROOMS = ("kitchen", "bar", "manager")
MAX_QTY = 20
MAX_LINES = 50
NOTE_LEN = 200

LOGIN_MAX_FAILS = 5
LOGIN_WINDOW = 600   # seconds in which failures are counted
LOGIN_LOCKOUT = 600  # seconds an IP stays locked out
STALE_MSG = "This table was closed. Please rescan the QR code."
_login_lock = threading.Lock()


def _now():
    return time.monotonic()


# ---------------------------------------------------------------- app factory

def _load_secret(app):
    """Persist a random secret key so staff sessions survive restarts."""
    os.makedirs(app.instance_path, exist_ok=True)
    path = os.path.join(app.instance_path, "secret_key")
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(secrets.token_hex(32))
    with open(path) as f:
        return f.read().strip()


def _migrate():
    """Add columns introduced after the first release to an existing SQLite file."""
    insp = inspect(db.engine)
    if "tables" not in insp.get_table_names():
        return
    if "session_id" not in [c["name"] for c in insp.get_columns("tables")]:
        with db.engine.begin() as conn:
            conn.execute(text("ALTER TABLE tables ADD COLUMN session_id VARCHAR(64)"))
            for (table_id,) in conn.execute(text("SELECT id FROM tables")).fetchall():
                conn.execute(text("UPDATE tables SET session_id = :s WHERE id = :i"),
                             {"s": new_token(), "i": table_id})


def create_app(config=None, require_pin=True):
    app = Flask(__name__)
    app.config.update(
        SQLALCHEMY_DATABASE_URI=os.environ.get("DATABASE_URL", "sqlite:///app.db"),
        STAFF_PIN=os.environ.get("STAFF_PIN", ""),
        WTF_CSRF_TIME_LIMIT=None,  # token lives as long as the session (screens stay open all night)
        BASE_URL=os.environ.get("BASE_URL", "http://localhost:5000"),
        CURRENCY=os.environ.get("CURRENCY", "€"),
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_HTTPONLY=True,
    )
    if config:
        app.config.update(config)
    app.config["STAFF_PIN"] = str(app.config.get("STAFF_PIN") or "").strip()
    if require_pin and not app.config["STAFF_PIN"]:
        raise RuntimeError("STAFF_PIN is not set. Set the STAFF_PIN environment variable to the "
                           "staff PIN before starting the app (there is no default PIN).")
    app.secret_key = (app.config.get("SECRET_KEY") or os.environ.get("SECRET_KEY")
                      or _load_secret(app))

    db.init_app(app)
    socketio.init_app(app, async_mode="threading")
    csrf.init_app(app)
    app.register_blueprint(bp)
    app.extensions["pin_limiter"] = {"fails": {}, "locked": {}}

    @app.errorhandler(CSRFError)
    def csrf_error(e):
        msg = "Security token missing or expired. Reload the page and try again."
        if request.path.startswith("/api/"):
            return jsonify(error=msg), 400
        return msg, 400

    @app.template_filter("money")
    def money(cents):
        return f"{app.config['CURRENCY']}{cents / 100:.2f}"

    @app.context_processor
    def inject():
        return {"currency": app.config["CURRENCY"], "is_staff": bool(session.get("staff"))}

    with app.app_context():
        _migrate()
        db.create_all()
    return app


# ---------------------------------------------------------------- helpers

def staff_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("staff"):
            if request.path.startswith("/api/"):
                return jsonify(error="Staff login required."), 401
            return redirect(url_for("main.login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def err(message, code=400):
    return jsonify(error=message), code


def clean_note(value):
    if not isinstance(value, str):
        return None
    return value.strip()[:NOTE_LEN] or None


def table_by_token(token):
    table = Table.query.filter_by(token=token).first()
    if table is None:
        abort(404)
    return table


def item_json(i):
    return {"id": i.id, "name": i.menu_item.name, "qty": i.qty, "note": i.note,
            "station": i.station, "status": i.status, "unit_price": i.unit_price}


def order_json(order, items=None):
    return {
        "id": order.id,
        "table": order.table.number,
        "table_id": order.table_id,
        "status": order.status,
        "created_at": order.created_at.isoformat() + "Z",
        "note": order.note,
        "total": order.total,
        "items": [item_json(i) for i in (order.items if items is None else items)],
    }


def table_json(t):
    return {"id": t.id, "number": t.number, "status": t.status, "total": t.open_total}


def notify(table, stations=()):
    """Every change goes to the manager floor and the table's own room."""
    socketio.emit("tables:update", {"table_id": table.id}, to="manager")
    socketio.emit("orders:update", {"table_id": table.id}, to=f"table:{table.id}")
    for s in stations:
        socketio.emit("station:update", {}, to=s)


# ---------------------------------------------------------------- socket.io

@socketio.on("join")
def on_join(data):
    data = data if isinstance(data, dict) else {}
    room = data.get("room")
    if room in STAFF_ROOMS:
        if not session.get("staff"):
            return {"ok": False}
        join_room(room)
        return {"ok": True}
    token = data.get("token")
    if isinstance(token, str):
        table = Table.query.filter_by(token=token).first()
        if table:
            join_room(f"table:{table.id}")
            return {"ok": True}
    return {"ok": False}


# ---------------------------------------------------------------- customer

@bp.get("/")
def index():
    return redirect(url_for("main.manager"))


@bp.get("/t/<token>")
def customer(token):
    table = table_by_token(token)
    if table.status == "free":
        table.status = "ordering"
        db.session.commit()
        notify(table)
    categories = Category.query.order_by(Category.id).all()
    return render_template("customer.html", table=table, categories=categories)


def session_is_current(table, session_id):
    return isinstance(session_id, str) and hmac.compare_digest(
        session_id.encode(), table.session_id.encode())


@bp.get("/t/<token>/orders")
def customer_orders(token):
    table = table_by_token(token)
    sid = request.args.get("sid")
    if sid is not None and not session_is_current(table, sid):
        return jsonify(error=STALE_MSG, code="stale_session"), 409
    return jsonify(orders=[order_json(o) for o in table.open_orders], total=table.open_total)


@bp.post("/t/<token>/order")
@csrf.exempt  # guests have no login; the table token + session_id authorise the order
def place_order(token):
    table = table_by_token(token)
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return err("Invalid request.")
    if not session_is_current(table, data.get("session_id")):
        return jsonify(error=STALE_MSG, code="stale_session"), 409
    lines = data.get("items")
    if not isinstance(lines, list) or not lines:
        return err("Your cart is empty.")
    if len(lines) > MAX_LINES:
        return err("Too many items in one order.")

    new_items = []
    for line in lines:
        if not isinstance(line, dict):
            return err("Invalid item.")
        item_id, qty = line.get("id"), line.get("qty")
        if type(item_id) is not int or type(qty) is not int:
            return err("Invalid item.")
        if not 1 <= qty <= MAX_QTY:
            return err(f"Quantity must be between 1 and {MAX_QTY}.")
        menu_item = db.session.get(MenuItem, item_id)
        if menu_item is None:
            return err("An item in your cart is no longer on the menu.")
        if not menu_item.available:
            return err(f"{menu_item.name} is currently unavailable.", 409)
        new_items.append(OrderItem(
            menu_item=menu_item, qty=qty, note=clean_note(line.get("note")),
            station=menu_item.category.station, status="new",
            unit_price=menu_item.price,  # price always comes from the DB
        ))

    order = Order(table=table, status="new", note=clean_note(data.get("note")), items=new_items)
    db.session.add(order)
    order.refresh_status()
    table.refresh_status()
    db.session.commit()

    # Split by station: each station only receives its own items.
    for station in STATIONS:
        items = [i for i in order.items if i.station == station]
        if items:
            socketio.emit("order:new", order_json(order, items), to=station)
    notify(table)
    return jsonify(order=order_json(order)), 201


# ---------------------------------------------------------------- staff auth

def _lockout_remaining(ip):
    """Seconds this IP is still locked out (0 if not locked)."""
    state = current_app.extensions["pin_limiter"]
    with _login_lock:
        remaining = state["locked"].get(ip, 0) - _now()
        if remaining <= 0:
            state["locked"].pop(ip, None)
            return 0
        return remaining


def _record_failure(ip):
    """Count a failed attempt; returns True if the IP is now locked out."""
    state = current_app.extensions["pin_limiter"]
    now = _now()
    with _login_lock:
        fails = [t for t in state["fails"].get(ip, []) if now - t < LOGIN_WINDOW] + [now]
        if len(fails) >= LOGIN_MAX_FAILS:
            state["fails"].pop(ip, None)
            state["locked"][ip] = now + LOGIN_LOCKOUT
            return True
        state["fails"][ip] = fails
        return False


def _lockout_message(seconds):
    minutes = max(1, math.ceil(seconds / 60))
    return (f"Too many failed PIN attempts. Login is locked for this device - "
            f"try again in {minutes} minute{'s' if minutes != 1 else ''}.")


@bp.route("/login", methods=["GET", "POST"])
def login():
    error, code = None, 200
    ip = request.remote_addr or "unknown"
    if request.method == "POST":
        remaining = _lockout_remaining(ip)
        pin = request.form.get("pin", "")
        staff_pin = current_app.config["STAFF_PIN"]
        if remaining:
            error, code = _lockout_message(remaining), 429
        elif staff_pin and hmac.compare_digest(pin.encode(), staff_pin.encode()):
            with _login_lock:
                current_app.extensions["pin_limiter"]["fails"].pop(ip, None)
            session.clear()
            session["staff"] = True
            session.permanent = True
            nxt = request.form.get("next", "")
            if not nxt.startswith("/") or nxt.startswith("//") or "\\" in nxt:
                nxt = url_for("main.manager")
            return redirect(nxt)
        elif _record_failure(ip):
            error, code = _lockout_message(LOGIN_LOCKOUT), 429
        else:
            error = "Wrong PIN."
    return render_template("login.html", error=error, next=request.values.get("next", "")), code


@bp.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("main.login"))


# ---------------------------------------------------------------- stations

@bp.get("/kitchen")
@staff_required
def kitchen():
    return render_template("station.html", station="kitchen", title="Kitchen")


@bp.get("/bar")
@staff_required
def bar():
    return render_template("station.html", station="bar", title="Bar")


@bp.get("/api/station/<station>")
@staff_required
def api_station(station):
    if station not in STATIONS:
        abort(404)
    cards = []
    orders = Order.query.filter(Order.status != "closed").order_by(Order.created_at).all()
    for o in orders:
        items = [i for i in o.items if i.station == station and i.status != "served"]
        if items:
            cards.append(order_json(o, items))
    return jsonify(orders=cards)


@bp.post("/api/items/<int:item_id>/status")
@staff_required
def api_item_status(item_id):
    item = db.session.get(OrderItem, item_id)
    if item is None:
        return err("Item not found.", 404)
    data = request.get_json(silent=True)
    target = data.get("status") if isinstance(data, dict) else None
    if target not in ITEM_FLOW:
        return err("Invalid status.")
    if item.order.status == "closed":
        return err("This order is closed.", 409)
    idx = ITEM_FLOW.index(item.status)
    if idx + 1 >= len(ITEM_FLOW) or ITEM_FLOW[idx + 1] != target:
        return err(f"Cannot move an item from '{item.status}' to '{target}'.", 409)
    item.status = target
    item.order.refresh_status()
    item.order.table.refresh_status()
    db.session.commit()
    notify(item.order.table, stations=[item.station])
    return jsonify(item=item_json(item), order_status=item.order.status)


# ---------------------------------------------------------------- manager

@bp.get("/manager")
@staff_required
def manager():
    return render_template("manager.html")


@bp.get("/api/tables")
@staff_required
def api_tables():
    return jsonify(tables=[table_json(t) for t in Table.query.order_by(Table.number)])


@bp.get("/api/tables/<int:table_id>")
@staff_required
def api_table(table_id):
    table = db.session.get(Table, table_id)
    if table is None:
        return err("Table not found.", 404)
    return jsonify(table=table_json(table), orders=[order_json(o) for o in table.open_orders])


@bp.post("/api/tables/<int:table_id>/close")
@staff_required
def api_close_table(table_id):
    table = db.session.get(Table, table_id)
    if table is None:
        return err("Table not found.", 404)
    for o in table.open_orders:
        o.status = "closed"
    table.status = "free"
    table.session_id = new_token()  # guest pages opened before now can no longer order
    db.session.commit()
    notify(table, stations=STATIONS)
    return jsonify(table=table_json(table))


# ---------------------------------------------------------------- menu admin

def _menu_form():
    """Validate the add/edit form. Returns (values, error)."""
    name = request.form.get("name", "").strip()[:120]
    description = request.form.get("description", "").strip()[:300]
    if not name:
        return None, "Name is required."
    try:
        price = Decimal(request.form.get("price", "").replace(",", "."))
        if not price.is_finite() or price < 0 or price > 100000:
            raise InvalidOperation
    except InvalidOperation:
        return None, "Price must be a positive number."
    try:
        category = db.session.get(Category, int(request.form.get("category_id", "")))
    except ValueError:
        category = None
    if category is None:
        return None, "Choose a category."
    return {"name": name, "description": description,
            "price": int((price * 100).to_integral_value()), "category": category}, None


@bp.get("/manager/menu")
@staff_required
def menu_admin():
    return render_template("menu_admin.html", categories=Category.query.order_by(Category.id).all())


@bp.post("/manager/menu/add")
@staff_required
def menu_add():
    values, error = _menu_form()
    if error:
        flash(error)
    else:
        db.session.add(MenuItem(**values))
        db.session.commit()
        flash(f"Added {values['name']}.")
    return redirect(url_for("main.menu_admin"))


@bp.post("/manager/menu/<int:item_id>/edit")
@staff_required
def menu_edit(item_id):
    item = db.session.get(MenuItem, item_id) or abort(404)
    values, error = _menu_form()
    if error:
        flash(error)
    else:
        for k, v in values.items():
            setattr(item, k, v)
        db.session.commit()
        flash(f"Saved {item.name}.")
    return redirect(url_for("main.menu_admin"))


@bp.post("/manager/menu/<int:item_id>/toggle")
@staff_required
def menu_toggle(item_id):
    item = db.session.get(MenuItem, item_id) or abort(404)
    item.available = not item.available
    db.session.commit()
    return redirect(url_for("main.menu_admin"))


# ---------------------------------------------------------------- category admin

def _category_form():
    name = request.form.get("name", "").strip()[:80]
    station = request.form.get("station")
    if not name:
        return None, "Category name is required."
    if station not in STATIONS:
        return None, "Station must be kitchen or bar."
    return {"name": name, "station": station}, None


@bp.post("/manager/categories/add")
@staff_required
def category_add():
    values, error = _category_form()
    if error:
        flash(error)
    else:
        db.session.add(Category(**values))
        db.session.commit()
        flash(f"Added category {values['name']}.")
    return redirect(url_for("main.menu_admin"))


@bp.post("/manager/categories/<int:category_id>/edit")
@staff_required
def category_edit(category_id):
    category = db.session.get(Category, category_id) or abort(404)
    values, error = _category_form()
    if error:
        flash(error)
    else:
        category.name, category.station = values["name"], values["station"]
        db.session.commit()
        flash(f"Saved category {category.name}.")
    return redirect(url_for("main.menu_admin"))


@bp.post("/manager/categories/<int:category_id>/delete")
@staff_required
def category_delete(category_id):
    category = db.session.get(Category, category_id) or abort(404)
    if category.items:
        flash(f"Cannot delete {category.name}: it still has {len(category.items)} item(s). "
              "Move them to another category first.")
    else:
        db.session.delete(category)
        db.session.commit()
        flash(f"Deleted category {category.name}.")
    return redirect(url_for("main.menu_admin"))


# ---------------------------------------------------------------- QR sheet

@bp.get("/manager/qr")
@staff_required
def qr_sheet():
    base = current_app.config["BASE_URL"].rstrip("/")
    codes = []
    for t in Table.query.order_by(Table.number):
        url = f"{base}/t/{t.token}"
        img = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2)
        codes.append({"number": t.number, "url": url,
                      "svg": Markup(img.to_string(encoding="unicode"))})
    return render_template("qr.html", codes=codes, base=base)


if __name__ == "__main__":
    try:
        app = create_app()
    except RuntimeError as exc:
        sys.exit(f"ERROR: {exc}")
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "5000"))
    print(f" * QR codes point to {app.config['BASE_URL']}  (set BASE_URL to change)")
    socketio.run(app, host=host, port=port, allow_unsafe_werkzeug=True)
