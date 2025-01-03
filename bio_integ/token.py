import json
import requests
import frappe
from frappe.utils.password import get_decrypted_password



settings = frappe.get_doc("Biometric Settings")

def get_token():
	headers = {
		"Content-Type": "application/json",
	} 
	data = {
		"username": settings.username,
		"password": get_decrypted_password("Biometric Settings","Biometric Settings","password"),
		"company":"Kaddah"
	}
	response = requests.post(settings.token_url,data=json.dumps(data), headers=headers,verify=False)
	token = json.loads(response.text)
	return "JWT " + token['token']



