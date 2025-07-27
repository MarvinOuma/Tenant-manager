# RentSync - Tenant Tracking System

A minimal tenant tracking system for landlords to manage rent payments and tenant information.

## Features

### Tenant Features
- View personal details (name, house number, phone)
- Check rent balance (overdue/credit/paid)
- View payment history with dates and amounts
- See payment status and modes

### Admin Features
- Add new tenants with monthly rent amounts
- Record rent payments (cash, M-Pesa, bank transfer, cheque)
- View all tenants with balance status
- Automatic balance calculation based on months since tenant creation
- Dashboard showing overdue/paid/credit tenants

## Quick Start

1. Install dependencies:
```bash
pip install -r requirements.txt
```

2. Run the application:
```bash
python app.py
```

3. Open http://localhost:5000 in your browser

4. Default admin login: `admin` / `admin123`

## Usage

1. **Admin Login**: Use admin credentials to access the admin dashboard
2. **Add Tenants**: Create tenant profiles with monthly rent amounts
3. **Record Payments**: Log payments as they come in
4. **Tenant Access**: Tenants can view their dashboard using their tenant ID
5. **Balance Tracking**: System automatically calculates balances based on months since tenant creation

## Database

Uses SQLite database (`rentsync.db`) with three tables:
- `tenants`: Tenant information and monthly rent
- `payments`: Payment records
- `admins`: Admin user accounts

Balance calculation: `(Monthly Rent × Months Since Creation) - Total Payments`