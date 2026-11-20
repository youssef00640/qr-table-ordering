"""Seed the database: 12 tables and 20 menu items.

    python seed.py           # seeds only if the database is empty
    python seed.py --reset   # wipes everything first (table tokens / QR codes change!)
"""
import sys

from app import create_app
from models import Category, MenuItem, Table, db

MENU = [
    ("Starters", "kitchen", [
        ("Garlic Bread", "Toasted sourdough, garlic butter, parsley", 450),
        ("Crispy Calamari", "With lemon aioli", 890),
        ("Loaded Nachos", "Cheddar, jalapeños, salsa, sour cream", 950),
        ("Soup of the Day", "Ask your server, served with bread", 650),
    ]),
    ("Mains", "kitchen", [
        ("Classic Cheeseburger", "Beef patty, cheddar, pickles, fries", 1390),
        ("Margherita Pizza", "Tomato, mozzarella, basil", 1150),
        ("Fish & Chips", "Beer-battered cod, tartare sauce", 1490),
        ("Chicken Caesar Salad", "Romaine, parmesan, croutons", 1190),
        ("Veggie Curry", "Coconut curry, seasonal vegetables, rice", 1250),
    ]),
    ("Desserts", "kitchen", [
        ("Chocolate Brownie", "Warm, with vanilla ice cream", 690),
        ("Cheesecake", "Baked, with berry compote", 650),
    ]),
    ("Beer & Wine", "bar", [
        ("Draft Lager (0.5l)", "Crisp house lager", 550),
        ("IPA (0.33l)", "Hoppy and citrusy", 590),
        ("House Red (glass)", "Medium-bodied", 620),
        ("House White (glass)", "Dry and fresh", 620),
    ]),
    ("Cocktails", "bar", [
        ("Mojito", "Rum, mint, lime, soda", 950),
        ("Espresso Martini", "Vodka, coffee liqueur, espresso", 1050),
        ("Aperol Spritz", "Aperol, prosecco, soda", 890),
    ]),
    ("Soft Drinks", "bar", [
        ("Cola", "0.33l bottle", 350),
        ("Sparkling Water", "0.5l bottle", 300),
    ]),
]


def seed(reset=False):
    if reset:
        db.drop_all()
        db.create_all()
    if Table.query.first() or MenuItem.query.first():
        print("Database already has data. Use --reset to wipe and reseed.")
        return
    for n in range(1, 13):
        db.session.add(Table(number=n))
    for cat_name, station, items in MENU:
        cat = Category(name=cat_name, station=station)
        for name, desc, price in items:
            cat.items.append(MenuItem(name=name, description=desc, price=price))
        db.session.add(cat)
    db.session.commit()
    print(f"Seeded {Table.query.count()} tables and {MenuItem.query.count()} menu items.")


if __name__ == "__main__":
    app = create_app(require_pin=False)  # seeding does not serve staff pages
    with app.app_context():
        seed(reset="--reset" in sys.argv)
