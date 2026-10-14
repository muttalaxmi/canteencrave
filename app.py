from datetime import datetime
from math import ceil
import os

from flask import Flask, jsonify, render_template, request
from flask_sqlalchemy import SQLAlchemy

app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///canteencrave.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

STATUSES = [
    "Placed",
    "Preparing",
    "Ready for Pickup",
    "Collected",
]


class Food(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    category = db.Column(db.String(50), nullable=False)
    price = db.Column(db.Float, nullable=False)
    calories = db.Column(db.Integer, nullable=False)
    prep_time = db.Column(db.Integer, nullable=False)
    emoji = db.Column(db.String(10), default="🍽️")


class Order(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    status = db.Column(db.String(40), default="Placed")
    total = db.Column(db.Float, nullable=False)
    calories = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    items = db.relationship(
        "OrderItem", backref="order", cascade="all, delete-orphan"
    )


class OrderItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(
        db.Integer, db.ForeignKey("order.id"), nullable=False
    )
    food_id = db.Column(db.Integer, nullable=False)
    name = db.Column(db.String(100), nullable=False)
    quantity = db.Column(db.Integer, nullable=False)
    price = db.Column(db.Float, nullable=False)
    calories = db.Column(db.Integer, nullable=False)


def order_json(order):
    # Simple estimate, not a live kitchen prediction.
    prep = max(
        (
            Food.query.get(item.food_id).prep_time
            if Food.query.get(item.food_id)
            else 5
        )
        for item in order.items
    ) if order.items else 5

    elapsed = max(
        0, int((datetime.utcnow() - order.created_at).total_seconds() / 60)
    )

    # Once an order is ready, show zero remaining prep time.
    remaining = (
        0 if order.status in ("Ready for Pickup", "Collected")
        else max(1, prep - elapsed)
    )

    return {
        "id": order.id,
        "status": order.status,
        "total": round(order.total, 2),
        "calories": order.calories,
        "estimated_minutes": remaining,
        "created_at": order.created_at.isoformat() + "Z",
        "items": [
            {
                "name": item.name,
                "quantity": item.quantity,
                "price": item.price,
                "calories": item.calories,
            }
            for item in order.items
        ],
    }


@app.get("/")
def home():
    return render_template("index.html")


@app.get("/api/menu")
def menu():
    foods = Food.query.order_by(Food.id).all()
    return jsonify([
        {
            "id": f.id,
            "name": f.name,
            "category": f.category,
            "price": f.price,
            "calories": f.calories,
            "prep_time": f.prep_time,
            "emoji": f.emoji,
        }
        for f in foods
    ])


@app.post("/api/orders")
def create_order():
    data = request.get_json(silent=True) or {}
    items = data.get("items", [])

    if not isinstance(items, list) or not items:
        return jsonify({"error": "Please add food to your cart."}), 400

    # Validate all items before creating the order.
    quantities = {}
    for item in items:
        try:
            food_id = int(item["id"])
            quantity = int(item["quantity"])
        except (KeyError, ValueError, TypeError):
            return jsonify({"error": "Invalid item or quantity."}), 400

        if quantity < 1 or quantity > 20:
            return jsonify({"error": "Quantity must be between 1 and 20."}), 400

        quantities[food_id] = quantities.get(food_id, 0) + quantity

    foods = {
        food.id: food
        for food in Food.query.filter(
            Food.id.in_(quantities.keys())
        ).all()
    }

    if len(foods) != len(quantities):
        return jsonify({"error": "A menu item no longer exists."}), 400

    total = sum(
        foods[fid].price * qty for fid, qty in quantities.items()
    )
    calories = sum(
        foods[fid].calories * qty for fid, qty in quantities.items()
    )

    order = Order(total=total, calories=calories)
    for fid, qty in quantities.items():
        food = foods[fid]
        order.items.append(OrderItem(
            food_id=food.id,
            name=food.name,
            quantity=qty,
            price=food.price,
            calories=food.calories * qty,
        ))

    db.session.add(order)
    db.session.commit()

    return jsonify(order_json(order)), 201


@app.get("/api/orders/<int:order_id>")
def get_order(order_id):
    order = db.session.get(Order, order_id)
    if not order:
        return jsonify({"error": "Order not found."}), 404
    return jsonify(order_json(order))


@app.get("/api/calories/today")
def daily_calories():
    # Demo total for all orders today. Add student accounts and
    # ownership checks before using this in a multi-user deployment.
    today = datetime.utcnow().date()
    orders = Order.query.filter(
        Order.created_at >= datetime.combine(
            today, datetime.min.time()
        ),
        Order.created_at < datetime.combine(
            today, datetime.max.time()
        ),
    ).all()

    return jsonify({
        "calories": sum(o.calories for o in orders),
        "orders": len(orders),
    })


@app.post("/api/chat")
def chat():
    data = request.get_json(silent=True) or {}
    message = str(data.get("message", "")).strip().lower()

    if not message or len(message) > 500:
        return jsonify({"error": "Enter a question under 500 characters."}), 400

    foods = Food.query.all()
    if not foods:
        return jsonify({"reply": "The menu is not available yet."})

    if any(word in message for word in ("break", "time", "quick", "hurry")):
        # Recommend items that are likely to fit a short break.
        choices = sorted(foods, key=lambda f: f.prep_time)[:3]
        reply = "For a short break, consider: " + ", ".join(
            f"{f.name} ({f.prep_time} min prep)"
            for f in choices
        ) + ". Add pickup and eating time before choosing."

    elif "calorie" in message or "healthy" in message:
        choices = sorted(foods, key=lambda f: f.calories)[:3]
        reply = "Lowest-calorie menu options per serving: " + ", ".join(
            f"{f.name} ({f.calories} kcal)"
            for f in choices
        ) + ". Calories are estimates."

    elif "budget" in message or "cheap" in message or "price" in message:
        choices = sorted(foods, key=lambda f: f.price)[:3]
        reply = "Budget-friendly choices: " + ", ".join(
            f"{f.name} (₹{f.price:.0f})"
            for f in choices
        )

    else:
        reply = "Here are some menu options: " + ", ".join(
            f"{f.name} (₹{f.price:.0f}, {f.calories} kcal)"
            for f in foods[:5]
        ) + ". Ask me about calories, budget, or a short break!"

    return jsonify({"reply": reply})


# Demo staff endpoint. Protect this with staff authentication
# and authorization before deployment.
@app.post("/api/orders/<int:order_id>/status")
def update_status(order_id):
    data = request.get_json(silent=True) or {}
    status = data.get("status")

    if status not in STATUSES:
        return jsonify({"error": "Invalid order status."}), 400

    order = db.session.get(Order, order_id)
    if not order:
        return jsonify({"error": "Order not found."}), 404

    order.status = status
    db.session.commit()
    return jsonify(order_json(order))


def seed_menu():
    if Food.query.count():
        return

    samples = [
        ("Veg Sandwich", "Snacks", 50, 280, 5, "🥪"),
        ("Masala Dosa", "Breakfast", 70, 350, 10, "🥞"),
        ("Veg Fried Rice", "Meals", 90, 420, 12, "🍚"),
        ("Idli (2 pcs)", "Breakfast", 35, 160, 5, "🍘"),
        ("Fruit Bowl", "Healthy", 45, 120, 3, "🍎"),
        ("Veg Burger", "Snacks", 65, 390, 8, "🍔"),
        ("Lemon Juice", "Drinks", 25, 90, 2, "🍋"),
        ("Tea", "Drinks", 15, 40, 2, "☕"),
    ]

    for name, category, price, calories, prep, emoji in samples:
        db.session.add(Food(
            name=name,
            category=category,
            price=price,
            calories=calories,
            prep_time=prep,
            emoji=emoji,
        ))
    db.session.commit()


with app.app_context():
    db.create_all()
    seed_menu()


if __name__ == "__main__":
    app.run(debug=True)