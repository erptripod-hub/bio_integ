// Copyright (c) 2025, Riane and contributors
// For license information, please see license.txt

frappe.ui.form.on('Clear Checkin', {
	refresh: function(frm) {
		// Make button primary
		frm.fields_dict.clear_checkins.$input.addClass('btn-primary');

		// Show comment section prominently
		if (frm.doc.checkins_cleared > 0) {
			frm.set_df_property('section_break_6', 'hidden', 0);
		}
	},

	clear_checkins: function(frm) {
		// Validate required fields
		if (!frm.doc.start_date || !frm.doc.end_date) {
			frappe.msgprint(__('Please select Start Date and End Date'));
			return;
		}

		proceed_with_preview(frm);
	}
});

function proceed_with_preview(frm) {
	// Show loading
	frappe.dom.freeze(__('Loading preview...'));

	// Call preview method
	frappe.call({
		method: 'bio_integ.biometric_integration.doctype.clear_checkin.clear_checkin.preview_checkins_to_clear',
		args: {
			start_date: frm.doc.start_date,
			end_date: frm.doc.end_date,
			employee: frm.doc.employee
		},
		callback: function(r) {
			frappe.dom.unfreeze();

			if (r.message) {
				const data = r.message;

				// Update preview HTML
				frm.set_df_property('preview_html', 'options', data.preview_html);
				frm.refresh_field('preview_html');

				// If no checkins with Present attendance found
				if (!data.can_clear) {
					frappe.msgprint({
						title: __('No Checkins to Clear'),
						message: data.message,
						indicator: 'yellow'
					});
					return;
				}

				// Show confirmation dialog
				frappe.confirm(
					__(`You are about to clear <b>${data.total_checkins}</b> checkin(s) ` +
					   `affecting <b>${data.total_attendances}</b> attendance record(s) marked as Present. ` +
					   `This action will be logged as comments for audit purposes.<br><br>` +
					   `<b>Are you sure you want to proceed?</b>`),
					function() {
						// User confirmed - proceed with clearing
						perform_clear(frm, data);
					},
					function() {
						// User cancelled
						frappe.msgprint(__('Operation cancelled'));
					}
				);
			}
		},
		error: function(r) {
			frappe.dom.unfreeze();
			frappe.msgprint({
				title: __('Error'),
				message: __('Failed to load preview'),
				indicator: 'red'
			});
		}
	});
}

function perform_clear(frm, preview_data) {
	frappe.dom.freeze(__('Clearing checkins...'));

	frappe.call({
		method: 'bio_integ.biometric_integration.doctype.clear_checkin.clear_checkin.clear_checkins',
		args: {
			start_date: frm.doc.start_date,
			end_date: frm.doc.end_date,
			employee: frm.doc.employee
		},
		callback: function(r) {
			frappe.dom.unfreeze();

			if (r.message) {
				const result = r.message;

				// Reload form to show comments and updated fields
				frm.reload_doc();

				// Show success message
				frappe.msgprint({
					title: __('Checkins Cleared'),
					message: result.message + '<br><br>Check the Comments section below for detailed audit log.',
					indicator: 'green'
				});

				// Clear preview
				frm.set_df_property('preview_html', 'options',
					'<div class="alert alert-success">Checkins cleared successfully. Check the Comments section for detailed audit log.</div>');
				frm.refresh_field('preview_html');
			}
		},
		error: function(r) {
			frappe.dom.unfreeze();
			frappe.msgprint({
				title: __('Error'),
				message: __('Failed to clear checkins. Please check the error log.'),
				indicator: 'red'
			});
		}
	});
}
