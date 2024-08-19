// Copyright (c) 2024, Riane and contributors
// For license information, please see license.txt

frappe.ui.form.on('Pull Checkin', {
	refresh: function(frm) {
		frm.toggle_enable("pull_checkin", frm.doc.__unsaved != 1)
	},
	pull_checkin: function(frm){
		var filters = {
			"start_time": frm.doc.start_time,
			"to_time": frm.doc.to_time,
			"attendance_device_id": frm.doc.attendance_device_id
		}
		frm.call({
			method: "bio_integ.api.pull_filtered_checkin",
			args:{
				filters: filters
			},
			freeze: true,
			callback: function(r){
				console.log(r)
			}
		})
	}
});
