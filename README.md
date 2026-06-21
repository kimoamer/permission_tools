# Permission Tools

A Frappe/ERPNext app to **safely import and export role permissions** — without the reset problem of raw `Custom DocPerm` Data Import.

## Why
Importing the `Custom DocPerm` doctype through the standard Data Import tool **resets** existing permissions. This app applies permissions **additively** using Frappe's official permission API, so only the rows you provide are changed. It's idempotent.

## Install
```bash
bench get-app permission_tools /path/to/permission_tools
bench --site yoursite.local install-app permission_tools
bench --site yoursite.local clear-cache && bench build
```

## Use the UI
Go to: **Awesomebar → "Permission Tools Manager"** (or `/app/permission-tools-manager`). System Manager only.
- **Import:** upload CSV, optionally tick *Dry run* to preview, then *Run Import*.
- **Export:** pick roles/doctypes (or leave blank for all) → *Export to CSV*.
- **Template:** *Download CSV template* button.

## Use from bench
```bash
bench --site yoursite.local execute permission_tools.api.run_from_file \
  --kwargs "{'csv_path': '/path/to/role_permissions.csv', 'dry_run': 1}"
```

## CSV format
`doctype, role, permlevel, read, write, create, delete, submit, cancel, amend, report, export, import, print, email, share, set_user_permissions`
Use `1`/`0` for each flag. One row per (doctype, role, permlevel).
