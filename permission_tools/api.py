"""
Permission Tools - Core API
===========================
Safe, additive import & export of role permissions for Frappe / ERPNext.

- IMPORT applies permissions using Frappe's official permission API
  (add_permission / update_permission_property). It only touches the
  (doctype, role, permlevel) rows you provide, so it NEVER resets the
  permissions of other roles or levels the way a raw Custom DocPerm
  Data Import does. It is idempotent.

- EXPORT pulls existing Custom DocPerm rows (i.e. everything configured
  through the Role Permissions Manager) into the same CSV format, so you
  get a clean Dev -> UAT -> Prod round-trip.

All functions are restricted to System Manager.
"""

import csv
import io
import frappe
from frappe import _
from frappe.permissions import add_permission, update_permission_property

# Permission flags stored on Custom DocPerm, in CSV column order.
PTYPES = [
    "read", "write", "create", "delete",
    "submit", "cancel", "amend",
    "report", "export", "import", "print",
    "email", "share", "set_user_permissions",
]
CSV_HEADER = ["doctype", "role", "permlevel"] + PTYPES


def _guard():
    """Only System Managers may run these tools."""
    frappe.only_for("System Manager")


def _to_bool(value):
    return str(value).strip().lower() in ("1", "true", "yes", "x", "y")


def _get_supported_ptypes():
    columns = set(frappe.db.get_table_columns("Custom DocPerm"))
    return [ptype for ptype in PTYPES if ptype in columns]


def _ensure_role(role_name, create_missing=True, log=None):
    if frappe.db.exists("Role", role_name):
        return
    if not create_missing:
        raise frappe.ValidationError(_("Role '{0}' does not exist.").format(role_name))
    role = frappe.new_doc("Role")
    role.role_name = role_name
    role.desk_access = 1
    role.insert(ignore_permissions=True)
    if log is not None:
        log.append(f"+ Created role: {role_name}")


# --------------------------------------------------------------------------- #
# IMPORT
# --------------------------------------------------------------------------- #
@frappe.whitelist()
def import_permissions(csv_content, create_missing_roles=1, dry_run=0):
    """
    Import role permissions from raw CSV text (used by the UI).

    csv_content         : the CSV file content as a string.
    create_missing_roles: create roles that don't exist yet (default yes).
    dry_run             : if truthy, report changes without writing.

    Returns a dict: {applied, skipped, dry_run, log: [..]}.
    """
    _guard()
    create_missing_roles = _to_bool(create_missing_roles)
    dry_run = _to_bool(dry_run)
    csv_content = (csv_content or "").lstrip("\ufeff")

    reader = csv.DictReader(io.StringIO(csv_content))
    rows = list(reader)
    log, applied, skipped = [], 0, 0

    if not rows:
        return {"applied": 0, "skipped": 0, "dry_run": dry_run,
                "log": ["No data rows found in CSV."]}

    supported_ptypes = _get_supported_ptypes()
    unsupported_ptypes = [
        ptype
        for ptype in PTYPES
        if ptype not in supported_ptypes and any(row.get(ptype) not in (None, "") for row in rows)
    ]
    if unsupported_ptypes and not dry_run:
        log.append(
            "! Skipping unsupported permission columns on this Frappe version: "
            + ", ".join(unsupported_ptypes)
        )

    for i, row in enumerate(rows, start=2):
        doctype = (row.get("doctype") or "").strip()
        role = (row.get("role") or "").strip()
        permlevel = int((row.get("permlevel") or "0").strip() or 0)

        if not doctype or not role:
            log.append(f"! Line {i}: missing doctype or role - skipped.")
            skipped += 1
            continue
        if not frappe.db.exists("DocType", doctype):
            log.append(f"! Line {i}: DocType '{doctype}' not found - skipped.")
            skipped += 1
            continue

        if dry_run:
            flags = {p: (1 if _to_bool(row[p]) else 0) for p in PTYPES if row.get(p) not in (None, "")}
            log.append(f"~ Line {i}: would set {doctype} | {role} | L{permlevel} -> {flags}")
            applied += 1
            continue

        _ensure_role(role, create_missing=create_missing_roles, log=log)
        add_permission(doctype, role, permlevel)

        for ptype in supported_ptypes:
            if row.get(ptype) in (None, ""):
                continue
            value = 1 if _to_bool(row[ptype]) else 0
            update_permission_property(doctype, role, permlevel, ptype, value, validate=False)

        log.append(f"OK Line {i}: {doctype} | {role} | L{permlevel}")
        applied += 1

    if not dry_run:
        frappe.clear_cache()
        frappe.db.commit()

    log.append("-" * 40)
    log.append(f"Done. Applied: {applied}, Skipped: {skipped}, Dry run: {dry_run}")
    return {"applied": applied, "skipped": skipped, "dry_run": dry_run, "log": log}


def run_from_file(csv_path, create_missing_roles=1, dry_run=0):
    """
    Bench entry point:
        bench --site SITE execute permission_tools.api.run_from_file \\
            --kwargs "{'csv_path': '/path/to/file.csv'}"
    """
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        content = f.read()
    result = import_permissions(content, create_missing_roles, dry_run)
    print("\n".join(result["log"]))
    return result


# --------------------------------------------------------------------------- #
# EXPORT
# --------------------------------------------------------------------------- #
@frappe.whitelist()
def export_permissions(roles=None, doctypes=None):
    """
    Export existing Custom DocPerm rows to CSV text.

    roles    : optional comma-separated string or list to filter by role.
    doctypes : optional comma-separated string or list to filter by doctype.

    Returns a dict: {csv, count}. The CSV uses the same columns the
    importer expects, so export -> import round-trips cleanly.
    """
    _guard()

    def _as_list(v):
        if not v:
            return None
        if isinstance(v, str):
            v = v.split(",")
        return [x.strip() for x in v if x and x.strip()]

    role_filter = _as_list(roles)
    doctype_filter = _as_list(doctypes)

    filters = {}
    if role_filter:
        filters["role"] = ["in", role_filter]
    if doctype_filter:
        filters["parent"] = ["in", doctype_filter]

    supported_ptypes = _get_supported_ptypes()
    fields = ["parent as doctype", "role", "permlevel"] + supported_ptypes
    perms = frappe.get_all(
        "Custom DocPerm",
        filters=filters,
        fields=fields,
        order_by="parent asc, role asc, permlevel asc",
    )

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(CSV_HEADER)
    for p in perms:
        writer.writerow([p.get("doctype"), p.get("role"), p.get("permlevel")]
                        + [int(p.get(pt) or 0) for pt in PTYPES])

    return {"csv": buf.getvalue(), "count": len(perms)}


@frappe.whitelist()
def list_roles():
    """Helper for the UI role picker."""
    _guard()
    return [r.name for r in frappe.get_all("Role", order_by="name asc", fields=["name"])]
