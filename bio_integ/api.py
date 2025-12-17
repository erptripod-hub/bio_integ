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


def process_checkins_background(filters, doc_name=None):
	"""
	Background job function to process large checkin imports.

	Args:
		filters: dict with start_time, to_time, and optional attendance_device_id
		doc_name: Optional name of Pull Checkin document to update with progress

	Returns:
		dict: Summary of processing results
	"""
	try:
		settings = get_settings()
		loaded_filters = frappe._dict(filters if isinstance(filters, dict) else json.loads(filters))

		# Build API URL with parameters
		if loaded_filters.get("attendance_device_id"):
			param = "?page_size={}&start_time={}&end_time={}&emp_code={}".format(
				settings.size,
				loaded_filters.start_time,
				loaded_filters.to_time,
				loaded_filters.attendance_device_id
			)
		else:
			param = "?page_size={}&start_time={}&end_time={}".format(
				settings.size,
				loaded_filters.start_time,
				loaded_filters.to_time
			)

		url = settings.url + param

		# Collect all records from paginated API
		all_checkins = []
		page_count = 0

		logger.info(f"Background job started: Fetching checkins from {loaded_filters.start_time} to {loaded_filters.to_time}")

		while url:
			page_count += 1
			logger.info(f"Fetching page {page_count} from: {url}")

			data = send_request(url, settings)
			if data and data.get("data"):
				checkinout = data['data']
				all_checkins.extend(checkinout)
				logger.info(f"Page {page_count}: Fetched {len(checkinout)} records (Total: {len(all_checkins)})")

				url = data.get("next")
			else:
				break

		logger.info(f"Fetch complete: {len(all_checkins)} total records from {page_count} pages")

		# Process all records using bulk function
		if all_checkins:
			summary = add_employee_checkins_bulk(all_checkins)

			# Update Pull Checkin document with results if doc_name provided
			if doc_name:
				try:
					doc = frappe.get_doc("Pull Checkin", doc_name)
					doc.add_comment(
						"Comment",
						f"Background processing complete: {summary['created']} created, "
						f"{summary['skipped']} skipped, {summary['unmatched']} unmatched"
					)
				except Exception as e:
					logger.error(f"Could not update Pull Checkin document: {str(e)}")

			# Send notification to user
			frappe.publish_realtime(
				"msgprint",
				{
					"message": f"Checkin import complete: {summary['created']} created, "
							   f"{summary['skipped']} duplicates skipped, "
							   f"{summary['unmatched']} unmatched employee codes",
					"title": "Import Complete",
					"indicator": "green"
				},
				user=frappe.session.user
			)

			logger.info(f"Background job complete: {summary}")
			return summary
		else:
			logger.info("No records found to process")
			if doc_name:
				try:
					doc = frappe.get_doc("Pull Checkin", doc_name)
					doc.add_comment("Comment", "No records found in the specified time range")
				except Exception:
					pass

			return {"created": 0, "skipped": 0, "unmatched": 0}

	except Exception as e:
		logger.error(f"Background job failed: {str(e)}")
		frappe.log_error(f"Background checkin processing failed: {str(e)}", "Biometric Integration")

		# Notify user of failure
		frappe.publish_realtime(
			"msgprint",
			{
				"message": f"Checkin import failed: {str(e)}",
				"title": "Import Failed",
				"indicator": "red"
			},
			user=frappe.session.user
		)
		raise


@frappe.whitelist()
def pull_filtered_checkin(filters, param="", next=None):
	"""
	Pull filtered checkin records from biometric API.
	Uses threshold-based processing: inline for small datasets, background job for large ones.

	Args:
		filters: JSON string with start_time, to_time, and optional attendance_device_id
		param: Pre-built parameter string (optional)
		next: Pagination URL (optional)
	"""
	settings = get_settings()
	doc = frappe.get_doc("Pull Checkin")
	loaded_filters = frappe._dict(json.loads(filters))

	# Build parameter string if not provided
	if not param:
		if loaded_filters.attendance_device_id:
			param = "?page_size={}&start_time={}&end_time={}&emp_code={}".format(
				settings.size,
				loaded_filters.start_time,
				loaded_filters.to_time,
				loaded_filters.attendance_device_id
			)
		else:
			param = "?page_size={}&start_time={}&end_time={}".format(
				settings.size,
				loaded_filters.start_time,
				loaded_filters.to_time
			)

	# Determine URL
	if next:
		url = next
	else:
		url = settings.url + param

	# Fetch first page to estimate total records
	first_page_data = send_request(url, settings)

	if not first_page_data or not first_page_data.get("data"):
		frappe.msgprint("No records found in the specified time range", indicator="yellow")
		return

	first_page_records = first_page_data['data']
	has_more_pages = bool(first_page_data.get("next"))

	# Estimate total records (rough estimate based on first page)
	# If there's a next page and first page is near page_size, assume large dataset
	estimated_total = len(first_page_records)
	if has_more_pages:
		estimated_total = int(settings.size) * 10  # Conservative estimate

	logger.info(f"First page: {len(first_page_records)} records, Has more: {has_more_pages}, Estimated: {estimated_total}")

	# Threshold for background processing: 1000 records
	BACKGROUND_THRESHOLD = 1000

	if estimated_total > BACKGROUND_THRESHOLD:
		# Large dataset - queue to background
		logger.info(f"Large dataset detected ({estimated_total} estimated records). Queuing to background job.")

		# Queue the background job
		frappe.enqueue(
			"bio_integ.api.process_checkins_background",
			filters=loaded_filters,
			doc_name=doc.name,
			queue="long",
			timeout=1800,  # 30 minutes
			now=False
		)

		# Immediate user feedback
		frappe.msgprint(
			f"Large dataset detected (estimated {estimated_total}+ records). "
			"Processing in background. You will be notified when complete.",
			title="Processing in Background",
			indicator="blue"
		)

		doc.add_comment("Comment", f"Background job queued for large dataset (estimated {estimated_total}+ records)")

	else:
		# Small dataset - process inline with bulk function
		logger.info(f"Small dataset ({estimated_total} records). Processing inline.")

		all_checkins = []
		current_url = url

		# Collect all pages
		while current_url:
			data = send_request(current_url, settings)
			if data and data.get("data"):
				checkinout = data['data']
				all_checkins.extend(checkinout)
				doc.add_comment("Comment", f"Fetched {len(checkinout)} record(s) (Total: {len(all_checkins)})")
				current_url = data.get("next")
			else:
				break

		# Process using bulk function
		if all_checkins:
			summary = add_employee_checkins_bulk(all_checkins)

			# User feedback
			frappe.msgprint(
				f"Import complete: {summary['created']} created, "
				f"{summary['skipped']} duplicates skipped, "
				f"{summary['unmatched']} unmatched employee codes",
				title="Import Complete",
				indicator="green"
			)

			doc.add_comment(
				"Comment",
				f"Processing complete: {summary['created']} created, "
				f"{summary['skipped']} skipped, {summary['unmatched']} unmatched"
			)


@frappe.whitelist()
def execute(next=None):
	"""
	Scheduled hourly task to sync employee checkins from biometric API.
	Uses optimized bulk processing for better performance.

	Args:
		next: Pagination URL (optional)
	"""
	settings = get_settings()
	if next:
		url = next
	else:
		param = f"?start_time={settings.start_time}&page_size={settings.size}"
		url = settings.url + param

	logger.info(f"Scheduled sync started from {settings.start_time}")

	# Collect all records from all pages
	all_checkins = []
	page_count = 0

	while url:
		data = send_request(url, settings)
		if data:
			if data.get("data"):
				checkinout = data['data']
				page_count += 1
				all_checkins.extend(checkinout)
				logger.info(f"Page {page_count}: Fetched {len(checkinout)} records (Total: {len(all_checkins)})")
				url = data.get("next")
			else:
				break
		else:
			logger.error(f"No data received from API: {str(data)}")
			frappe.throw(str(data))

	# Early exit if no data
	if not all_checkins:
		logger.info("No records found to sync")
		return

	# Filter records by start_time
	start_dt = datetime.strptime(settings.start_time, '%Y-%m-%d %H:%M:%S')
	filtered_checkin = [
		d for d in all_checkins
		if datetime.strptime(d['punch_time'], '%Y-%m-%d %H:%M:%S') >= start_dt
	]

	logger.info(f"Filtered {len(filtered_checkin)} records (from {len(all_checkins)} total) after start_time filter")

	# Process using optimized bulk function
	if filtered_checkin:
		summary = add_employee_checkins_bulk(filtered_checkin)
		logger.info(f"Scheduled sync complete: {summary['created']} created, {summary['skipped']} skipped, {summary['unmatched']} unmatched")
	else:
		logger.info("No records to process after filtering")

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

def add_employee_checkins_bulk(filtered_checkin, batch_size=500):
	"""
	Optimized bulk processing of employee checkins with caching and batch commits.

	Args:
		filtered_checkin: List of checkin records from biometric API
		batch_size: Number of records to process per batch (default: 500)

	Returns:
		dict: Summary with counts of created, skipped, and unmatched records
	"""
	if not filtered_checkin:
		return {"created": 0, "skipped": 0, "unmatched": 0}

	settings = get_settings()

	# Pre-fetch all employees with attendance_device_id (single query)
	logger.info("Pre-fetching employee data for bulk processing")
	employees = frappe.get_all(
		"Employee",
		filters={"attendance_device_id": ["!=", ""]},
		fields=["name", "attendance_device_id", "default_shift"]
	)

	# Create lookup dict: {device_id: {name, shift}}
	employee_map = {
		emp["attendance_device_id"]: {"name": emp["name"], "shift": emp["default_shift"]}
		for emp in employees
	}
	logger.info(f"Loaded {len(employee_map)} employees into cache")

	# Pre-fetch existing checkins for the time range (single query)
	time_values = [record.get("punch_time") for record in filtered_checkin if record.get("punch_time")]
	if time_values:
		min_time = min(time_values)
		max_time = max(time_values)

		existing_checkins = frappe.get_all(
			"Employee Checkin",
			filters={
				"time": ["between", [min_time, max_time]]
			},
			fields=["employee", "time"]
		)

		# Create set of (employee, time) tuples for O(1) duplicate checking
		existing_set = {(ec["employee"], str(ec["time"])) for ec in existing_checkins}
		logger.info(f"Loaded {len(existing_set)} existing checkins for duplicate checking")
	else:
		existing_set = set()

	# Track unmatched employee codes
	unmatched_codes = set()

	# Counters
	created_count = 0
	skipped_count = 0

	# Process in batches
	total_batches = (len(filtered_checkin) + batch_size - 1) // batch_size
	logger.info(f"Processing {len(filtered_checkin)} records in {total_batches} batches")

	for batch_num in range(total_batches):
		start_idx = batch_num * batch_size
		end_idx = min(start_idx + batch_size, len(filtered_checkin))
		batch = filtered_checkin[start_idx:end_idx]

		logger.debug(f"Processing batch {batch_num + 1}/{total_batches} ({len(batch)} records)")

		# Prepare checkin records for this batch
		checkins_to_create = []

		for checkin_record in batch:
			emp_code = checkin_record.get("emp_code")
			punch_time = checkin_record.get("punch_time")
			punch_state = checkin_record.get("punch_state")
			terminal_alias = checkin_record.get("terminal_alias")

			# Validate punch state
			if punch_state not in ["0", "1", "255"]:
				continue

			# Map punch state to log type
			punch_dict = {"0": "IN", "1": "OUT", "255": ""}
			log_type = punch_dict[punch_state]

			# Check if employee exists in cache
			if emp_code not in employee_map:
				if emp_code not in unmatched_codes:
					unmatched_codes.add(emp_code)
					# Log unmatched code (check if already logged)
					if not frappe.db.exists("Bio logs", {"code": emp_code}):
						log_doc = frappe.new_doc("Bio logs")
						log_doc.code = emp_code
						log_doc.log = f"code {emp_code} is not attached to any employee"
						log_doc.insert(ignore_permissions=True)
				continue

			employee_data = employee_map[emp_code]
			employee_name = employee_data["name"]
			default_shift = employee_data["shift"]

			# Check if duplicate
			if (employee_name, punch_time) in existing_set:
				skipped_count += 1
				continue

			# Add to existing set to prevent duplicates within this batch
			existing_set.add((employee_name, punch_time))

			# Prepare checkin record
			checkins_to_create.append({
				"doctype": "Employee Checkin",
				"employee": employee_name,
				"time": punch_time,
				"log_type": log_type,
				"device_id": terminal_alias,
				"shift": default_shift
			})

		# Bulk create checkins for this batch
		if checkins_to_create:
			try:
				for checkin_data in checkins_to_create:
					try:
						doc = frappe.get_doc(checkin_data)
						doc.insert(ignore_permissions=True)
						created_count += 1
					except frappe.DuplicateEntryError:
						skipped_count += 1
					except Exception as e:
						logger.error(f"Error creating checkin for employee {checkin_data['employee']}: {str(e)}")
						skipped_count += 1

				# Commit this batch
				frappe.db.commit()
				logger.info(f"Batch {batch_num + 1}/{total_batches} committed: {len(checkins_to_create)} checkins created")

			except Exception as e:
				logger.error(f"Error processing batch {batch_num + 1}: {str(e)}")
				frappe.db.rollback()

	# Update shift types if enabled
	if settings.update_last_checkin:
		shift_list = frappe.get_all('Shift Type', 'name', {'enable_auto_attendance': '1'}, as_list=True)
		logger.info(f"Updating last sync time for {len(shift_list)} shift types")
		for row in shift_list:
			frappe.set_value('Shift Type', row[0], 'last_sync_of_checkin', now_datetime())
		frappe.db.commit()

	summary = {
		"created": created_count,
		"skipped": skipped_count,
		"unmatched": len(unmatched_codes)
	}

	logger.info(f"Bulk processing complete: {created_count} created, {skipped_count} skipped, {len(unmatched_codes)} unmatched")
	return summary

def add_employee_checkins_legacy(filtered_checkin):
	"""
	Legacy function for processing employee checkins (DEPRECATED).

	This function is kept for backward compatibility only.
	New code should use add_employee_checkins_bulk() instead for better performance.

	Args:
		filtered_checkin: List of checkin records from biometric API
	"""
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
		

