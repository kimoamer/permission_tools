frappe.pages['permission-tools-manager'].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: 'Permission Tools Manager',
		single_column: true,
	});

	const $body = $(wrapper).find('.layout-main-section');
	$body.html(`
		<div class="pt-wrap" style="max-width:820px;">
			<!-- IMPORT -->
			<div class="pt-card" style="border:1px solid var(--border-color);border-radius:8px;padding:16px;margin-bottom:20px;">
				<h4 style="margin-top:0;">Import Permissions</h4>
				<p class="text-muted" style="margin-bottom:12px;">
					Upload a CSV (doctype, role, permlevel, then flags). Permissions are applied
					<b>additively</b> &mdash; only the rows you list change. Nothing else is reset.
				</p>
				<input type="file" id="pt-file" accept=".csv" class="form-control" style="margin-bottom:12px;max-width:360px;"/>
				<div style="margin-bottom:12px;">
					<label style="margin-right:18px;"><input type="checkbox" id="pt-create-roles" checked/> Create missing roles</label>
					<label><input type="checkbox" id="pt-dry-run"/> Dry run (preview only)</label>
				</div>
				<button class="btn btn-primary btn-sm" id="pt-import-btn">Run Import</button>
				<a href="#" id="pt-template" class="btn btn-default btn-sm" style="margin-left:8px;">Download CSV template</a>
			</div>

			<!-- EXPORT -->
			<div class="pt-card" style="border:1px solid var(--border-color);border-radius:8px;padding:16px;margin-bottom:20px;">
				<h4 style="margin-top:0;">Export Permissions</h4>
				<p class="text-muted" style="margin-bottom:12px;">
					Export current custom role permissions to CSV. Leave filters blank to export everything.
				</p>
				<div id="pt-role-field" style="max-width:360px;margin-bottom:8px;"></div>
				<div id="pt-doctype-field" style="max-width:360px;margin-bottom:12px;"></div>
				<button class="btn btn-primary btn-sm" id="pt-export-btn">Export to CSV</button>
			</div>

			<!-- LOG -->
			<div class="pt-card" style="border:1px solid var(--border-color);border-radius:8px;padding:16px;">
				<h4 style="margin-top:0;">Log</h4>
				<pre id="pt-log" style="max-height:300px;overflow:auto;background:var(--control-bg);padding:12px;border-radius:6px;margin:0;">Ready.</pre>
			</div>
		</div>
	`);

	const logEl = $body.find('#pt-log');
	const setLog = (lines) => logEl.text(Array.isArray(lines) ? lines.join('\n') : lines);
	let importPollTimer = null;

	const setImportBusy = (busy) => {
		$body.find('#pt-import-btn')
			.prop('disabled', busy)
			.text(busy ? __('Import Running...') : __('Run Import'));
	};

	const stopImportPolling = () => {
		if (importPollTimer) {
			clearTimeout(importPollTimer);
			importPollTimer = null;
		}
	};

	const arrayBufferToBase64 = (buffer) => {
		const bytes = new Uint8Array(buffer);
		const chunkSize = 0x8000;
		let binary = '';
		for (let i = 0; i < bytes.length; i += chunkSize) {
			const chunk = bytes.subarray(i, i + chunkSize);
			binary += String.fromCharCode.apply(null, chunk);
		}
		return btoa(binary);
	};

	const renderImportStatus = (job) => {
		const lines = [];
		const status = job.status || job.rq_status || 'unknown';
		lines.push(__('Status: {0}', [status]));
		if (job.total) {
			lines.push(__('Processed {0} of {1} rows. Applied {2}, skipped {3}.', [
				job.processed || 0, job.total, job.applied || 0, job.skipped || 0,
			]));
		} else if (job.processed) {
			lines.push(__('Processed {0} rows. Applied {1}, skipped {2}.', [
				job.processed, job.applied || 0, job.skipped || 0,
			]));
		}
		if (job.job_id) {
			lines.push(__('Job ID: {0}', [job.job_id]));
		}
		if (job.error) {
			lines.push('');
			lines.push(job.error);
		}
		if (job.log && job.log.length) {
			lines.push('');
			lines.push(...job.log);
		}
		setLog(lines);
	};

	const pollImportJob = (jobId) => {
		frappe.call({
			method: 'permission_tools.api.get_import_job_status',
			args: { job_id: jobId },
			callback: (r) => {
				const job = r.message || {};
				renderImportStatus(job);

				if (['finished', 'failed', 'missing'].includes(job.status)) {
					stopImportPolling();
					setImportBusy(false);
					if (job.status === 'finished') {
						frappe.show_alert({
							message: __('Import finished. Applied {0}, skipped {1}', [
								job.applied || 0, job.skipped || 0,
							]),
							indicator: job.skipped ? 'orange' : 'green',
						});
					} else {
						frappe.show_alert({ message: __('Import did not finish.'), indicator: 'red' });
					}
					return;
				}

				importPollTimer = setTimeout(() => pollImportJob(jobId), 2000);
			},
			error: () => {
				stopImportPolling();
				setImportBusy(false);
				setLog(__('Could not read import job status.'));
			},
		});
	};

	// ---- Export filter controls (multiselect role + link doctype) ----
	const roleControl = frappe.ui.form.make_control({
		df: { fieldtype: 'MultiSelectList', label: 'Roles (optional)', fieldname: 'pt_roles',
			get_data: () => frappe.db.get_link_options('Role') },
		parent: $body.find('#pt-role-field'), render_input: true,
	});
	const doctypeControl = frappe.ui.form.make_control({
		df: { fieldtype: 'MultiSelectList', label: 'Doctypes (optional)', fieldname: 'pt_doctypes',
			get_data: () => frappe.db.get_link_options('DocType') },
		parent: $body.find('#pt-doctype-field'), render_input: true,
	});

	// ---- Import ----
	$body.find('#pt-import-btn').on('click', () => {
		const file = $body.find('#pt-file')[0].files[0];
		if (!file) { frappe.msgprint(__('Please choose a CSV file first.')); return; }
		stopImportPolling();
		setImportBusy(true);
		setLog(__('Reading CSV...'));
		const reader = new FileReader();
		reader.onload = (e) => {
			frappe.call({
				method: 'permission_tools.api.enqueue_import_permissions',
				args: {
					csv_content_base64: arrayBufferToBase64(e.target.result),
					create_missing_roles: $body.find('#pt-create-roles').is(':checked') ? 1 : 0,
					dry_run: $body.find('#pt-dry-run').is(':checked') ? 1 : 0,
				},
				freeze: true, freeze_message: __('Queueing import...'),
				callback: (r) => {
					if (r.message) {
						if (r.message.status === 'finished') {
							setImportBusy(false);
							setLog(r.message.log);
							return;
						}
						setLog([__('Import queued.'), __('Job ID: {0}', [r.message.job_id])]);
						pollImportJob(r.message.job_id);
					}
				},
				error: () => {
					setImportBusy(false);
					setLog(__('Could not queue import job.'));
				},
			});
		};
		reader.onerror = () => {
			setImportBusy(false);
			setLog(__('Could not read the selected CSV file.'));
		};
		reader.readAsArrayBuffer(file);
	});

	// ---- Export ----
	$body.find('#pt-export-btn').on('click', () => {
		frappe.call({
			method: 'permission_tools.api.export_permissions',
			args: {
				roles: (roleControl.get_value() || []).join(','),
				doctypes: (doctypeControl.get_value() || []).join(','),
			},
			freeze: true, freeze_message: __('Exporting...'),
			callback: (r) => {
				if (r.message) {
					download_csv(r.message.csv, 'role_permissions_export.csv');
					setLog(__('Exported {0} permission rows.', [r.message.count]));
					frappe.show_alert({ message: __('Exported {0} rows', [r.message.count]), indicator: 'green' });
				}
			},
		});
	});

	// ---- CSV template ----
	$body.find('#pt-template').on('click', (e) => {
		e.preventDefault();
		const header = 'doctype,role,permlevel,read,write,create,delete,submit,cancel,amend,report,export,import,print,email,share,set_user_permissions';
		const sample = 'Sales Invoice,Custom Sales Role,0,1,1,1,0,1,0,0,1,1,0,1,1,1,0';
		download_csv(header + '\n' + sample + '\n', 'role_permissions_template.csv');
	});

	function download_csv(content, filename) {
		const blob = new Blob([content], { type: 'text/csv;charset=utf-8;' });
		const url = URL.createObjectURL(blob);
		const a = document.createElement('a');
		a.href = url; a.download = filename;
		document.body.appendChild(a); a.click();
		document.body.removeChild(a); URL.revokeObjectURL(url);
	}
};
