import json,requests
import frappe
from datetime import datetime
from frappe.utils import date_diff
from bio_integ.token import get_token
from frappe.utils import now_datetime

# Setup logger
logger = frappe.logger("biometric_integration", allow_site=True, file_count=5)

# Constants
MONTHLY_ROLLOVER_DAYS = 32  # Days to wait before advancing start_time by one month


def get_settings():
	"""Get Biometric Settings document"""
	return frappe.get_doc("Biometric Settings")


@frappe.whitelist()
def pull_filtered_checkin(filters, param="", next=None):
	settings = get_settings()
	doc = frappe.get_doc("Pull Checkin")
	loaded_filters = frappe._dict(json.loads(filters))
	if not param:
		if loaded_filters.attendance_device_id:
			param = "?page_size={}&start_time={}&end_time={}&emp_code={}".format(settings.size, loaded_filters.start_time,loaded_filters.to_time, loaded_filters.attendance_device_id)
		else:
			param = "?page_size={}&start_time={}&end_time={}".format(settings.size, loaded_filters.start_time, loaded_filters.to_time)

	if next:
		url = next
	else:
		url = settings.url + param

	# Iterative approach to handle pagination without recursion
	while url:
		data = send_request(url, settings)
		if data:
			if data.get("data"):
				checkinout = data['data']
				doc.add_comment("Comment", f"Adding {len(checkinout)} record(s)")
				add_employee_checkins(checkinout)
				url = data.get("next")
			else:
				break
		else:
			frappe.throw(str(data))


@frappe.whitelist()
def execute(next=None):
	settings = get_settings()
	if next:
		url = next
	else:
		param = f"?start_time={settings.start_time}&page_size={settings.size}"
		url = settings.url + param

	# Iterative approach to handle pagination without recursion
	while url:
		data = send_request(url, settings)
		if data:
			if data.get("data"):
				checkinout = data['data']
				filtered_checkin = [d for d in checkinout if datetime.strptime(d['punch_time'], '%Y-%m-%d %H:%M:%S') >= datetime.strptime(settings.start_time, '%Y-%m-%d %H:%M:%S')]
				add_employee_checkins(filtered_checkin)
				url = data.get("next")
			else:
				break
		else:
			frappe.throw(str(data))

def send_request(url, settings):
	logger.info(f"Fetching data from: {url}")
	headers = {
		"Content-Type": "application/json",
		"Authorization": settings.key,
	}
	payload = {}
	try:
		response = requests.get(url, headers=headers, params=payload, timeout=settings.timeout, verify=False)
		response.raise_for_status()  # Raises HTTPError for bad status codes
		return response.json()
	except requests.exceptions.Timeout:
		frappe.log_error("Biometric API request timeout", "Biometric Integration")
		frappe.throw("Biometric API request timed out. Please try again later.")
	except requests.exceptions.HTTPError as e:
		frappe.log_error(f"Biometric API HTTP error: {str(e)}", "Biometric Integration")
		frappe.throw(f"Biometric API returned an error: {str(e)}")
	except requests.exceptions.RequestException as e:
		frappe.log_error(f"Biometric API request error: {str(e)}", "Biometric Integration")
		frappe.throw(f"Failed to connect to Biometric API: {str(e)}")
	except ValueError as e:
		frappe.log_error(f"Invalid JSON response from Biometric API: {str(e)}", "Biometric Integration")
		frappe.throw("Received invalid response from Biometric API")

def add_employee_checkins(filtered_checkin):
	settings = get_settings()
	checkins_created = 0
	processed_emp_codes = []

	for checkin_record in filtered_checkin:
		emp_code = checkin_record["emp_code"]

		# Track unique employee codes
		if emp_code not in processed_emp_codes:
			processed_emp_codes.append(emp_code)

		if checkin_record['punch_state'] in ["0","1","255"]:
			punch_dict = {"0":"IN","1":"OUT","255":""}
			# Single query - get_value returns None if employee doesn't exist
			employee = frappe.db.get_value("Employee", {"attendance_device_id": emp_code}, "name")
			if not employee:
				if not frappe.db.exists("Bio logs", {"code": emp_code}):
					log = frappe.new_doc("Bio logs")
					log.code = emp_code
					log.log = "code {} is not attached to any employee".format(emp_code)
					log.save()
			else:
				checkins_created += 1
				time = checkin_record['punch_time']
				location = checkin_record['terminal_alias']
				try:
					create_checkin(employee, time, location, punch_dict[checkin_record['punch_state']])
				except Exception as e:
					frappe.log_error(f"Error adding employee checkin for employee {employee}")
	if settings.update_last_checkin:
		shift_list = frappe.get_all('Shift Type', 'name', {'enable_auto_attendance':'1'}, as_list=True)
		logger.info(f"Updating last sync time for {len(shift_list)} shift types")
		for row in shift_list:
			logger.debug(f"Updating shift type: {row[0]}")
			frappe.set_value('Shift Type', row[0], 'last_sync_of_checkin',now_datetime())

	# Commit all changes at once for better performance
	frappe.db.commit()


def create_checkin(employee, time, location, log_type):
	"""Create an Employee Checkin record if it doesn't already exist"""
	# Check for duplicate to avoid unnecessary processing
	if frappe.db.exists("Employee Checkin", {"time": time, "employee": employee}):
		return

	try:
		echeck = frappe.new_doc("Employee Checkin")
		echeck.employee = employee
		echeck.time = time
		echeck.log_type = log_type
		echeck.device_id = location
		echeck.shift = frappe.db.get_value("Employee", employee, "default_shift")
		echeck.save()
	except frappe.DuplicateEntryError:
		# Handle race condition where record was created between check and insert
		pass


def update_start_time():
	"""Update start_time to next month after MONTHLY_ROLLOVER_DAYS"""
	settings = get_settings()
	start = datetime.strptime(settings.start_time, "%Y-%m-%d %H:%M:%S")
	if date_diff(datetime.today(), start) == MONTHLY_ROLLOVER_DAYS:
		settings.start_time = frappe.utils.add_months(start, 1)
		settings.save()

@frappe.whitelist()
def update_key():
	settings = get_settings()
	settings.db_set("key", get_token())
	return get_token()
		

