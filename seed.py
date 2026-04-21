import psycopg2
import os
from werkzeug.security import generate_password_hash # If I was using hashing, but I did plain text for simplicity as requested "Just HTML Python..." implying simple.
# I'll stick to plain text as per my app.py implementation for now to keep it simple and match the code I wrote.

def get_db_connection():
    conn = psycopg2.connect(os.environ['DATABASE_URL'])
    return conn

def seed():
    conn = get_db_connection()
    cur = conn.cursor()

    # Create User
    # Check if user exists
    cur.execute("SELECT * FROM users WHERE username = 'admin'")
    if not cur.fetchone():
        print("Creating admin user...")
        cur.execute("INSERT INTO users (username, password) VALUES (%s, %s)", ('admin', 'password'))

    # Create Customers
    customers = ['Customer A', 'Customer B', 'Customer C', 'Industrial Supplies Co']
    for s_name in customers:
        cur.execute("SELECT id FROM customers WHERE name = %s", (s_name,))
        if not cur.fetchone():
            print(f"Creating customer {s_name}...")
            cur.execute("INSERT INTO customers (name) VALUES (%s)", (s_name,))

    # Create Products linked to Customers
    # Map customers to IDs
    cur.execute("SELECT id, name FROM customers")
    customer_map = {name: id for id, name in cur.fetchall()}

    products_data = [
        ('Hammer', 'Customer A'),
        ('Wrench', 'Customer A'),
        ('Nails', 'Customer B'),
        ('Bolts', 'Customer B'),
        ('Screws', 'Customer C'),
        ('Drill Bit', 'Customer C'),
        ('Grinding Wheel', 'Industrial Supplies Co'),
        ('Sandpaper', 'Industrial Supplies Co')
    ]

    for p_name, s_name in products_data:
        if s_name in customer_map:
            s_id = customer_map[s_name]
            cur.execute("SELECT id FROM products WHERE name = %s AND customer_id = %s", (p_name, s_id))
            if not cur.fetchone():
                print(f"Creating product {p_name} for {s_name}...")
                cur.execute("INSERT INTO products (name, customer_id) VALUES (%s, %s)", (p_name, s_id))

    conn.commit()
    cur.close()
    conn.close()
    print("Database seeded successfully!")

if __name__ == '__main__':
    seed()
