import frappe
from frappe.model.document import Document
from frappe.utils import getdate, now_datetime

logger = frappe.logger("biometric_integration", allow_site=True, file_count=5)


class ClearCheckin(Document):
	"""DocType controller for clearing employee checkins with audit trail"""

	def validate(self):
		"""Validate date range"""
		if self.start_date and self.end_date:
			if getdate(self.start_date) > getdate(self.end_date):
				frappe.throw("Start Date cannot be after End Date")


@frappe.whitelist()
def preview_checkins_to_clear(start_date, end_date, employee=None):
	"""
	Preview checkins that will be cleared for the given date range.
	Only shows checkins where attendance is already marked as Present.

	Args:
		start_date: Start date for the range
		end_date: End date for the range
		employee: Optional employee filter

	Returns:
		dict: Preview data with checkins and attendance info
	"""
	filters = {
		"time": ["between", [f"{start_date} 00:00:00", f"{end_date} 23:59:59"]]
	}

	if employee:
		filters["employee"] = employee

	# Get all checkins in the date range
	checkins = frappe.get_all(
		"Employee Checkin",
		filters=filters,
		fields=["name", "employee", "employee_name", "time", "log_type", "device_id"],
		order_by="time asc"
	)

	if not checkins:
		return {
			"checkins": [],
			"total_checkins": 0,
			"attendances": [],
			"total_attendances": 0,
			"can_clear": False,
			"message": "No checkins found in the specified date range"
		}

	# Get unique employee-date combinations
	employee_dates = set()
	for checkin in checkins:
		emp = checkin["employee"]
		date = getdate(checkin["time"])
		employee_dates.add((emp, date))

	# Check which dates have Present attendance
	attendances_present = []
	for emp, date in employee_dates:
		attendance = frappe.get_all(
			"Attendance",
			filters={
				"employee": emp,
				"attendance_date": date,
				"status": "Present"
			},
			fields=["name", "employee", "employee_name", "attendance_date", "status"],
			limit=1
		)

		if attendance:
			attendances_present.extend(attendance)

	# Build preview HTML
	preview_html = f"""
	<div class="frappe-control">
		<div class="alert alert-info">
			<strong>Preview:</strong> Found {len(checkins)} checkin(s) in the selected date range
		</div>
	"""

	if attendances_present:
		preview_html += f"""
		<div class="alert alert-warning">
			<strong>Attendance Impact:</strong> {len(attendances_present)} attendance record(s) marked as Present will be affected
		</div>
		<table class="table table-bordered table-sm">
			<thead>
				<tr>
					<th>Employee</th>
					<th>Date</th>
					<th>Status</th>
				</tr>
			</thead>
			<tbody>
		"""

		for att in attendances_present[:10]:  # Show first 10
			preview_html += f"""
				<tr>
					<td>{att['employee_name']} ({att['employee']})</td>
					<td>{att['attendance_date']}</td>
					<td><span class="indicator-pill green">{att['status']}</span></td>
				</tr>
			"""

		if len(attendances_present) > 10:
			preview_html += f"""
				<tr>
					<td colspan="3" class="text-center">
						<em>... and {len(attendances_present) - 10} more attendance record(s)</em>
					</td>
				</tr>
			"""

		preview_html += """
			</tbody>
		</table>
		"""
	else:
		preview_html += """
		<div class="alert alert-success">
			<strong>Note:</strong> No Present attendance records found for these dates
		</div>
		"""

	# Show sample checkins
	preview_html += """
	<h6 class="mt-3">Sample Checkins (first 10):</h6>
	<table class="table table-bordered table-sm">
		<thead>
			<tr>
				<th>Employee</th>
				<th>Time</th>
				<th>Type</th>
				<th>Device</th>
			</tr>
		</thead>
		<tbody>
	"""

	for checkin in checkins[:10]:
		log_type_color = "blue" if checkin['log_type'] == "IN" else "orange"
		preview_html += f"""
			<tr>
				<td>{checkin['employee_name']} ({checkin['employee']})</td>
				<td>{checkin['time']}</td>
				<td><span class="indicator-pill {log_type_color}">{checkin['log_type']}</span></td>
				<td>{checkin.get('device_id', '')}</td>
			</tr>
		"""

	if len(checkins) > 10:
		preview_html += f"""
			<tr>
				<td colspan="4" class="text-center">
					<em>... and {len(checkins) - 10} more checkin(s)</em>
				</td>
			</tr>
		"""

	preview_html += """
		</tbody>
	</table>
	</div>
	"""

	return {
		"checkins": checkins,
		"total_checkins": len(checkins),
		"attendances": attendances_present,
		"total_attendances": len(attendances_present),
		"can_clear": len(attendances_present) > 0,
		"preview_html": preview_html
	}


@frappe.whitelist()
def clear_checkins(start_date, end_date, employee=None):
	"""
	Clear employee checkins for the given date range.
	Only clears if attendance is marked as Present.
	Logs all actions as comments on the Clear Checkin document.

	Args:
		start_date: Start date for the range
		end_date: End date for the range
		employee: Optional employee filter

	Returns:
		dict: Summary of cleared records
	"""
	# Get the single document
	doc = frappe.get_doc("Clear Checkin")

	# First preview to get the data
	preview = preview_checkins_to_clear(start_date, end_date, employee)

	if not preview["can_clear"]:
		frappe.throw(
			"No checkins found with Present attendance in the specified date range. "
			"Checkins can only be cleared if attendance is already marked as Present."
		)

	checkins = preview["checkins"]
	attendances = preview["attendances"]

	# Add initial comment with operation details
	doc.add_comment(
		"Comment",
		f"<b>Clear Operation Started</b><br>"
		f"User: {frappe.session.user}<br>"
		f"Date Range: {start_date} to {end_date}<br>"
		f"Employee Filter: {employee or 'All Employees'}<br>"
		f"Checkins to clear: {len(checkins)}<br>"
		f"Attendances affected: {len(attendances)}"
	)

	deleted_count = 0
	failed_count = 0
	failed_checkins = []

	# Delete checkins
	for checkin in checkins:
		try:
			frappe.delete_doc("Employee Checkin", checkin["name"], force=1)
			deleted_count += 1
		except Exception as e:
			logger.error(f"Failed to delete checkin {checkin['name']}: {str(e)}")
			failed_count += 1
			failed_checkins.append({
				"name": checkin["name"],
				"employee": checkin["employee"],
				"time": str(checkin["time"]),
				"error": str(e)
			})

	# Commit changes
	frappe.db.commit()

	# Add detailed log as comment
	log_comment = f"<b>Clear Operation Completed</b><br>"
	log_comment += f"Checkins cleared: {deleted_count}<br>"
	log_comment += f"Failed: {failed_count}<br>"
	log_comment += f"Attendances affected: {len(attendances)}<br><br>"

	# Add sample cleared checkins (first 20)
	if deleted_count > 0:
		log_comment += "<b>Sample Cleared Checkins (first 20):</b><br>"
		log_comment += "<table class='table table-bordered table-sm' style='font-size: 11px;'>"
		log_comment += "<tr><th>Employee</th><th>Time</th><th>Type</th></tr>"
		for i, checkin in enumerate(checkins[:20]):
			if checkin["name"] not in [f["name"] for f in failed_checkins]:
				log_comment += f"<tr><td>{checkin['employee_name']} ({checkin['employee']})</td>"
				log_comment += f"<td>{checkin['time']}</td>"
				log_comment += f"<td>{checkin['log_type']}</td></tr>"
		if deleted_count > 20:
			log_comment += f"<tr><td colspan='3'><i>... and {deleted_count - 20} more</i></td></tr>"
		log_comment += "</table><br>"

	# Add affected attendances (first 20)
	if len(attendances) > 0:
		log_comment += "<b>Affected Attendances (first 20):</b><br>"
		log_comment += "<table class='table table-bordered table-sm' style='font-size: 11px;'>"
		log_comment += "<tr><th>Employee</th><th>Date</th><th>Status</th></tr>"
		for i, att in enumerate(attendances[:20]):
			log_comment += f"<tr><td>{att['employee_name']} ({att['employee']})</td>"
			log_comment += f"<td>{att['attendance_date']}</td>"
			log_comment += f"<td><span class='indicator-pill green'>{att['status']}</span></td></tr>"
		if len(attendances) > 20:
			log_comment += f"<tr><td colspan='3'><i>... and {len(attendances) - 20} more</i></td></tr>"
		log_comment += "</table><br>"

	# Add failed checkins if any
	if failed_count > 0:
		log_comment += "<b>Failed Checkins:</b><br>"
		log_comment += "<table class='table table-bordered table-sm' style='font-size: 11px;'>"
		log_comment += "<tr><th>ID</th><th>Employee</th><th>Time</th><th>Error</th></tr>"
		for fc in failed_checkins:
			log_comment += f"<tr><td>{fc['name']}</td><td>{fc['employee']}</td>"
			log_comment += f"<td>{fc['time']}</td><td>{fc['error']}</td></tr>"
		log_comment += "</table>"

	doc.add_comment("Comment", log_comment)

	# Update document fields
	doc.checkins_cleared = deleted_count
	doc.attendances_affected = len(attendances)
	doc.save(ignore_permissions=True)
	frappe.db.commit()

	logger.info(
		f"Cleared {deleted_count} checkins ({failed_count} failed) "
		f"for date range {start_date} to {end_date}"
	)

	return {
		"deleted": deleted_count,
		"failed": failed_count,
		"attendances_affected": len(attendances),
		"message": f"Successfully cleared {deleted_count} checkin(s). {failed_count} failed."
	}
