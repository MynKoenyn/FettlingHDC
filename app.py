from flask import Flask, render_template, request, redirect, url_for, session, jsonify
import psycopg2
import os
from datetime import date
import json

app = Flask(__name__, template_folder='templates', static_folder='static')
app.secret_key = os.environ.get('SESSION_SECRET', 'default_secret_key')

def get_db_connection():
    conn = psycopg2.connect(os.environ['DATABASE_URL'])
    return conn

@app.route('/')
def index():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    return redirect(url_for('dashboard'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('SELECT id, password FROM users WHERE username = %s', (username,))
        user = cur.fetchone()
        cur.close()
        conn.close()
        
        if user and user[1] == password: # In production use hashing!
            session['user_id'] = user[0]
            session['username'] = username
            return redirect(url_for('dashboard'))
        else:
            return render_template('login.html', error="Invalid credentials")
            
    return render_template('login.html')

@app.route('/dashboard')
def dashboard():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('''
        SELECT f.entry_date, SUM(f.quantity) as total_qty 
        FROM fettling_entries f 
        WHERE f.user_id = %s 
        GROUP BY f.entry_date 
        ORDER BY f.entry_date DESC
    ''', (session['user_id'],))
    recent_activity = cur.fetchall()
    cur.close()
    conn.close()
    
    return render_template('dashboard.html', recent_activity=recent_activity)

@app.route('/suppliers', methods=['GET', 'POST'])
def suppliers():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    conn = get_db_connection()
    cur = conn.cursor()
    
    if request.method == 'POST':
        name = request.form.get('name')
        if name:
            cur.execute('INSERT INTO suppliers (name) VALUES (%s)', (name,))
            conn.commit()
            return redirect(url_for('suppliers'))
            
    cur.execute('SELECT id, name FROM suppliers ORDER BY name')
    all_suppliers = cur.fetchall()
    cur.close()
    conn.close()
    return render_template('suppliers.html', suppliers=all_suppliers)

@app.route('/products', methods=['GET', 'POST'])
def products_crud():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    conn = get_db_connection()
    cur = conn.cursor()
    
    if request.method == 'POST':
        name = request.form.get('name')
        supplier_id = request.form.get('supplier_id')
        if name and supplier_id:
            cur.execute('INSERT INTO products (name, supplier_id) VALUES (%s, %s)', (name, supplier_id))
            conn.commit()
            return redirect(url_for('products_crud'))
            
    cur.execute('''
        SELECT p.id, p.name, s.name 
        FROM products p 
        JOIN suppliers s ON p.supplier_id = s.id 
        ORDER BY s.name, p.name
    ''')
    all_products = cur.fetchall()
    
    cur.execute('SELECT id, name FROM suppliers ORDER BY name')
    all_suppliers = cur.fetchall()
    
    cur.close()
    conn.close()
    return render_template('products.html', products=all_products, suppliers=all_suppliers)

@app.route('/entry', methods=['GET', 'POST'])
def entry():
    if 'user_id' not in session:
        return redirect(url_for('login'))
        
    conn = get_db_connection()
    cur = conn.cursor()
    
    if request.method == 'POST':
        entry_date = request.form['entry_date']
        user_id = session['user_id']
        
        # Process the 50 lines
        # Assuming form data like: product_id_1, quantity_1, product_id_2, quantity_2...
        
        # We need to parse the form data carefully
        entries_to_insert = []
        
        for key, value in request.form.items():
            if key.startswith('quantity_') and value and int(value) > 0:
                line_no = key.split('_')[1]
                product_id_key = f'product_id_{line_no}'
                product_id = request.form.get(product_id_key)
                
                if product_id:
                    entries_to_insert.append((entry_date, product_id, int(value), user_id))
        
        if entries_to_insert:
            cur.executemany('''
                INSERT INTO fettling_entries (entry_date, product_id, quantity, user_id)
                VALUES (%s, %s, %s, %s)
            ''', entries_to_insert)
            conn.commit()
            
        cur.close()
        conn.close()
        return redirect(url_for('dashboard'))

    # GET request - Load suppliers and products for the form
    cur.execute('SELECT id, name FROM suppliers ORDER BY name')
    suppliers = cur.fetchall()
    
    # Get all products to build the JS mapping
    cur.execute('SELECT id, name, supplier_id FROM products ORDER BY name')
    products = cur.fetchall()
    
    products_by_supplier = {}
    for p in products:
        p_id, p_name, s_id = p
        if s_id not in products_by_supplier:
            products_by_supplier[s_id] = []
        products_by_supplier[s_id].append({'id': p_id, 'name': p_name})
        
    cur.close()
    conn.close()
    
    return render_template('entry.html', 
                         suppliers=suppliers, 
                         products_by_supplier=json.dumps(products_by_supplier),
                         today=date.today())

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/report')
def report():
    if 'user_id' not in session:
        return redirect(url_for('login'))
        
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')
    supplier_id = request.args.get('supplier_id')
    product_id = request.args.get('product_id')
    
    conn = get_db_connection()
    cur = conn.cursor()
    
    # Load filters data
    cur.execute('SELECT id, name FROM suppliers ORDER BY name')
    suppliers = cur.fetchall()
    
    cur.execute('SELECT id, name FROM products ORDER BY name')
    products = cur.fetchall()
    
    # Build query
    query = '''
        SELECT f.entry_date, s.name as supplier_name, p.name as product_name, f.quantity
        FROM fettling_entries f
        JOIN products p ON f.product_id = p.id
        JOIN suppliers s ON p.supplier_id = s.id
        WHERE 1=1
    '''
    params = []
    
    if start_date:
        query += ' AND f.entry_date >= %s'
        params.append(start_date)
    if end_date:
        query += ' AND f.entry_date <= %s'
        params.append(end_date)
    if supplier_id:
        query += ' AND s.id = %s'
        params.append(supplier_id)
    if product_id:
        query += ' AND p.id = %s'
        params.append(product_id)
        
    query += ' ORDER BY f.entry_date DESC, s.name, p.name'
    
    cur.execute(query, tuple(params))
    report_data = cur.fetchall()
    
    cur.close()
    conn.close()
    
    return render_template('report.html', 
                         report_data=report_data, 
                         suppliers=suppliers, 
                         products=products,
                         filters={
                             'start_date': start_date,
                             'end_date': end_date,
                             'supplier_id': supplier_id,
                             'product_id': product_id
                         })

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
