import json,requests
import frappe
from datetime import datetime
from frappe.utils import date_diff
from bio_integ.token import get_token
from frappe.utils import now_datetime




settings = frappe.get_doc("Biometric Settings")
headers = {
		"Content-Type": "application/json",
		"Authorization": settings.key,
}
payload = {
	# "page_size": settings.size,
}
param = f"?start_time={settings.start_time}&page_size={settings.size}"


@frappe.whitelist()
def pull_filtered_checkin(filters, param="", next=None):
	settings = frappe.get_doc("Biometric Settings")
	doc = frappe.get_doc("Pull Checkin")
	loaded_filters = frappe._dict(json.loads(filters))
	if not param:
		if loaded_filters.attendance_device_id:
			param = "?page_size={}&start_time={}&end_time={}&emp_code={}".format(settings.size, loaded_filters.start_time,loaded_filters.to_time, loaded_filters.attendance_device_id)
		else:
			param = "?page_size={}&start_time={}&end_time={}".format(settings.size, loaded_filters.start_time, loaded_filters.to_time)
	if next:
		data = send_request(next)
	else:
		data = send_request(settings.url+param)
	if data:
		if data["data"]:
			checkinout = data['data']
			doc.add_comment("Comment",f"Adding {len(checkinout)} record(s)") 
			add_employee_checkins(checkinout)
			if data["next"]:
				pull_filtered_checkin(filters, param=param, next=data["next"])
	else:
		frappe.throw(str(data))


@frappe.whitelist()
def execute(next=None):
	if next:
		data = send_request(next)
	else:
		settings = frappe.get_doc("Biometric Settings")
		data = send_request(settings.url+param)
	if data:
		if data["data"]:
			checkinout = data['data']
			filtered_checkin = [d for d in checkinout if datetime.strptime(d['punch_time'], '%Y-%m-%d %H:%M:%S') >= datetime.strptime(settings.start_time, '%Y-%m-%d %H:%M:%S')]
			add_employee_checkins(filtered_checkin)
			if data["next"]:
				execute(next=data["next"])
	else:
		frappe.throw(str(data))

def send_request(url):
	print(url)
	response = requests.get(url, headers=headers, params=payload, timeout=settings.timeout, verify=False)
	return response.json()

def add_employee_checkins(filtered_checkin):
	settings = frappe.get_doc("Biometric Settings")
	log_type = ""
	l = 0
	code = []
	for c in range(len(filtered_checkin)):
		if not filtered_checkin[c]["emp_code"] in code:
			code.append(filtered_checkin[c]["emp_code"])
		if filtered_checkin[c]['punch_state'] in ["0","1","255"]:
			punch_dict = {"0":"IN","1":"OUT","255":""}
			employee = frappe.db.get_value("Employee",{"attendance_device_id":filtered_checkin[c]["emp_code"]},"name")
			if not frappe.db.exists("Employee",{"attendance_device_id":filtered_checkin[c]["emp_code"]}):
				if not frappe.db.exists("Bio logs",{"code":filtered_checkin[c]["emp_code"]}):
					log = frappe.new_doc("Bio logs")
					log.code = filtered_checkin[c]["emp_code"]
					log.log = "code {} is not attached to any employee".format(filtered_checkin[c]["emp_code"])
					log.save()
			else:
				l+= 1
				time = filtered_checkin[c]['punch_time']
				location = filtered_checkin[c]['terminal_alias']
				create_checkin(employee,time,location,punch_dict[filtered_checkin[c]['punch_state']])
	if settings.update_last_checkin:
		shift_list = frappe.get_all('Shift Type', 'name', {'enable_auto_attendance':'1'}, as_list=True)
		print(shift_list)
		for row in shift_list:
			print(row[0])
			frappe.set_value('Shift Type', row[0], 'last_sync_of_checkin',now_datetime())
			frappe.db.commit()


def create_checkin(employee,time,location,log_type):
	# echeck = frappe.new_doc("Employee Checkin")
	if frappe.db.exists("Employee Checkin",{"time":time,"employee":employee}):
		pass
	else:
		echeck = frappe.new_doc("Employee Checkin")
		echeck.employee = employee
		echeck.time = time
		echeck.log_type = log_type
		echeck.device_id = location
		echeck.shift = frappe.db.get_value("Employee",employee,"default_shift")
		echeck.save()
		frappe.db.commit()


def update_start_time():
	start = datetime.strptime(settings.start_time, "%Y-%m-%d %H:%M:%S")
	if date_diff(datetime.today(),start) ==32:
		settings.start_time = frappe.utils.add_months(start, 1)
		settings.save()

@frappe.whitelist()
def update_key():
	settings.db_set("key", get_token())
	return get_token()
		

