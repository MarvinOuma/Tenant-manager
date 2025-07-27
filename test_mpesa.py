import base64
from datetime import datetime
from mpesa import MPesaAPI

def generate_simulator_values():
    """Generate values needed for M-Pesa simulator"""
    
    # Initialize M-Pesa API
    mpesa = MPesaAPI()
    
    # Generate timestamp
    timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
    
    # Generate password (Base64 encoded: BusinessShortCode + PassKey + Timestamp)
    password_string = f"{mpesa.business_shortcode}{mpesa.passkey}{timestamp}"
    password = base64.b64encode(password_string.encode()).decode()
    
    print("=== M-Pesa Simulator Values ===")
    print(f"Business Short Code: {mpesa.business_shortcode}")
    print(f"Timestamp: {timestamp}")
    print(f"Pass Key: {mpesa.passkey}")
    print(f"Password (Base64): {password}")
    print("\n=== Test STK Push ===")
    
    # Test STK Push
    result = mpesa.stk_push(
        phone_number='254708374149',
        amount=100,
        account_reference='TEST-RENT',
        transaction_desc='Test rent payment'
    )
    
    print(f"STK Push Result: {result}")
    
    if result.get('CheckoutRequestID'):
        print(f"\nCheckout Request ID: {result['CheckoutRequestID']}")
        print("Use this Checkout Request ID in the simulator!")

if __name__ == "__main__":
    generate_simulator_values()