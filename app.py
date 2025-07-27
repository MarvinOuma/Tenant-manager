from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, make_response
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import sqlite3
from datetime import datetime, date
import os
from mpesa import MPesaAPI
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter
from io import BytesIO

app = Flask(__name__)
app.secret_key = 'your-secret-key-change-this'
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024  # 5MB max file size
app.config['UPLOAD_FOLDER'] = 'static/profile_pictures'
app.config['PERMANENT_SESSION_LIFETIME'] = 86400  # 24 hours
app.config['SESSION_PERMANENT'] = True

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
        profile_picture TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    
    # Create profile pictures directory
    os.makedirs('static/profile_pictures', exist_ok=True)
    
    # Notifications table
    c.execute('''CREATE TABLE IF NOT EXISTS notifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tenant_id INTEGER NOT NULL,
        message TEXT NOT NULL,
        type TEXT DEFAULT 'info',
        is_read BOOLEAN DEFAULT FALSE,
        deleted_at TIMESTAMP NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (tenant_id) REFERENCES tenants (id)
    )''')
    
    # Payments table
    c.execute('''CREATE TABLE IF NOT EXISTS payments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tenant_id INTEGER NOT NULL,
        amount REAL NOT NULL,
        payment_date DATE NOT NULL,
        payment_mode TEXT DEFAULT 'Cash',
        status TEXT DEFAULT 'Paid',
        mpesa_code TEXT,
        receipt_file TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (tenant_id) REFERENCES tenants (id)
    )''')
    
    # Admin table
    c.execute('''CREATE TABLE IF NOT EXISTS admins (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        role TEXT DEFAULT 'landlord',
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
    
    # Help requests table
    c.execute('''CREATE TABLE IF NOT EXISTS help_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tenant_id INTEGER NOT NULL,
        category TEXT NOT NULL,
        subject TEXT NOT NULL,
        message TEXT NOT NULL,
        status TEXT DEFAULT 'Open',
        admin_response TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (tenant_id) REFERENCES tenants (id)
    )''')
    
    # Utility bills table
    c.execute('''CREATE TABLE IF NOT EXISTS utility_bills (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tenant_id INTEGER NOT NULL,
        bill_type TEXT NOT NULL,
        amount REAL NOT NULL,
        due_date DATE NOT NULL,
        status TEXT DEFAULT 'Pending',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (tenant_id) REFERENCES tenants (id)
    )''')
    
    # Create default accounts if none exist
    c.execute('SELECT COUNT(*) FROM admins')
    if c.fetchone()[0] == 0:
        landlord_hash = generate_password_hash('landlord123')
        caretaker_hash = generate_password_hash('caretaker123')
        c.execute('INSERT INTO admins (username, password_hash, role) VALUES (?, ?, ?)', 
                 ('landlord', landlord_hash, 'landlord'))
        c.execute('INSERT INTO admins (username, password_hash, role) VALUES (?, ?, ?)', 
                 ('caretaker', caretaker_hash, 'caretaker'))
    
    conn.commit()
    conn.close()

def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn

def get_next_rent_info(tenant_id):
    conn = get_db()
    tenant = conn.execute('SELECT created_at, monthly_rent FROM tenants WHERE id = ?', (tenant_id,)).fetchone()
    if not tenant:
        return None
    
    created_date = datetime.strptime(tenant['created_at'], '%Y-%m-%d %H:%M:%S').date()
    today = date.today()
    
    # Calculate next due date (same day next month)
    if today.day >= created_date.day:
        # Next month
        next_month = today.month + 1 if today.month < 12 else 1
        next_year = today.year if today.month < 12 else today.year + 1
        next_due = date(next_year, next_month, created_date.day)
    else:
        # This month
        next_due = date(today.year, today.month, created_date.day)
    
    days_until_due = (next_due - today).days
    
    # Calculate amount needed based on balance
    balance = calculate_balance(tenant_id)
    if balance > 0:
        # Outstanding balance - pay the full balance
        amount_needed = balance
    elif balance < 0:
        # Credit balance - pay monthly rent minus credit
        credit_amount = abs(balance)
        amount_needed = max(0, tenant['monthly_rent'] - credit_amount)
    else:
        # Zero balance - pay full monthly rent
        amount_needed = tenant['monthly_rent']
    
    conn.close()
    return {
        'next_due_date': next_due,
        'days_until_due': days_until_due,
        'amount_needed': amount_needed,
        'is_overdue': days_until_due < 0
    }

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
    
    # Add pending utility bills
    utility_bills = conn.execute(
        'SELECT COALESCE(SUM(amount), 0) FROM utility_bills WHERE tenant_id = ? AND status = "Pending"', 
        (tenant_id,)
    ).fetchone()[0]
    
    total_expected += utility_bills
    
    # Get total paid
    total_paid = conn.execute(
        'SELECT COALESCE(SUM(amount), 0) FROM payments WHERE tenant_id = ?', 
        (tenant_id,)
    ).fetchone()[0]
    
    conn.close()
    return total_expected - total_paid

def generate_notifications(tenant_id):
    """Generate notifications for a tenant based on their payment status"""
    conn = get_db()
    tenant = conn.execute('SELECT * FROM tenants WHERE id = ?', (tenant_id,)).fetchone()
    if not tenant:
        conn.close()
        return []
    
    balance = calculate_balance(tenant_id)
    notifications = []
    
    # Calculate days until next rent due (assuming monthly rent on same day)
    today = date.today()
    created_date = datetime.strptime(tenant['created_at'], '%Y-%m-%d %H:%M:%S').date()
    
    # Next due date
    if today.day >= created_date.day:
        next_month = today.month + 1 if today.month < 12 else 1
        next_year = today.year if today.month < 12 else today.year + 1
        next_due = date(next_year, next_month, created_date.day)
    else:
        next_due = date(today.year, today.month, created_date.day)
    
    days_until_due = (next_due - today).days
    
    # Generate notifications based on status
    if balance > 0:
        notifications.append({
            'message': f'You have an unpaid balance of KSh {balance:.2f}',
            'type': 'danger',
            'icon': 'fas fa-exclamation-triangle'
        })
    elif balance < 0:
        notifications.append({
            'message': f'You have a credit balance of KSh {abs(balance):.2f}',
            'type': 'success',
            'icon': 'fas fa-check-circle'
        })
    
    if days_until_due <= 5 and days_until_due > 0:
        notifications.append({
            'message': f'Rent due in {days_until_due} day{"s" if days_until_due != 1 else ""}',
            'type': 'warning',
            'icon': 'fas fa-calendar-alt'
        })
    elif days_until_due == 0:
        notifications.append({
            'message': 'Rent is due today!',
            'type': 'danger',
            'icon': 'fas fa-calendar-times'
        })
    
    # Check recent payments for thank you message
    recent_payment = conn.execute(
        'SELECT * FROM payments WHERE tenant_id = ? ORDER BY created_at DESC LIMIT 1',
        (tenant_id,)
    ).fetchone()
    
    if recent_payment:
        payment_date = datetime.strptime(recent_payment['created_at'], '%Y-%m-%d %H:%M:%S').date()
        if (today - payment_date).days <= 3:
            notifications.append({
                'message': f'Payment received - thank you! KSh {recent_payment["amount"]:.2f}',
                'type': 'success',
                'icon': 'fas fa-heart'
            })
    
    conn.close()
    return notifications

def save_notification(tenant_id, message, notification_type='info'):
    """Save notification to database for web display"""
    conn = get_db()
    conn.execute(
        'INSERT INTO notifications (tenant_id, message, type) VALUES (?, ?, ?)',
        (tenant_id, message, notification_type)
    )
    conn.commit()
    conn.close()
    return True

# Routes
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/login/<login_type>', methods=['GET', 'POST'])
def unified_login(login_type):
    if request.method == 'POST':
        form_login_type = request.form.get('login_type') or login_type
        
        if form_login_type == 'tenant':
            phone = request.form['phone']
            house_number = request.form['house_number']
            
            conn = get_db()
            tenant = conn.execute(
                'SELECT * FROM tenants WHERE phone = ? AND house_number = ?', 
                (phone, house_number)
            ).fetchone()
            conn.close()
            
            if tenant:
                session.permanent = True
                session['user_type'] = 'tenant'
                session['tenant_id'] = tenant['id']
                return redirect(url_for('tenant_dashboard'))
            else:
                flash('Invalid phone number or house number')
        
        elif form_login_type == 'admin':
            username = request.form['username']
            password = request.form['password']
            
            conn = get_db()
            admin = conn.execute('SELECT * FROM admins WHERE username = ?', (username,)).fetchone()
            conn.close()
            
            if admin and check_password_hash(admin['password_hash'], password):
                session.permanent = True
                session['user_type'] = 'admin'
                session['admin_id'] = admin['id']
                session['admin_role'] = admin['role']
                
                if admin['role'] == 'caretaker':
                    return redirect(url_for('caretaker_dashboard'))
                else:
                    return redirect(url_for('admin_dashboard'))
            else:
                flash('Invalid admin credentials')
    
    return render_template('unified_login.html', login_type=login_type)



@app.route('/tenant/dashboard')
def tenant_dashboard():
    if 'tenant_id' not in session:
        return redirect(url_for('unified_login'))
    
    tenant_id = session['tenant_id']
    conn = get_db()
    tenant = conn.execute('SELECT * FROM tenants WHERE id = ?', (tenant_id,)).fetchone()
    if not tenant:
        session.pop('tenant_id', None)
        flash('Tenant not found')
        return redirect(url_for('tenant_login'))
    
    payments = conn.execute(
        'SELECT * FROM payments WHERE tenant_id = ? ORDER BY payment_date DESC', 
        (tenant_id,)
    ).fetchall()
    
    balance = calculate_balance(tenant_id)
    next_rent_info = get_next_rent_info(tenant_id)
    
    # Get utility bills (both pending and paid)
    utility_bills = conn.execute(
        'SELECT * FROM utility_bills WHERE tenant_id = ? ORDER BY due_date ASC',
        (tenant_id,)
    ).fetchall()
    
    # Get both generated and saved notifications
    auto_notifications = generate_notifications(tenant_id)
    saved_notifications = conn.execute(
        'SELECT * FROM notifications WHERE tenant_id = ? AND is_read = FALSE AND deleted_at IS NULL ORDER BY created_at DESC',
        (tenant_id,)
    ).fetchall()
    
    # Convert saved notifications to display format
    web_notifications = []
    for notif in saved_notifications:
        web_notifications.append({
            'id': notif['id'],
            'message': notif['message'],
            'type': notif['type'],
            'icon': 'fas fa-bell',
            'time': notif['created_at'],
            'is_saved': True
        })
    
    # Mark auto notifications as not saved
    for notif in auto_notifications:
        notif['is_saved'] = False
    
    # Combine all notifications
    all_notifications = auto_notifications + web_notifications
    conn.close()
    
    return render_template('tenant_dashboard.html', tenant=tenant, payments=payments, balance=balance, notifications=all_notifications, utility_bills=utility_bills, next_rent_info=next_rent_info)

@app.route('/logout')
def unified_logout():
    session.clear()
    flash('You have been logged out successfully')
    return redirect(url_for('index'))









@app.route('/account', methods=['GET', 'POST'])
def account_management():
    if 'admin_id' not in session:
        return redirect(url_for('unified_login'))
    
    admin_id = session['admin_id']
    role = session.get('admin_role')
    
    if request.method == 'POST':
        current_password = request.form['current_password']
        new_password = request.form['new_password']
        confirm_password = request.form['confirm_password']
        
        if new_password != confirm_password:
            flash('New passwords do not match')
            return redirect(url_for('account_management'))
        
        conn = get_db()
        admin = conn.execute('SELECT * FROM admins WHERE id = ?', (admin_id,)).fetchone()
        
        if not admin or not check_password_hash(admin['password_hash'], current_password):
            flash('Current password is incorrect')
            conn.close()
            return redirect(url_for('account_management'))
        
        new_hash = generate_password_hash(new_password)
        conn.execute('UPDATE admins SET password_hash = ? WHERE id = ?', (new_hash, admin_id))
        conn.commit()
        conn.close()
        
        flash('Password updated successfully!')
        return redirect(url_for('account_management'))
    
    conn = get_db()
    admin = conn.execute('SELECT username, role, created_at FROM admins WHERE id = ?', (admin_id,)).fetchone()
    conn.close()
    
    return render_template('admin_account.html', admin=admin, role=role)

@app.route('/admin')
def admin_dashboard():
    if 'admin_id' not in session or session.get('admin_role') != 'landlord':
        return redirect(url_for('unified_login'))
    
    conn = get_db()
    tenants = conn.execute('SELECT * FROM tenants ORDER BY name').fetchall()
    
    # Calculate statistics for landlord overview
    total_tenants = len(tenants)
    total_rent = sum(tenant['monthly_rent'] for tenant in tenants)
    
    overdue_count = 0
    paid_count = 0
    credit_count = 0
    total_balance = 0
    
    for tenant in tenants:
        balance = calculate_balance(tenant['id'])
        total_balance += balance
        if balance > 0:
            overdue_count += 1
        elif balance < 0:
            credit_count += 1
        else:
            paid_count += 1
    
    # Recent payments for overview
    recent_payments = conn.execute(
        '''SELECT p.*, t.name as tenant_name 
           FROM payments p 
           JOIN tenants t ON p.tenant_id = t.id 
           ORDER BY p.created_at DESC LIMIT 10'''
    ).fetchall()
    
    stats = {
        'total_tenants': total_tenants,
        'total_rent': total_rent,
        'overdue_count': overdue_count,
        'paid_count': paid_count,
        'credit_count': credit_count,
        'total_balance': total_balance
    }
    
    conn.close()
    return render_template('landlord_overview.html', stats=stats, recent_payments=recent_payments)

@app.route('/caretaker')
def caretaker_dashboard():
    if 'admin_id' not in session or session.get('admin_role') != 'caretaker':
        return redirect(url_for('unified_login', login_type='admin'))
    
    conn = get_db()
    # Caretakers handle all operational tasks
    tenants = conn.execute('SELECT * FROM tenants ORDER BY name').fetchall()
    
    # Calculate balances for tenant management
    tenant_data = []
    for tenant in tenants:
        balance = calculate_balance(tenant['id'])
        next_rent_info = get_next_rent_info(tenant['id'])
        tenant_data.append({
            'tenant': tenant,
            'balance': balance,
            'status': 'Overdue' if balance > 0 else 'Paid' if balance == 0 else 'Credit',
            'next_rent_info': next_rent_info
        })
    
    # Get help requests for caretaker
    help_requests = conn.execute(
        '''SELECT hr.*, t.name as tenant_name, t.house_number 
           FROM help_requests hr 
           JOIN tenants t ON hr.tenant_id = t.id 
           ORDER BY hr.created_at DESC LIMIT 5'''
    ).fetchall()
    
    conn.close()
    return render_template('caretaker_dashboard.html', tenant_data=tenant_data, help_requests=help_requests)

@app.route('/admin/tenant/add', methods=['GET', 'POST'])
def add_tenant():
    if 'admin_id' not in session:
        return redirect(url_for('unified_login', login_type='admin'))
    
    if request.method == 'POST':
        conn = get_db()
        cursor = conn.execute(
            'INSERT INTO tenants (name, house_number, phone, email, monthly_rent) VALUES (?, ?, ?, ?, ?)',
            (request.form['name'], request.form['house_number'], 
             request.form['phone'], request.form['email'], 
             float(request.form['monthly_rent']))
        )
        tenant_id = cursor.lastrowid
        
        # Create welcome notification for new tenant
        welcome_message = f"Welcome to RentSync, {request.form['name']}! 🎉 Your account has been set up successfully. You can now make payments, view your balance, and download receipts. If you need any help, please contact your landlord."
        conn.execute(
            'INSERT INTO notifications (tenant_id, message, type) VALUES (?, ?, ?)',
            (tenant_id, welcome_message, 'success')
        )
        
        conn.commit()
        conn.close()
        flash('Tenant added successfully')
        
        # Redirect based on user role
        if session.get('admin_role') == 'caretaker':
            return redirect(url_for('caretaker_dashboard'))
        else:
            return redirect(url_for('admin_dashboard'))
    
    return render_template('add_tenant.html')

@app.route('/admin/tenant/remove/<int:tenant_id>', methods=['POST'])
def remove_tenant(tenant_id):
    if 'admin_id' not in session:
        return redirect(url_for('unified_login', login_type='admin'))
    
    conn = get_db()
    tenant = conn.execute('SELECT name FROM tenants WHERE id = ?', (tenant_id,)).fetchone()
    
    if tenant:
        # Delete related records first
        conn.execute('DELETE FROM payments WHERE tenant_id = ?', (tenant_id,))
        conn.execute('DELETE FROM notifications WHERE tenant_id = ?', (tenant_id,))
        conn.execute('DELETE FROM utility_bills WHERE tenant_id = ?', (tenant_id,))
        conn.execute('DELETE FROM help_requests WHERE tenant_id = ?', (tenant_id,))
        conn.execute('DELETE FROM mpesa_transactions WHERE tenant_id = ?', (tenant_id,))
        
        # Delete tenant
        conn.execute('DELETE FROM tenants WHERE id = ?', (tenant_id,))
        conn.commit()
        flash(f'Tenant {tenant["name"]} removed successfully')
    else:
        flash('Tenant not found')
    
    conn.close()
    
    # Redirect based on user role
    if session.get('admin_role') == 'caretaker':
        return redirect(url_for('caretaker_dashboard'))
    else:
        return redirect(url_for('admin_dashboard'))

@app.route('/admin/payment/add/<int:tenant_id>', methods=['GET', 'POST'])
def add_payment(tenant_id):
    if 'admin_id' not in session:
        return redirect(url_for('unified_login'))
    
    conn = get_db()
    tenant = conn.execute('SELECT * FROM tenants WHERE id = ?', (tenant_id,)).fetchone()
    if not tenant:
        flash('Tenant not found')
        conn.close()
        return redirect(url_for('admin_dashboard'))
    
    balance = calculate_balance(tenant_id)
    recent_payments = conn.execute(
        'SELECT * FROM payments WHERE tenant_id = ? ORDER BY payment_date DESC LIMIT 5',
        (tenant_id,)
    ).fetchall()
    
    if request.method == 'POST':
        amount = float(request.form['amount'])
        payment_date = request.form['payment_date']
        payment_mode = request.form['payment_mode']
        mpesa_code = request.form.get('mpesa_code', '')
        
        conn.execute(
            'INSERT INTO payments (tenant_id, amount, payment_date, payment_mode, mpesa_code, status) VALUES (?, ?, ?, ?, ?, ?)',
            (tenant_id, amount, payment_date, payment_mode, mpesa_code, 'Paid')
        )
        
        # Auto-pay utility bills with remaining amount
        remaining_amount = amount
        pending_bills = conn.execute(
            'SELECT * FROM utility_bills WHERE tenant_id = ? AND status = "Pending" ORDER BY due_date ASC',
            (tenant_id,)
        ).fetchall()
        
        paid_bills = []
        for bill in pending_bills:
            if remaining_amount >= bill['amount']:
                remaining_amount -= bill['amount']
                conn.execute(
                    'UPDATE utility_bills SET status = "Paid" WHERE id = ?',
                    (bill['id'],)
                )
                paid_bills.append(f"{bill['bill_type']} (KSh {bill['amount']:.2f})")
        
        # Send notification to tenant
        notification_message = f"Payment of KSh {amount:.2f} recorded successfully. Thank you!"
        if paid_bills:
            notification_message += f" Utility bills paid: {', '.join(paid_bills)}"
        
        conn.execute(
            'INSERT INTO notifications (tenant_id, message, type) VALUES (?, ?, ?)',
            (tenant_id, notification_message, 'success')
        )
        
        conn.commit()
        conn.close()
        flash(f'Payment of KSh {amount:.2f} recorded for {tenant["name"]}')
        
        # Redirect based on user role
        if session.get('admin_role') == 'caretaker':
            return redirect(url_for('caretaker_dashboard'))
        else:
            return redirect(url_for('admin_dashboard'))
    
    conn.close()
    return render_template('add_payment.html', tenant=tenant, balance=balance, recent_payments=recent_payments)

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
            
            # Auto-pay utility bills
            remaining_amount = transaction['amount']
            pending_bills = conn.execute(
                'SELECT * FROM utility_bills WHERE tenant_id = ? AND status = "Pending" ORDER BY due_date ASC',
                (transaction['tenant_id'],)
            ).fetchall()
            
            for bill in pending_bills:
                if remaining_amount >= bill['amount']:
                    remaining_amount -= bill['amount']
                    conn.execute(
                        'UPDATE utility_bills SET status = "Paid" WHERE id = ?',
                        (bill['id'],)
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

@app.route('/tenant/pay', methods=['GET', 'POST'])
def tenant_payment():
    if 'tenant_id' not in session:
        return redirect(url_for('unified_login'))
    
    tenant_id = session['tenant_id']
    conn = get_db()
    tenant = conn.execute('SELECT * FROM tenants WHERE id = ?', (tenant_id,)).fetchone()
    if not tenant:
        flash('Tenant not found')
        return redirect(url_for('index'))
    
    balance = calculate_balance(tenant_id)
    
    if request.method == 'POST':
        payment_type = request.form.get('payment_type')
        amount = float(request.form['amount'])
        
        if payment_type == 'mpesa':
            # M-Pesa STK Push
            mpesa = MPesaAPI()
            phone = request.form.get('mpesa_phone', tenant['phone'])
            
            if phone.startswith('+'):
                phone = phone[1:]
            if phone.startswith('0'):
                phone = '254' + phone[1:]
            
            result = mpesa.stk_push(
                phone_number=phone,
                amount=amount,
                account_reference=f'RENT-{tenant["house_number"]}',
                transaction_desc=f'Rent payment for {tenant["name"]}'
            )
            
            if result.get('ResponseCode') == '0':
                conn.execute(
                    'INSERT INTO mpesa_transactions (tenant_id, checkout_request_id, merchant_request_id, amount, phone_number) VALUES (?, ?, ?, ?, ?)',
                    (tenant_id, result['CheckoutRequestID'], result['MerchantRequestID'], amount, phone)
                )
                conn.commit()
                conn.close()
                
                return render_template('payment_processing.html', 
                                     tenant=tenant, 
                                     checkout_request_id=result['CheckoutRequestID'],
                                     amount=amount,
                                     mpesa_phone=phone)
            else:
                flash(f'Payment failed: {result.get("errorMessage", "Unknown error")}')
        
        elif payment_type == 'manual':
            # Manual payment entry
            mpesa_code = request.form.get('mpesa_code', '')
            receipt_file = request.files.get('receipt')
            
            receipt_filename = None
            if receipt_file and receipt_file.filename:
                receipt_filename = secure_filename(receipt_file.filename)
                receipt_file.save(os.path.join('uploads', receipt_filename))
            
            conn.execute(
                'INSERT INTO payments (tenant_id, amount, payment_date, payment_mode, status, mpesa_code, receipt_file) VALUES (?, ?, ?, ?, ?, ?, ?)',
                (tenant_id, amount, date.today(), 'Manual Entry', 'Paid', mpesa_code, receipt_filename)
            )
            
            # Auto-pay utility bills for manual payments too
            remaining_amount = amount
            pending_bills = conn.execute(
                'SELECT * FROM utility_bills WHERE tenant_id = ? AND status = "Pending" ORDER BY due_date ASC',
                (tenant_id,)
            ).fetchall()
            
            paid_bills = []
            for bill in pending_bills:
                if remaining_amount >= bill['amount']:
                    remaining_amount -= bill['amount']
                    conn.execute(
                        'UPDATE utility_bills SET status = "Paid" WHERE id = ?',
                        (bill['id'],)
                    )
                    paid_bills.append(f"{bill['bill_type']} (KSh {bill['amount']:.2f})")
            
            conn.commit()
            
            success_message = 'Payment recorded successfully!'
            if paid_bills:
                success_message += f' Utility bills paid: {", ".join(paid_bills)}'
            flash(success_message)
    
    conn.close()
    return render_template('tenant_payment.html', tenant=tenant, balance=balance)

@app.route('/tenant/receipt/<int:payment_id>')
def download_receipt(payment_id):
    if 'tenant_id' not in session:
        return redirect(url_for('tenant_login'))
    
    tenant_id = session['tenant_id']
    conn = get_db()
    tenant = conn.execute('SELECT * FROM tenants WHERE id = ?', (tenant_id,)).fetchone()
    payment = conn.execute('SELECT * FROM payments WHERE id = ? AND tenant_id = ?', (payment_id, tenant_id)).fetchone()
    
    if not tenant or not payment:
        flash('Receipt not found')
        return redirect(url_for('tenant_dashboard'))
    
    # Generate PDF receipt
    buffer = BytesIO()
    p = canvas.Canvas(buffer, pagesize=letter)
    
    # Header
    p.setFont("Helvetica-Bold", 16)
    p.drawString(50, 750, "RentSync - Payment Receipt")
    
    # Tenant details
    p.setFont("Helvetica", 12)
    p.drawString(50, 700, f"Tenant: {tenant['name']}")
    p.drawString(50, 680, f"House/Unit: {tenant['house_number']}")
    p.drawString(50, 660, f"Phone: {tenant['phone']}")
    
    # Payment details
    p.drawString(50, 620, f"Payment Date: {payment['payment_date']}")
    p.drawString(50, 600, f"Amount: KSh {payment['amount']:.2f}")
    p.drawString(50, 580, f"Payment Mode: {payment['payment_mode']}")
    p.drawString(50, 560, f"Status: {payment['status']}")
    
    if payment['mpesa_code']:
        p.drawString(50, 540, f"M-Pesa Code: {payment['mpesa_code']}")
    
    # Footer
    p.drawString(50, 100, f"Generated on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    p.drawString(50, 80, "Thank you for your payment!")
    
    p.save()
    buffer.seek(0)
    
    response = make_response(buffer.getvalue())
    response.headers['Content-Type'] = 'application/pdf'
    response.headers['Content-Disposition'] = f'attachment; filename=receipt_{payment_id}.pdf'
    
    conn.close()
    return response

@app.route('/tenant/statement')
def download_statement():
    if 'tenant_id' not in session:
        return redirect(url_for('tenant_login'))
    
    tenant_id = session['tenant_id']
    conn = get_db()
    tenant = conn.execute('SELECT * FROM tenants WHERE id = ?', (tenant_id,)).fetchone()
    payments = conn.execute(
        'SELECT * FROM payments WHERE tenant_id = ? ORDER BY payment_date DESC', 
        (tenant_id,)
    ).fetchall()
    
    if not tenant:
        flash('Tenant not found')
        return redirect(url_for('index'))
    
    balance = calculate_balance(tenant_id)
    
    # Generate PDF statement
    buffer = BytesIO()
    p = canvas.Canvas(buffer, pagesize=letter)
    
    # Header
    p.setFont("Helvetica-Bold", 16)
    p.drawString(50, 750, "RentSync - Payment Statement")
    
    # Tenant details
    p.setFont("Helvetica", 12)
    p.drawString(50, 700, f"Tenant: {tenant['name']}")
    p.drawString(50, 680, f"House/Unit: {tenant['house_number']}")
    p.drawString(50, 660, f"Monthly Rent: KSh {tenant['monthly_rent']:.2f}")
    p.drawString(50, 640, f"Current Balance: KSh {balance:.2f}")
    
    # Payment history
    p.setFont("Helvetica-Bold", 12)
    p.drawString(50, 600, "Payment History:")
    
    y_position = 580
    p.setFont("Helvetica", 10)
    
    for payment in payments:
        if y_position < 100:
            p.showPage()
            y_position = 750
        
        p.drawString(50, y_position, f"{payment['payment_date']} - KSh {payment['amount']:.2f} ({payment['payment_mode']})")
        y_position -= 20
    
    # Footer
    p.drawString(50, 50, f"Generated on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    
    p.save()
    buffer.seek(0)
    
    response = make_response(buffer.getvalue())
    response.headers['Content-Type'] = 'application/pdf'
    response.headers['Content-Disposition'] = f'attachment; filename=statement_{tenant_id}.pdf'
    
    conn.close()
    return response

@app.route('/tenant/notifications/mark-read', methods=['POST'])
def mark_notifications_read():
    """Mark tenant notifications as read"""
    if 'tenant_id' not in session:
        return jsonify({'success': False, 'message': 'Unauthorized'})
    
    tenant_id = session['tenant_id']
    conn = get_db()
    conn.execute('UPDATE notifications SET is_read = TRUE WHERE tenant_id = ? AND is_read = FALSE AND deleted_at IS NULL', (tenant_id,))
    conn.commit()
    conn.close()
    
    return jsonify({'success': True, 'message': 'Notifications marked as read'})

@app.route('/tenant/notifications/delete/<int:notification_id>', methods=['POST'])
def delete_notification(notification_id):
    """Move notification to bin (soft delete)"""
    if 'tenant_id' not in session:
        return jsonify({'success': False, 'message': 'Unauthorized'})
    
    tenant_id = session['tenant_id']
    conn = get_db()
    
    # Soft delete by setting deleted_at timestamp
    conn.execute(
        'UPDATE notifications SET deleted_at = CURRENT_TIMESTAMP WHERE id = ? AND tenant_id = ?',
        (notification_id, tenant_id)
    )
    conn.commit()
    conn.close()
    
    return jsonify({'success': True, 'message': 'Notification moved to bin'})

@app.route('/tenant/notifications/bin')
def notification_bin():
    """View deleted notifications"""
    if 'tenant_id' not in session:
        return redirect(url_for('tenant_login'))
    
    tenant_id = session['tenant_id']
    conn = get_db()
    
    tenant = conn.execute('SELECT * FROM tenants WHERE id = ?', (tenant_id,)).fetchone()
    deleted_notifications = conn.execute(
        'SELECT * FROM notifications WHERE tenant_id = ? AND deleted_at IS NOT NULL ORDER BY deleted_at DESC',
        (tenant_id,)
    ).fetchall()
    
    conn.close()
    return render_template('notification_bin.html', tenant=tenant, notifications=deleted_notifications)

@app.route('/tenant/notifications/restore/<int:notification_id>', methods=['POST'])
def restore_notification(notification_id):
    """Restore notification from bin"""
    if 'tenant_id' not in session:
        return jsonify({'success': False, 'message': 'Unauthorized'})
    
    tenant_id = session['tenant_id']
    conn = get_db()
    
    conn.execute(
        'UPDATE notifications SET deleted_at = NULL, is_read = FALSE WHERE id = ? AND tenant_id = ?',
        (notification_id, tenant_id)
    )
    conn.commit()
    conn.close()
    
    return jsonify({'success': True, 'message': 'Notification restored'})

@app.route('/tenant/notifications/clear-bin', methods=['POST'])
def clear_notification_bin():
    """Permanently delete all notifications in bin"""
    if 'tenant_id' not in session:
        return jsonify({'success': False, 'message': 'Unauthorized'})
    
    tenant_id = session['tenant_id']
    conn = get_db()
    
    conn.execute(
        'DELETE FROM notifications WHERE tenant_id = ? AND deleted_at IS NOT NULL',
        (tenant_id,)
    )
    conn.commit()
    conn.close()
    
    return jsonify({'success': True, 'message': 'Bin cleared permanently'})

@app.route('/tenant/profile', methods=['GET', 'POST'])
def tenant_profile():
    if 'tenant_id' not in session:
        return redirect(url_for('unified_login'))
    
    tenant_id = session['tenant_id']
    conn = get_db()
    
    if request.method == 'POST':
        profile_picture = request.files.get('profile_picture')
        
        if profile_picture and profile_picture.filename:
            try:
                # Ensure directory exists
                upload_dir = os.path.join(os.getcwd(), 'static', 'profile_pictures')
                os.makedirs(upload_dir, exist_ok=True)
                print(f"Upload directory: {upload_dir}")
                
                # Generate simple filename
                file_ext = profile_picture.filename.rsplit('.', 1)[1].lower() if '.' in profile_picture.filename else 'jpg'
                filename = f"tenant_{tenant_id}.{file_ext}"
                filepath = os.path.join(upload_dir, filename)
                print(f"Saving to: {filepath}")
                
                # Save file
                profile_picture.save(filepath)
                print(f"File saved successfully: {filename}")
                print(f"Database updated for tenant {tenant_id}")
                
                # Update database
                conn.execute('UPDATE tenants SET profile_picture = ? WHERE id = ?', (filename, tenant_id))
                conn.commit()
                flash('✅ Profile picture updated successfully!')
                
            except Exception as e:
                print(f"Upload error: {str(e)}")
                flash(f'Upload failed: {str(e)}')
        else:
            flash('Please select a file to upload.')
        
        conn.close()
        return redirect(url_for('tenant_profile'))
    
    tenant = conn.execute('SELECT * FROM tenants WHERE id = ?', (tenant_id,)).fetchone()
    conn.close()
    
    return render_template('tenant_profile.html', tenant=tenant)

@app.route('/tenant/help', methods=['GET', 'POST'])
def tenant_help():
    if 'tenant_id' not in session:
        return redirect(url_for('unified_login'))
    
    tenant_id = session['tenant_id']
    conn = get_db()
    
    if request.method == 'POST':
        category = request.form['category']
        subject = request.form['subject']
        message = request.form['message']
        
        conn.execute(
            'INSERT INTO help_requests (tenant_id, category, subject, message) VALUES (?, ?, ?, ?)',
            (tenant_id, category, subject, message)
        )
        conn.commit()
        flash('Help request submitted successfully! You will receive a response soon.')
        return redirect(url_for('tenant_help'))
    
    # Get tenant's help requests
    tenant = conn.execute('SELECT * FROM tenants WHERE id = ?', (tenant_id,)).fetchone()
    help_requests = conn.execute(
        'SELECT * FROM help_requests WHERE tenant_id = ? ORDER BY created_at DESC',
        (tenant_id,)
    ).fetchall()
    
    conn.close()
    return render_template('tenant_help.html', tenant=tenant, help_requests=help_requests)

@app.route('/admin/notify/<int:tenant_id>', methods=['POST'])
def send_notification(tenant_id):
    """Send web notification to tenant"""
    if 'admin_id' not in session:
        return jsonify({'success': False, 'message': 'Unauthorized'})
    
    message = request.json.get('message', '')
    notification_type = request.json.get('type', 'info')
    
    if not message:
        # Generate automatic notification based on tenant status
        notifications = generate_notifications(tenant_id)
        if notifications:
            message = notifications[0]['message']
            notification_type = notifications[0]['type']
        else:
            return jsonify({'success': False, 'message': 'No notification to send'})
    
    success = save_notification(tenant_id, message, notification_type)
    return jsonify({'success': success, 'message': 'Notification sent to tenant dashboard'})

@app.route('/admin/help-requests')
def admin_help_requests():
    if 'admin_id' not in session:
        return redirect(url_for('admin_login'))
    
    conn = get_db()
    if session.get('admin_role') == 'caretaker':
        # Caretakers only see maintenance, security, and utilities requests
        help_requests = conn.execute(
            '''SELECT hr.*, t.name as tenant_name, t.house_number 
               FROM help_requests hr 
               JOIN tenants t ON hr.tenant_id = t.id 
               WHERE hr.category IN ('Maintenance', 'Security', 'Utilities')
               ORDER BY hr.created_at DESC'''
        ).fetchall()
    else:
        # Landlords see all requests
        help_requests = conn.execute(
            '''SELECT hr.*, t.name as tenant_name, t.house_number 
               FROM help_requests hr 
               JOIN tenants t ON hr.tenant_id = t.id 
               ORDER BY hr.created_at DESC'''
        ).fetchall()
    conn.close()
    
    dashboard_url = '/caretaker' if session.get('admin_role') == 'caretaker' else '/admin'
    return render_template('admin_help_requests.html', help_requests=help_requests, dashboard_url=dashboard_url)

@app.route('/admin/utility/add/<int:tenant_id>', methods=['GET', 'POST'])
def add_utility_bill(tenant_id):
    if 'admin_id' not in session:
        return redirect(url_for('unified_login', login_type='admin'))
    
    conn = get_db()
    tenant = conn.execute('SELECT * FROM tenants WHERE id = ?', (tenant_id,)).fetchone()
    if not tenant:
        flash('Tenant not found')
        conn.close()
        return redirect(url_for('caretaker_dashboard'))
    
    if request.method == 'POST':
        bill_type = request.form['bill_type']
        amount = float(request.form['amount'])
        due_date = request.form['due_date']
        
        conn.execute(
            'INSERT INTO utility_bills (tenant_id, bill_type, amount, due_date) VALUES (?, ?, ?, ?)',
            (tenant_id, bill_type, amount, due_date)
        )
        
        # Send notification to tenant
        notification_message = f"New {bill_type} bill: KSh {amount:.2f} due on {due_date}"
        conn.execute(
            'INSERT INTO notifications (tenant_id, message, type) VALUES (?, ?, ?)',
            (tenant_id, notification_message, 'warning')
        )
        
        conn.commit()
        conn.close()
        flash(f'{bill_type} bill added for {tenant["name"]}')
        
        if session.get('admin_role') == 'caretaker':
            return redirect(url_for('caretaker_dashboard'))
        else:
            return redirect(url_for('admin_dashboard'))
    
    conn.close()
    return render_template('add_utility_bill.html', tenant=tenant)

@app.route('/admin/help-request/<int:request_id>', methods=['GET', 'POST'])
def admin_respond_help(request_id):
    if 'admin_id' not in session:
        return redirect(url_for('admin_login'))
    
    conn = get_db()
    
    if request.method == 'POST':
        response = request.form['response']
        status = request.form['status']
        
        conn.execute(
            'UPDATE help_requests SET admin_response = ?, status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?',
            (response, status, request_id)
        )
        
        # Get tenant info to send notification
        help_request = conn.execute(
            '''SELECT hr.*, t.name as tenant_name 
               FROM help_requests hr 
               JOIN tenants t ON hr.tenant_id = t.id 
               WHERE hr.id = ?''',
            (request_id,)
        ).fetchone()
        
        conn.commit()
        
        if help_request:
            # Send notification to tenant
            notification_message = f"Response to your help request: '{help_request['subject']}' - Status: {status}"
            conn.execute(
                'INSERT INTO notifications (tenant_id, message, type) VALUES (?, ?, ?)',
                (help_request['tenant_id'], notification_message, 'info')
            )
            conn.commit()
        
        conn.close()
        flash('Response sent successfully!')
        return redirect(url_for('admin_help_requests'))
    
    help_request = conn.execute(
        '''SELECT hr.*, t.name as tenant_name, t.house_number 
           FROM help_requests hr 
           JOIN tenants t ON hr.tenant_id = t.id 
           WHERE hr.id = ?''',
        (request_id,)
    ).fetchone()
    
    conn.close()
    dashboard_url = '/caretaker' if session.get('admin_role') == 'caretaker' else '/admin'
    return render_template('admin_help_response.html', help_request=help_request, dashboard_url=dashboard_url)

if __name__ == '__main__':
    init_db()
    app.run(debug=True)