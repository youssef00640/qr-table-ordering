import secrets
from datetime import datetime, timezone

from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()

ITEM_FLOW = ["new", "preparing", "ready", "served"]
STATIONS = ("kitchen", "bar")
TABLE_STATUSES = ("free", "ordering", "waiting", "ready")


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def new_token():
    return secrets.token_urlsafe(16)


class Table(db.Model):
    __tablename__ = "tables"
    id = db.Column(db.Integer, primary_key=True)
    number = db.Column(db.Integer, unique=True, nullable=False)
    token = db.Column(db.String(64), unique=True, nullable=False, default=new_token)
    status = db.Column(db.String(16), nullable=False, default="free")
    # Rotates every time the table is closed; the QR token never changes.
    session_id = db.Column(db.String(64), nullable=False, default=new_token)
    orders = db.relationship("Order", back_populates="table", order_by="Order.created_at")

    @property
    def open_orders(self):
        return [o for o in self.orders if o.status != "closed"]

    @property
    def open_total(self):
        return sum(o.total for o in self.open_orders)

    def refresh_status(self):
        """Derive the floor status from the items of the open orders."""
        statuses = [i.status for o in self.open_orders for i in o.items]
        if not statuses:
            return  # stays free / ordering
        if "ready" in statuses:
            self.status = "ready"
        elif "new" in statuses or "preparing" in statuses:
            self.status = "waiting"
        else:  # everything served, guests still seated
            self.status = "ordering"


class Category(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), nullable=False)
    station = db.Column(db.String(16), nullable=False)  # "kitchen" | "bar"
    items = db.relationship("MenuItem", back_populates="category", order_by="MenuItem.id")


class MenuItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    category_id = db.Column(db.Integer, db.ForeignKey("category.id"), nullable=False)
    name = db.Column(db.String(120), nullable=False)
    description = db.Column(db.String(300), nullable=False, default="")
    price = db.Column(db.Integer, nullable=False)  # in cents
    available = db.Column(db.Boolean, nullable=False, default=True)
    category = db.relationship("Category", back_populates="items")


class Order(db.Model):
    __tablename__ = "orders"
    id = db.Column(db.Integer, primary_key=True)
    table_id = db.Column(db.Integer, db.ForeignKey("tables.id"), nullable=False)
    status = db.Column(db.String(16), nullable=False, default="new")  # item flow + "closed"
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    note = db.Column(db.String(200))
    table = db.relationship("Table", back_populates="orders")
    items = db.relationship("OrderItem", back_populates="order", cascade="all, delete-orphan")

    @property
    def total(self):
        return sum(i.qty * i.unit_price for i in self.items)

    def refresh_status(self):
        """Order status is derived from its items (unless the order is closed)."""
        if self.status == "closed" or not self.items:
            return
        idx = [ITEM_FLOW.index(i.status) for i in self.items]
        if min(idx) == 0 and max(idx) > 0:
            self.status = "preparing"
        else:
            self.status = ITEM_FLOW[min(idx)]


class OrderItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("orders.id"), nullable=False)
    menu_item_id = db.Column(db.Integer, db.ForeignKey("menu_item.id"), nullable=False)
    qty = db.Column(db.Integer, nullable=False)
    note = db.Column(db.String(200))
    station = db.Column(db.String(16), nullable=False)
    status = db.Column(db.String(16), nullable=False, default="new")
    unit_price = db.Column(db.Integer, nullable=False)  # cents, copied from the DB at order time
    order = db.relationship("Order", back_populates="items")
    menu_item = db.relationship("MenuItem")
