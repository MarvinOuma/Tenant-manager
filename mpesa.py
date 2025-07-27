import requests
import base64
from datetime import datetime
import os
from dotenv import load_dotenv

load_dotenv()

class MPesaAPI:
    def __init__(self):
        self.consumer_key = os.getenv('MPESA_CONSUMER_KEY')
        self.consumer_secret = os.getenv('MPESA_CONSUMER_SECRET')
        self.business_shortcode = os.getenv('MPESA_SHORTCODE', '174379')
        self.passkey = os.getenv('MPESA_PASSKEY', 'bfb279f9aa9bdbcf158e97dd71a467cd2e0c893059b10f78e6b72ada1ed2c919')
        self.base_url = 'https://sandbox.safaricom.co.ke'  # ( https://api.safaricom.co.ke )for production
        
    def get_access_token(self):
        url = f'{self.base_url}/oauth/v1/generate?grant_type=client_credentials'
        credentials = base64.b64encode(f'{self.consumer_key}:{self.consumer_secret}'.encode()).decode()
        
        headers = {'Authorization': f'Basic {credentials}'}
        response = requests.get(url, headers=headers)
        
        if response.status_code == 200:
            return response.json()['access_token']
        return None
    
    def stk_push(self, phone_number, amount, account_reference, transaction_desc):
        access_token = self.get_access_token()
        if not access_token:
            return {'success': False, 'message': 'Failed to get access token'}
        
        timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
        password = base64.b64encode(f'{self.business_shortcode}{self.passkey}{timestamp}'.encode()).decode()
        
        url = f'{self.base_url}/mpesa/stkpush/v1/processrequest'
        headers = {
            'Authorization': f'Bearer {access_token}',
            'Content-Type': 'application/json'
        }
        
        payload = {
            'BusinessShortCode': self.business_shortcode,
            'Password': password,
            'Timestamp': timestamp,
            'TransactionType': 'CustomerPayBillOnline',
            'Amount': int(amount),
            'PartyA': phone_number,
            'PartyB': self.business_shortcode,
            'PhoneNumber': phone_number,
            'CallBackURL': 'https://mydomain.com/mpesa/callback',  # Dummy URL for sandbox testing
            'AccountReference': account_reference,
            'TransactionDesc': transaction_desc
        }
        
        response = requests.post(url, json=payload, headers=headers)
        return response.json()
    
    def query_transaction(self, checkout_request_id):
        access_token = self.get_access_token()
        if not access_token:
            return {'success': False, 'message': 'Failed to get access token'}
        
        timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
        password = base64.b64encode(f'{self.business_shortcode}{self.passkey}{timestamp}'.encode()).decode()
        
        url = f'{self.base_url}/mpesa/stkpushquery/v1/query'
        headers = {
            'Authorization': f'Bearer {access_token}',
            'Content-Type': 'application/json'
        }
        
        payload = {
            'BusinessShortCode': self.business_shortcode,
            'Password': password,
            'Timestamp': timestamp,
            'CheckoutRequestID': checkout_request_id
        }
        
        response = requests.post(url, json=payload, headers=headers)
        return response.json()