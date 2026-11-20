import os
import re
import sqlite3
import subprocess
import sys

import pytest
from flask import g, has_app_context

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module  # noqa: E402
from app import create_app, socketio  # noqa: E402
from models import Category, MenuItem, Order, OrderItem, Table, db  # noqa: E402

PIN = "4321"


@pytest.fixture()
def app():
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite://",
                      "STAFF_PIN": PIN, "SECRET_KEY": "test"})
    with app.app_context():
        food = Category(name="Food", station="kitchen")
        drinks = Category(name="Drinks", station="bar")
        food.items.append(MenuItem(name="Burger", price=1200))
        drinks.items.append(MenuItem(name="Beer", price=500))
        drinks.items.append(MenuItem(name="Rare Whisky", price=900, available=False))
        db.session.add_all([food, drinks, Table(number=1, token="tok1"), Table(number=2, token="tok2")])
        db.session.commit()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture()
def client(app):
    return app.test_client()


def csrf_token(c, path="/login"):
    """Read the CSRF token from the page's meta tag, like the browser JS does."""
    # The app fixture keeps one app context open for the whole test, so Flask-WTF's
    # per-request token cache on `g` would leak between test clients. Clear it first.
    if has_app_context():
        g.pop("csrf_token", None)
    html = c.get(path).get_data(as_text=True)
    return re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)


def login(c, pin=PIN):
    return c.post("/login", data={"pin": pin, "csrf_token": csrf_token(c)})


@pytest.fixture()
def staff(app):
    """Logged-in staff client that sends the CSRF header on every request."""
    c = app.test_client()
    assert login(c).status_code == 302
    c.environ_base["HTTP_X_CSRFTOKEN"] = csrf_token(c, "/manager")
    return c


def ids():
    return {m.name: m.id for m in MenuItem.query.all()}


def order(client, items, token="tok1", **extra):
    table = Table.query.filter_by(token=token).first()
    sid = table.session_id if table else "x"
    return client.post(f"/t/{token}/order", json={"session_id": sid, "items": items, **extra})


def room_client(app, flask_client, payload):
    sc = socketio.test_client(app, flask_test_client=flask_client)
    ack = sc.emit("join", payload, callback=True)
    sc.get_received()
    return sc, ack


def events(sc, name):
    return [e["args"][0] for e in sc.get_received() if e["name"] == name]


# ------------------------------------------------------------ order creation

def test_order_creation_uses_db_prices(client):
    m = ids()
    r = order(client, [{"id": m["Burger"], "qty": 2, "note": " no onions ", "price": 1},
                       {"id": m["Beer"], "qty": 1}], note="birthday")
    assert r.status_code == 201
    data = r.get_json()["order"]
    assert data["total"] == 2 * 1200 + 500  # client-sent price ignored
    assert data["status"] == "new"
    o = db.session.get(Order, data["id"])
    assert o.note == "birthday"
    assert {(i.menu_item.name, i.qty, i.note, i.unit_price) for i in o.items} == {
        ("Burger", 2, "no onions", 1200), ("Beer", 1, None, 500)}
    assert o.table.status == "waiting"


def test_unavailable_item_rejected_and_nothing_saved(client):
    m = ids()
    r = order(client, [{"id": m["Beer"], "qty": 1}, {"id": m["Rare Whisky"], "qty": 1}])
    assert r.status_code == 409
    assert "unavailable" in r.get_json()["error"]
    assert Order.query.count() == 0 and OrderItem.query.count() == 0


@pytest.mark.parametrize("items", [
    [], None, "x", [{"id": 9999, "qty": 1}], [{"id": 1, "qty": 0}], [{"id": 1, "qty": -3}],
    [{"id": 1, "qty": 21}], [{"id": 1, "qty": "2"}], [{"id": 1, "qty": 1.5}], [{"qty": 1}], ["x"],
])
def test_invalid_orders_rejected(client, items):
    assert order(client, items).status_code == 400
    assert Order.query.count() == 0


def test_unknown_token_404(client):
    assert client.get("/t/nope").status_code == 404
    assert order(client, [{"id": 1, "qty": 1}], token="nope").status_code == 404


def test_customer_page_marks_table_ordering(client):
    assert client.get("/t/tok1").status_code == 200
    assert Table.query.filter_by(token="tok1").one().status == "ordering"


def test_customer_sees_only_own_table_orders(client):
    m = ids()
    order(client, [{"id": m["Beer"], "qty": 1}], token="tok1")
    assert len(client.get("/t/tok1/orders").get_json()["orders"]) == 1
    assert client.get("/t/tok2/orders").get_json()["orders"] == []


# ------------------------------------------------------------ station routing

def test_items_get_station_from_category(client, staff):
    m = ids()
    order(client, [{"id": m["Burger"], "qty": 1}, {"id": m["Beer"], "qty": 2}])
    kitchen = staff.get("/api/station/kitchen").get_json()["orders"]
    bar = staff.get("/api/station/bar").get_json()["orders"]
    assert [i["name"] for i in kitchen[0]["items"]] == ["Burger"]
    assert [i["name"] for i in bar[0]["items"]] == ["Beer"]
    assert kitchen[0]["table"] == 1


def test_socket_emits_split_by_station(app, client, staff):
    m = ids()
    kitchen, ack_k = room_client(app, staff, {"room": "kitchen"})
    bar, ack_b = room_client(app, staff, {"room": "bar"})
    manager, _ = room_client(app, staff, {"room": "manager"})
    table, ack_t = room_client(app, client, {"token": "tok1"})
    other, _ = room_client(app, client, {"token": "tok2"})
    assert ack_k["ok"] and ack_b["ok"] and ack_t["ok"]

    order(client, [{"id": m["Burger"], "qty": 1}, {"id": m["Beer"], "qty": 2}])
    k, b = events(kitchen, "order:new"), events(bar, "order:new")
    assert [i["name"] for i in k[0]["items"]] == ["Burger"]
    assert [i["name"] for i in b[0]["items"]] == ["Beer"]
    assert len(events(manager, "tables:update")) == 1
    assert len(events(table, "orders:update")) == 1
    assert other.get_received() == []

    # drinks-only order never reaches the kitchen
    order(client, [{"id": m["Beer"], "qty": 1}])
    assert events(kitchen, "order:new") == []
    assert len(events(bar, "order:new")) == 1


def test_guest_cannot_join_staff_rooms(app, client):
    m = ids()
    sc, ack = room_client(app, client, {"room": "kitchen"})
    assert ack["ok"] is False
    order(client, [{"id": m["Burger"], "qty": 1}])
    assert sc.get_received() == []


# ------------------------------------------------------------ status transitions

def set_status(c, item_id, status):
    return c.post(f"/api/items/{item_id}/status", json={"status": status})


def test_full_item_flow_and_derived_statuses(app, client, staff):
    m = ids()
    oid = order(client, [{"id": m["Burger"], "qty": 1}, {"id": m["Beer"], "qty": 1}]).get_json()["order"]["id"]
    o = db.session.get(Order, oid)
    burger, beer = sorted(o.items, key=lambda i: i.station != "kitchen")
    manager, _ = room_client(app, staff, {"room": "manager"})
    table, _ = room_client(app, client, {"token": "tok1"})

    assert set_status(staff, burger.id, "preparing").status_code == 200
    assert len(events(manager, "tables:update")) == 1
    assert len(events(table, "orders:update")) == 1
    db.session.refresh(o)
    assert o.status == "preparing" and o.table.status == "waiting"

    set_status(staff, burger.id, "ready")
    db.session.refresh(o); db.session.refresh(o.table)
    assert o.table.status == "ready"  # something can be served

    set_status(staff, burger.id, "served")
    for s in ("preparing", "ready"):
        set_status(staff, beer.id, s)
    db.session.refresh(o)
    assert o.status == "ready"
    assert set_status(staff, beer.id, "served").status_code == 200
    db.session.refresh(o); db.session.refresh(o.table)
    assert o.status == "served" and o.table.status == "ordering"
    # served items leave the station screens
    assert staff.get("/api/station/kitchen").get_json()["orders"] == []


@pytest.mark.parametrize("target", ["ready", "served", "new", "bogus", None])
def test_invalid_transitions_rejected(client, staff, target):
    m = ids()
    order(client, [{"id": m["Burger"], "qty": 1}])
    item = OrderItem.query.one()
    assert set_status(staff, item.id, target).status_code in (400, 409)
    db.session.refresh(item)
    assert item.status == "new"


def test_close_table_resets_to_free(client, staff):
    m = ids()
    order(client, [{"id": m["Burger"], "qty": 1}])
    t = Table.query.filter_by(token="tok1").one()
    assert staff.get("/api/tables").get_json()["tables"][0]["total"] == 1200
    assert staff.post(f"/api/tables/{t.id}/close").status_code == 200
    db.session.refresh(t)
    assert t.status == "free" and t.open_total == 0
    assert Order.query.one().status == "closed"
    assert client.get("/t/tok1/orders").get_json()["orders"] == []
    assert staff.get("/api/station/kitchen").get_json()["orders"] == []
    assert set_status(staff, OrderItem.query.one().id, "preparing").status_code == 409


# ------------------------------------------------------------ auth

@pytest.mark.parametrize("path", ["/kitchen", "/bar", "/manager", "/manager/menu", "/manager/qr"])
def test_staff_pages_require_pin(client, staff, path):
    r = client.get(path)
    assert r.status_code == 302 and "/login" in r.headers["Location"]
    assert staff.get(path).status_code == 200


def test_staff_api_requires_pin(client):
    # a guest can get a valid CSRF token from any page, but still has no staff session
    client.environ_base["HTTP_X_CSRFTOKEN"] = csrf_token(client, "/t/tok1")
    assert client.get("/api/tables").status_code == 401
    assert client.post("/api/items/1/status", json={"status": "preparing"}).status_code == 401
    assert client.post("/api/tables/1/close").status_code == 401


def test_wrong_pin(client):
    r = login(client, "0000")
    assert r.status_code == 200 and b"Wrong PIN" in r.data
    assert client.get("/manager").status_code == 302


def test_qr_sheet_uses_base_url(app, staff):
    app.config["BASE_URL"] = "http://192.168.1.50:5000/"
    html = staff.get("/manager/qr").get_data(as_text=True)
    assert "http://192.168.1.50:5000/t/tok1" in html and html.count("<svg") == 2


def test_menu_admin_add_edit_toggle(staff):
    cat = Category.query.filter_by(station="bar").one()
    staff.post("/manager/menu/add", data={"name": "Gin Tonic", "description": "", "price": "8,50", "category_id": cat.id})
    item = MenuItem.query.filter_by(name="Gin Tonic").one()
    assert item.price == 850 and item.available
    staff.post(f"/manager/menu/{item.id}/edit", data={"name": "G&T", "description": "x", "price": "9", "category_id": cat.id})
    staff.post(f"/manager/menu/{item.id}/toggle")
    db.session.refresh(item)
    assert (item.name, item.price, item.available) == ("G&T", 900, False)
    staff.post("/manager/menu/add", data={"name": "Bad", "price": "abc", "category_id": cat.id})
    assert MenuItem.query.filter_by(name="Bad").count() == 0


# ------------------------------------------------------------ 1. CSRF

def test_csrf_required_on_staff_fetch_posts(app, client):
    m = ids()
    order(client, [{"id": m["Burger"], "qty": 1}])
    item, table = OrderItem.query.one(), Table.query.filter_by(token="tok1").one()
    c = app.test_client()
    assert login(c).status_code == 302  # logged in, but sends no CSRF header
    r = c.post(f"/api/items/{item.id}/status", json={"status": "preparing"})
    assert r.status_code == 400 and "token" in r.get_json()["error"].lower()
    assert c.post(f"/api/tables/{table.id}/close").status_code == 400
    c.environ_base["HTTP_X_CSRFTOKEN"] = "forged"
    assert c.post(f"/api/tables/{table.id}/close").status_code == 400
    db.session.refresh(item)
    assert item.status == "new" and Order.query.one().status == "new"
    c.environ_base["HTTP_X_CSRFTOKEN"] = csrf_token(c, "/manager")
    assert c.post(f"/api/items/{item.id}/status", json={"status": "preparing"}).status_code == 200


def test_csrf_required_on_staff_forms(app):
    c = app.test_client()
    assert c.post("/login", data={"pin": PIN}).status_code == 400  # login form itself
    assert c.get("/manager").status_code == 302
    assert login(c).status_code == 302
    cat = Category.query.first()
    item = MenuItem.query.filter_by(name="Burger").one()
    form = {"name": "X", "description": "", "price": "1", "category_id": cat.id}
    for path, data in [
        ("/manager/menu/add", form),
        (f"/manager/menu/{item.id}/edit", form),
        (f"/manager/menu/{item.id}/toggle", {}),
        ("/manager/categories/add", {"name": "X", "station": "bar"}),
        (f"/manager/categories/{cat.id}/edit", {"name": "X", "station": "bar"}),
        (f"/manager/categories/{cat.id}/delete", {}),
        ("/logout", {}),
    ]:
        assert c.post(path, data=data).status_code == 400, path
    db.session.refresh(item)
    assert item.name == "Burger" and item.available
    assert MenuItem.query.count() == 3 and Category.query.count() == 2
    assert c.get("/manager").status_code == 200  # logout was blocked too
    # with the hidden form field everything works
    tok = csrf_token(c, "/manager/menu")
    assert c.post(f"/manager/menu/{item.id}/toggle", data={"csrf_token": tok}).status_code == 302
    db.session.refresh(item)
    assert item.available is False
    assert c.post("/logout", data={"csrf_token": tok}).status_code == 302
    assert c.get("/manager").status_code == 302


def test_every_staff_form_has_csrf_field(staff):
    for path in ("/manager/menu", "/manager", "/kitchen", "/bar", "/manager/qr"):
        html = staff.get(path).get_data(as_text=True)
        forms = re.findall(r"<form.*?</form>", html, flags=re.S)
        assert forms and all('name="csrf_token"' in f for f in forms), path
        assert 'name="csrf-token"' in html


def test_guest_order_needs_no_csrf_but_needs_table_token(client):
    m = ids()
    assert order(client, [{"id": m["Beer"], "qty": 1}]).status_code == 201  # no CSRF token sent
    assert order(client, [{"id": m["Beer"], "qty": 1}], token="wrong").status_code == 404


# ------------------------------------------------------------ 2. PIN rate limit

def test_pin_lockout_after_five_failures(app, client, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(app_module, "_now", lambda: now[0])
    for _ in range(4):
        r = login(client, "0000")
        assert r.status_code == 200 and b"Wrong PIN" in r.data
    r = login(client, "0000")  # 5th failure
    assert r.status_code == 429 and b"Too many failed PIN attempts" in r.data and b"10 minutes" in r.data
    r = login(client)  # correct PIN is refused while locked
    assert r.status_code == 429 and client.get("/manager").status_code == 302
    now[0] += 9 * 60
    r = login(client)
    assert r.status_code == 429 and b"1 minute." in r.data
    now[0] += 61
    assert login(client).status_code == 302 and client.get("/manager").status_code == 200


def test_pin_failures_expire_and_reset(app, client, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(app_module, "_now", lambda: now[0])
    for _ in range(4):
        login(client, "0000")
    now[0] += 601  # the four failures fall out of the 10-minute window
    for _ in range(4):
        assert login(client, "0000").status_code == 200
    assert login(client).status_code == 302  # success clears the counter
    other = app.test_client()
    for _ in range(4):
        assert login(other, "0000").status_code == 200


def test_pin_lockout_is_per_ip(app, monkeypatch):
    a, b = app.test_client(), app.test_client()
    a.environ_base["REMOTE_ADDR"] = "10.0.0.1"
    b.environ_base["REMOTE_ADDR"] = "10.0.0.2"
    for _ in range(5):
        login(a, "0000")
    assert login(a).status_code == 429
    assert login(b).status_code == 302


# ------------------------------------------------------------ 3. table sessions

def test_guest_page_carries_session_id(client):
    t = Table.query.filter_by(token="tok1").one()
    assert t.session_id and len(t.session_id) >= 16
    assert f'data-session="{t.session_id}"' in client.get("/t/tok1").get_data(as_text=True)


def test_closing_table_rotates_session_and_rejects_stale_orders(client, staff):
    m = ids()
    t = Table.query.filter_by(token="tok1").one()
    old_sid, old_token = t.session_id, t.token
    assert order(client, [{"id": m["Beer"], "qty": 1}]).status_code == 201
    assert staff.post(f"/api/tables/{t.id}/close").status_code == 200
    db.session.refresh(t)
    assert t.session_id != old_sid and t.token == old_token  # printed QR keeps working

    r = client.post("/t/tok1/order", json={"session_id": old_sid, "items": [{"id": m["Beer"], "qty": 1}]})
    assert r.status_code == 409
    assert r.get_json() == {"error": "This table was closed. Please rescan the QR code.", "code": "stale_session"}
    assert client.get(f"/t/tok1/orders?sid={old_sid}").status_code == 409
    assert Order.query.filter(Order.status != "closed").count() == 0
    db.session.refresh(t)
    assert t.status == "free"

    # rescanning the same QR code gives the new session and ordering works again
    html = client.get("/t/tok1").get_data(as_text=True)
    new_sid = re.search(r'data-session="([^"]+)"', html).group(1)
    assert new_sid == t.session_id
    r = client.post("/t/tok1/order", json={"session_id": new_sid, "items": [{"id": m["Beer"], "qty": 1}]})
    assert r.status_code == 201
    assert client.get(f"/t/tok1/orders?sid={new_sid}").status_code == 200


@pytest.mark.parametrize("sid", [None, "", "wrong", 5, ["a"]])
def test_missing_or_bad_session_id_rejected(client, sid):
    m = ids()
    payload = {"items": [{"id": m["Beer"], "qty": 1}]}
    if sid is not None:
        payload["session_id"] = sid
    assert client.post("/t/tok1/order", json=payload).status_code == 409
    assert Order.query.count() == 0


def test_session_of_another_table_rejected(client):
    m = ids()
    other = Table.query.filter_by(token="tok2").one().session_id
    r = client.post("/t/tok1/order", json={"session_id": other, "items": [{"id": m["Beer"], "qty": 1}]})
    assert r.status_code == 409


def test_existing_database_gets_session_id_column(tmp_path):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE tables (id INTEGER PRIMARY KEY, number INTEGER NOT NULL UNIQUE, "
                "token VARCHAR(64) NOT NULL UNIQUE, status VARCHAR(16) NOT NULL)")
    con.executemany("INSERT INTO tables (number, token, status) VALUES (?, ?, 'free')", [(1, "a"), (2, "b")])
    con.commit(); con.close()
    old = create_app({"TESTING": True, "STAFF_PIN": PIN, "SECRET_KEY": "t",
                      "SQLALCHEMY_DATABASE_URI": "sqlite:///" + path.as_posix()})
    with old.app_context():
        sids = [t.session_id for t in Table.query.all()]
        assert len(sids) == 2 and all(sids) and sids[0] != sids[1]
        assert old.test_client().get("/t/a").status_code == 200
        db.session.remove()
        db.engine.dispose()


# ------------------------------------------------------------ 4. category admin

def test_category_create_rename_change_station(client, staff):
    assert staff.post("/manager/categories/add", data={"name": " Snacks ", "station": "kitchen"}).status_code == 302
    cat = Category.query.filter_by(name="Snacks").one()
    assert cat.station == "kitchen"
    staff.post(f"/manager/categories/{cat.id}/edit", data={"name": "Bar Snacks", "station": "bar"})
    db.session.refresh(cat)
    assert (cat.name, cat.station) == ("Bar Snacks", "bar")
    html = staff.get("/manager/menu").get_data(as_text=True)
    assert "Bar Snacks" in html and "Categories" in html

    # new items in the category route to its new station
    staff.post("/manager/menu/add", data={"name": "Olives", "description": "", "price": "4", "category_id": cat.id})
    olives = MenuItem.query.filter_by(name="Olives").one()
    assert order(client, [{"id": olives.id, "qty": 1}]).status_code == 201
    assert OrderItem.query.one().station == "bar"


@pytest.mark.parametrize("data", [
    {"name": "", "station": "bar"}, {"name": "   ", "station": "bar"},
    {"name": "X", "station": "grill"}, {"name": "X"},
])
def test_category_validation(staff, data):
    staff.post("/manager/categories/add", data=data)
    assert Category.query.count() == 2
    cat = Category.query.filter_by(name="Food").one()
    staff.post(f"/manager/categories/{cat.id}/edit", data=data)
    db.session.refresh(cat)
    assert (cat.name, cat.station) == ("Food", "kitchen")


def test_category_delete_blocked_while_it_has_items(staff):
    food = Category.query.filter_by(name="Food").one()
    r = staff.post(f"/manager/categories/{food.id}/delete", follow_redirects=True)
    assert b"Cannot delete Food" in r.data
    assert db.session.get(Category, food.id) is not None and MenuItem.query.count() == 3

    staff.post("/manager/categories/add", data={"name": "Empty", "station": "bar"})
    empty = Category.query.filter_by(name="Empty").one()
    r = staff.post(f"/manager/categories/{empty.id}/delete", follow_redirects=True)
    assert b"Deleted category Empty" in r.data
    assert Category.query.filter_by(name="Empty").count() == 0
    assert staff.post("/manager/categories/9999/delete").status_code == 404


def test_category_admin_requires_staff(client):
    tok = csrf_token(client)
    r = client.post("/manager/categories/add", data={"name": "X", "station": "bar", "csrf_token": tok})
    assert r.status_code == 302 and "/login" in r.headers["Location"]
    assert Category.query.count() == 2


# ------------------------------------------------------------ 5. no default PIN

@pytest.mark.parametrize("pin", [None, "", "   "])
def test_create_app_refuses_without_pin(monkeypatch, pin):
    monkeypatch.delenv("STAFF_PIN", raising=False)
    cfg = {"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite://", "SECRET_KEY": "t"}
    if pin is not None:
        cfg["STAFF_PIN"] = pin
    with pytest.raises(RuntimeError, match="STAFF_PIN is not set"):
        create_app(cfg)


def test_server_refuses_to_start_without_pin(tmp_path):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = {k: v for k, v in os.environ.items() if k != "STAFF_PIN"}
    env.update(DATABASE_URL="sqlite:///" + (tmp_path / "x.db").as_posix(), SECRET_KEY="t", PORT="5999")
    r = subprocess.run([sys.executable, os.path.join(root, "app.py")], env=env, cwd=root,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode != 0
    assert "STAFF_PIN is not set" in r.stderr and "Traceback" not in r.stderr


def test_no_pin_means_no_login_even_if_app_is_built_for_seeding(monkeypatch):
    monkeypatch.delenv("STAFF_PIN", raising=False)
    a = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite://", "SECRET_KEY": "t"},
                   require_pin=False)
    c = a.test_client()
    for pin in ("", "1234"):
        assert login(c, pin).status_code in (200, 429)
    assert c.get("/manager").status_code == 302
    with a.app_context():
        db.session.remove()
        db.drop_all()
