from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
import sqlite3
from datetime import datetime, date
import os
from mpesa import MPesaAPI

app = Flask(__name__)
app.secret_key = 'your-secret-key-change-this'

DATABASE = 'rentsync.db'

def init_db():
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    
    # Tenants table
    c.execute('''CREATE TABLE IF NOT EXISTS tenants (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        house_number TEXT NOT NULL,
        phone TEXT NOT NULL,
        email TEXT,
        monthly_rent REAL NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    
    # Payments table
    c.execute('''CREATE TABLE IF NOT EXISTS payments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tenant_id INTEGER NOT NULL,
        amount REAL NOT NULL,
        payment_date DATE NOT NULL,
        payment_mode TEXT DEFAULT 'Cash',
        status TEXT DEFAULT 'Paid',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (tenant_id) REFERENCES tenants (id)
    )''')
    
    # Admin table
    c.execute('''CREATE TABLE IF NOT EXISTS admins (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    
    # M-Pesa transactions table
    c.execute('''CREATE TABLE IF NOT EXISTS mpesa_transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tenant_id INTEGER NOT NULL,
        checkout_request_id TEXT,
        merchant_request_id TEXT,
        amount REAL NOT NULL,
        phone_number TEXT NOT NULL,
        status TEXT DEFAULT 'PENDING',
        mpesa_receipt_number TEXT,
        transaction_date TIMESTAMP,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (tenant_id) REFERENCES tenants (id)
    )''')
    
    # Create default admin if none exists
    c.execute('SELECT COUNT(*) FROM admins')
    if c.fetchone()[0] == 0:
        admin_hash = generate_password_hash('admin123')
        c.execute('INSERT INTO admins (username, password_hash) VALUES (?, ?)', 
                 ('admin', admin_hash))
    
    conn.commit()
    conn.close()

def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn

def calculate_balance(tenant_id):
    conn = get_db()
    
    # Get tenant's monthly rent
    tenant = conn.execute('SELECT monthly_rent, created_at FROM tenants WHERE id = ?', 
                         (tenant_id,)).fetchone()
    if not tenant:
        return 0
    
    # Calculate months since tenant creation
    created_date = datetime.strptime(tenant['created_at'], '%Y-%m-%d %H:%M:%S').date()
    today = date.today()
    months_diff = (today.year - created_date.year) * 12 + (today.month - created_date.month)
    if today.day >= created_date.day:
        months_diff += 1
    
    total_expected = tenant['monthly_rent'] * max(1, months_diff)
    
    # Get total paid
    total_paid = conn.execute(
        'SELECT COALESCE(SUM(amount), 0) FROM payments WHERE tenant_id = ?', 
        (tenant_id,)
    ).fetchone()[0]
    
    conn.close()
    return total_expected - total_paid

# Routes
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/tenant/<int:tenant_id>')
def tenant_dashboard(tenant_id):
    conn = get_db()
    tenant = conn.execute('SELECT * FROM tenants WHERE id = ?', (tenant_id,)).fetchone()
    if not tenant:
        flash('Tenant not found')
        return redirect(url_for('index'))
    
    payments = conn.execute(
        'SELECT * FROM payments WHERE tenant_id = ? ORDER BY payment_date DESC', 
        (tenant_id,)
    ).fetchall()
    
    balance = calculate_balance(tenant_id)
    conn.close()
    
    return render_template('tenant_dashboard.html', tenant=tenant, payments=payments, balance=balance)

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        
        conn = get_db()
        admin = conn.execute('SELECT * FROM admins WHERE username = ?', (username,)).fetchone()
        conn.close()
        
        if admin and check_password_hash(admin['password_hash'], password):
            session['admin_id'] = admin['id']
            return redirect(url_for('admin_dashboard'))
        flash('Invalid credentials')
    
    return render_template('admin_login.html')

@app.route('/admin/logout')
def admin_logout():
    session.pop('admin_id', None)
    return redirect(url_for('index'))

@app.route('/admin')
def admin_dashboard():
    if 'admin_id' not in session:
        return redirect(url_for('admin_login'))
    
    conn = get_db()
    tenants = conn.execute('SELECT * FROM tenants ORDER BY name').fetchall()
    
    # Calculate balances for all tenants
    tenant_data = []
    for tenant in tenants:
        balance = calculate_balance(tenant['id'])
        tenant_data.append({
            'tenant': tenant,
            'balance': balance,
            'status': 'Overdue' if balance > 0 else 'Paid' if balance == 0 else 'Credit'
        })
    
    conn.close()
    return render_template('admin_dashboard.html', tenant_data=tenant_data)

@app.route('/admin/tenant/add', methods=['GET', 'POST'])
def add_tenant():
    if 'admin_id' not in session:
        return redirect(url_for('admin_login'))
    
    if request.method == 'POST':
        conn = get_db()
        conn.execute(
            'INSERT INTO tenants (name, house_number, phone, email, monthly_rent) VALUES (?, ?, ?, ?, ?)',
            (request.form['name'], request.form['house_number'], 
             request.form['phone'], request.form['email'], 
             float(request.form['monthly_rent']))
        )
        conn.commit()
        conn.close()
        flash('Tenant added successfully')
        return redirect(url_for('admin_dashboard'))
    
    return render_template('add_tenant.html')

@app.route('/admin/payment/add/<int:tenant_id>', methods=['GET', 'POST'])
def add_payment(tenant_id):
    if 'admin_id' not in session:
        return redirect(url_for('admin_login'))
    
    if request.method == 'POST':
        conn = get_db()
        conn.execute(
            'INSERT INTO payments (tenant_id, amount, payment_date, payment_mode) VALUES (?, ?, ?, ?)',
            (tenant_id, float(request.form['amount']), 
             request.form['payment_date'], request.form['payment_mode'])
        )
        conn.commit()
        conn.close()
        flash('Payment recorded successfully')
        return redirect(url_for('admin_dashboard'))
    
    conn = get_db()
    tenant = conn.execute('SELECT * FROM tenants WHERE id = ?', (tenant_id,)).fetchone()
    conn.close()
    
    return render_template('add_payment.html', tenant=tenant)

@app.route('/mpesa/pay/<int:tenant_id>', methods=['POST'])
def mpesa_payment(tenant_id):
    if 'admin_id' not in session:
        return jsonify({'success': False, 'message': 'Unauthorized'})
    
    conn = get_db()
    tenant = conn.execute('SELECT * FROM tenants WHERE id = ?', (tenant_id,)).fetchone()
    if not tenant:
        return jsonify({'success': False, 'message': 'Tenant not found'})
    
    amount = float(request.json['amount'])
    phone = request.json['phone']
    
    # Initialize M-Pesa API
    mpesa = MPesaAPI()
    
    # Format phone number (remove + and ensure it starts with 254)
    if phone.startswith('+'):
        phone = phone[1:]
    if phone.startswith('0'):
        phone = '254' + phone[1:]
    
    # Initiate STK Push
    result = mpesa.stk_push(
        phone_number=phone,
        amount=amount,
        account_reference=f'RENT-{tenant["house_number"]}',
        transaction_desc=f'Rent payment for {tenant["name"]}'
    )
    
    if result.get('ResponseCode') == '0':
        # Save transaction to database
        conn.execute(
            'INSERT INTO mpesa_transactions (tenant_id, checkout_request_id, merchant_request_id, amount, phone_number) VALUES (?, ?, ?, ?, ?)',
            (tenant_id, result['CheckoutRequestID'], result['MerchantRequestID'], amount, phone)
        )
        conn.commit()
        conn.close()
        
        return jsonify({
            'success': True,
            'message': 'Payment request sent to phone',
            'checkout_request_id': result['CheckoutRequestID']
        })
    else:
        conn.close()
        return jsonify({
            'success': False,
            'message': result.get('errorMessage', 'Payment failed')
        })

@app.route('/mpesa/callback', methods=['POST'])
def mpesa_callback():
    callback_data = request.json
    
    if callback_data['Body']['stkCallback']['ResultCode'] == 0:
        # Payment successful
        checkout_request_id = callback_data['Body']['stkCallback']['CheckoutRequestID']
        
        conn = get_db()
        
        # Update M-Pesa transaction
        conn.execute(
            'UPDATE mpesa_transactions SET status = ?, mpesa_receipt_number = ?, transaction_date = ? WHERE checkout_request_id = ?',
            ('COMPLETED', callback_data['Body']['stkCallback']['CallbackMetadata']['Item'][1]['Value'],
             datetime.now(), checkout_request_id)
        )
        
        # Get transaction details
        transaction = conn.execute(
            'SELECT * FROM mpesa_transactions WHERE checkout_request_id = ?',
            (checkout_request_id,)
        ).fetchone()
        
        if transaction:
            # Add to payments table
            conn.execute(
                'INSERT INTO payments (tenant_id, amount, payment_date, payment_mode, status) VALUES (?, ?, ?, ?, ?)',
                (transaction['tenant_id'], transaction['amount'], date.today(), 'M-Pesa', 'Paid')
            )
        
        conn.commit()
        conn.close()
    else:
        # Payment failed
        checkout_request_id = callback_data['Body']['stkCallback']['CheckoutRequestID']
        conn = get_db()
        conn.execute(
            'UPDATE mpesa_transactions SET status = ? WHERE checkout_request_id = ?',
            ('FAILED', checkout_request_id)
        )
        conn.commit()
        conn.close()
    
    return jsonify({'ResultCode': 0, 'ResultDesc': 'Success'})

@app.route('/mpesa/status/<checkout_request_id>')
def check_mpesa_status(checkout_request_id):
    if 'admin_id' not in session:
        return jsonify({'success': False, 'message': 'Unauthorized'})
    
    conn = get_db()
    transaction = conn.execute(
        'SELECT * FROM mpesa_transactions WHERE checkout_request_id = ?',
        (checkout_request_id,)
    ).fetchone()
    conn.close()
    
    if transaction:
        return jsonify({
            'success': True,
            'status': transaction['status'],
            'amount': transaction['amount']
        })
    
    return jsonify({'success': False, 'message': 'Transaction not found'})

if __name__ == '__main__':
    init_db()
    app.run(debug=True)