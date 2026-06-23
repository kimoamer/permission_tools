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

import base64
import csv
import io
import os
from uuid import uuid4

import frappe
from frappe import _
from frappe.permissions import add_permission, update_permission_property
from frappe.utils.background_jobs import get_job_status

# Permission flags stored on Custom DocPerm, in CSV column order.
PTYPES = [
    "read", "write", "create", "delete",
    "submit", "cancel", "amend",
    "report", "export", "import", "print",
    "email", "share", "set_user_permissions",
]
CSV_HEADER = ["doctype", "role", "permlevel"] + PTYPES
IMPORT_JOB_CACHE_PREFIX = "permission_tools:import_job:"
IMPORT_JOB_CACHE_TTL = 24 * 60 * 60
IMPORT_JOB_TIMEOUT = 60 * 60
IMPORT_PROGRESS_EVERY = 100
IMPORT_COMMIT_EVERY = 100
MAX_LOG_LINES = 1000
LOG_TRUNCATED_MESSAGE = "... Older log lines truncated; showing the latest entries."


def _guard():
    """Only System Managers may run these tools."""
    frappe.only_for("System Manager")


def _to_bool(value):
    return str(value).strip().lower() in ("1", "true", "yes", "x", "y")


def _estimate_total_rows(csv_content):
    if not csv_content:
        return 0

    return max(len(csv_content.splitlines()) - 1, 0)


def _normalize_csv_text(csv_content):
    csv_content = csv_content or ""
    csv_content = csv_content.lstrip("\ufeff")
    if "\x00" not in csv_content:
        return csv_content

    try:
        return _decode_csv_bytes(csv_content.encode("latin-1"))
    except UnicodeEncodeError:
        return csv_content.replace("\x00", "")


def _decode_csv_bytes(content):
    if not content:
        return ""

    null_ratio = content.count(b"\x00") / max(len(content), 1)
    encodings = ["utf-8-sig"]
    if content.startswith((b"\xff\xfe", b"\xfe\xff")):
        encodings.insert(0, "utf-16")
    if null_ratio > 0.05:
        even_nulls = content[0::2].count(b"\x00")
        odd_nulls = content[1::2].count(b"\x00")
        if odd_nulls >= even_nulls:
            encodings.extend(["utf-16-le", "utf-16-be", "utf-16"])
        else:
            encodings.extend(["utf-16-be", "utf-16-le", "utf-16"])
    encodings.extend(["cp1256", "latin-1"])

    tried = set()
    for encoding in encodings:
        if encoding in tried:
            continue
        tried.add(encoding)
        try:
            decoded = content.decode(encoding)
        except UnicodeError:
            continue
        decoded = decoded.lstrip("\ufeff")
        if "\x00" not in decoded:
            return decoded

    return content.decode("utf-8", errors="replace").replace("\x00", "").lstrip("\ufeff")


def _decode_csv_base64(csv_content_base64):
    try:
        content = base64.b64decode(csv_content_base64 or "", validate=True)
    except Exception:
        frappe.throw(_("Could not decode the uploaded CSV file."))
    return content, _decode_csv_bytes(content)


def _append_log(log, message):
    log.append(message)
    if len(log) > MAX_LOG_LINES:
        del log[: len(log) - MAX_LOG_LINES]
        log[0] = LOG_TRUNCATED_MESSAGE


def _get_supported_ptypes():
    columns = set(frappe.db.get_table_columns("Custom DocPerm"))
    return [ptype for ptype in PTYPES if ptype in columns]


def _get_import_job_key(job_id):
    return f"{IMPORT_JOB_CACHE_PREFIX}{job_id}"


def _job_status_value(status):
    if not status:
        return None
    return getattr(status, "value", None) or str(status)


def _set_import_job_state(job_id, **state):
    key = _get_import_job_key(job_id)
    current = frappe.cache().get_value(key) or {}
    current.update(state)
    current.update({"job_id": job_id, "updated_at": frappe.utils.now()})
    frappe.cache().set_value(key, current, expires_in_sec=IMPORT_JOB_CACHE_TTL)
    return current


def _get_import_upload_dir():
    return frappe.get_site_path("private", "files", "permission_tools")


def _write_import_file(job_id, csv_content):
    folder = _get_import_upload_dir()
    os.makedirs(folder, exist_ok=True)
    file_path = os.path.join(folder, f"{job_id}.csv")
    if isinstance(csv_content, bytes):
        with open(file_path, "wb") as f:
            f.write(csv_content)
    else:
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(_normalize_csv_text(csv_content))
    return file_path


def _cleanup_import_file(file_path):
    if not file_path:
        return

    folder = os.path.abspath(_get_import_upload_dir())
    file_path = os.path.abspath(file_path)
    if file_path.startswith(folder + os.sep) and os.path.exists(file_path):
        os.remove(file_path)


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
        _append_log(log, f"+ Created role: {role_name}")


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
    return _import_permissions(csv_content, create_missing_roles, dry_run)


def _import_permissions(csv_content, create_missing_roles=1, dry_run=0, progress_callback=None):
    """Shared import implementation used by direct calls and background jobs."""
    create_missing_roles = _to_bool(create_missing_roles)
    dry_run = _to_bool(dry_run)
    csv_content = _normalize_csv_text(csv_content)

    reader = csv.DictReader(io.StringIO(csv_content, newline=""))
    log, applied, skipped, processed = [], 0, 0, 0

    if not reader.fieldnames:
        return {"applied": 0, "skipped": 0, "dry_run": dry_run,
                "log": ["No data rows found in CSV."]}

    total = _estimate_total_rows(csv_content)
    supported_ptypes = _get_supported_ptypes()
    unsupported_ptypes = [
        ptype
        for ptype in PTYPES
        if ptype not in supported_ptypes and ptype in reader.fieldnames
    ]
    if unsupported_ptypes and not dry_run:
        _append_log(
            log,
            "! Skipping unsupported permission columns on this Frappe version: "
            + ", ".join(unsupported_ptypes),
        )

    for i, row in enumerate(reader, start=2):
        processed += 1
        doctype = (row.get("doctype") or "").strip()
        role = (row.get("role") or "").strip()
        try:
            permlevel = int((row.get("permlevel") or "0").strip() or 0)
        except ValueError:
            _append_log(log, f"! Line {i}: invalid permlevel '{row.get('permlevel')}' - skipped.")
            skipped += 1
            if progress_callback and processed % IMPORT_PROGRESS_EVERY == 0:
                progress_callback(processed, total, applied, skipped, log)
            continue

        if not doctype or not role:
            _append_log(log, f"! Line {i}: missing doctype or role - skipped.")
            skipped += 1
            if progress_callback and processed % IMPORT_PROGRESS_EVERY == 0:
                progress_callback(processed, total, applied, skipped, log)
            continue
        if not frappe.db.exists("DocType", doctype):
            _append_log(log, f"! Line {i}: DocType '{doctype}' not found - skipped.")
            skipped += 1
            if progress_callback and processed % IMPORT_PROGRESS_EVERY == 0:
                progress_callback(processed, total, applied, skipped, log)
            continue

        if dry_run:
            flags = {p: (1 if _to_bool(row[p]) else 0) for p in PTYPES if row.get(p) not in (None, "")}
            _append_log(log, f"~ Line {i}: would set {doctype} | {role} | L{permlevel} -> {flags}")
            applied += 1
            if progress_callback and processed % IMPORT_PROGRESS_EVERY == 0:
                progress_callback(processed, total, applied, skipped, log)
            continue

        _ensure_role(role, create_missing=create_missing_roles, log=log)
        add_permission(doctype, role, permlevel)

        for ptype in supported_ptypes:
            if row.get(ptype) in (None, ""):
                continue
            value = 1 if _to_bool(row[ptype]) else 0
            update_permission_property(doctype, role, permlevel, ptype, value, validate=False)

        _append_log(log, f"OK Line {i}: {doctype} | {role} | L{permlevel}")
        applied += 1

        if applied % IMPORT_COMMIT_EVERY == 0:
            frappe.db.commit()
            frappe.clear_cache(doctype=doctype)

        if progress_callback and processed % IMPORT_PROGRESS_EVERY == 0:
            progress_callback(processed, total, applied, skipped, log)

    if not processed:
        return {"applied": 0, "skipped": 0, "dry_run": dry_run,
                "log": ["No data rows found in CSV."]}

    if not dry_run:
        frappe.clear_cache()
        frappe.db.commit()

    _append_log(log, "-" * 40)
    _append_log(log, f"Done. Applied: {applied}, Skipped: {skipped}, Dry run: {dry_run}")
    if progress_callback:
        progress_callback(processed, total, applied, skipped, log)
    return {"applied": applied, "skipped": skipped, "dry_run": dry_run, "log": log}


@frappe.whitelist()
def enqueue_import_permissions(csv_content=None, csv_content_base64=None, create_missing_roles=1, dry_run=0):
    """
    Queue a CSV import in the long worker and return a job id for polling.
    """
    _guard()
    raw_content = None
    if csv_content_base64:
        raw_content, csv_content = _decode_csv_base64(csv_content_base64)
    else:
        csv_content = _normalize_csv_text(csv_content)

    if not (csv_content or "").strip():
        return {"status": "finished", "applied": 0, "skipped": 0, "dry_run": _to_bool(dry_run),
                "log": ["No data rows found in CSV."]}

    job_id = f"permission_tools_import_{uuid4().hex}"
    csv_path = _write_import_file(job_id, raw_content if raw_content is not None else csv_content)
    _set_import_job_state(
        job_id,
        status="queued",
        processed=0,
        total=_estimate_total_rows(csv_content),
        applied=0,
        skipped=0,
        dry_run=_to_bool(dry_run),
        log=["Import queued."],
    )

    try:
        job = frappe.enqueue(
            "permission_tools.api.run_import_permissions_job",
            queue="long",
            timeout=IMPORT_JOB_TIMEOUT,
            job_id=job_id,
            import_job_id=job_id,
            csv_path=csv_path,
            create_missing_roles=create_missing_roles,
            dry_run=dry_run,
        )
    except Exception:
        _cleanup_import_file(csv_path)
        _set_import_job_state(
            job_id,
            status="failed",
            error=frappe.get_traceback(),
            log=["Failed to queue import job.", frappe.get_traceback()],
        )
        raise

    return {"status": "queued", "job_id": job_id, "rq_job_id": job.id}


def run_import_permissions_job(import_job_id, csv_path, create_missing_roles=1, dry_run=0):
    """
    Background worker entry point. Do not call directly from the browser.
    """
    _guard()
    _set_import_job_state(import_job_id, status="started", log=["Import started."])

    def update_progress(processed, total, applied, skipped, log):
        _set_import_job_state(
            import_job_id,
            status="started",
            processed=processed,
            total=total,
            applied=applied,
            skipped=skipped,
            log=log[-MAX_LOG_LINES:],
        )

    try:
        with open(csv_path, "rb") as f:
            csv_content = _decode_csv_bytes(f.read())
        result = _import_permissions(csv_content, create_missing_roles, dry_run, update_progress)
        _set_import_job_state(
            import_job_id,
            status="finished",
            processed=result.get("applied", 0) + result.get("skipped", 0),
            applied=result.get("applied", 0),
            skipped=result.get("skipped", 0),
            dry_run=result.get("dry_run"),
            result=result,
            log=result.get("log", []),
        )
        return result
    except Exception as exc:
        error = frappe.get_traceback()
        _set_import_job_state(
            import_job_id,
            status="failed",
            error=str(exc),
            traceback=error,
            log=["Import failed.", error],
        )
        raise
    finally:
        _cleanup_import_file(csv_path)


@frappe.whitelist()
def get_import_job_status(job_id):
    """
    Return cached import job progress and the current RQ status when available.
    """
    _guard()
    if not job_id:
        frappe.throw(_("Job ID is required."))

    state = frappe.cache().get_value(_get_import_job_key(job_id))
    rq_status = _job_status_value(get_job_status(job_id))
    if not state:
        return {"job_id": job_id, "status": "missing", "rq_status": rq_status,
                "log": ["Import job was not found or its result expired."]}

    state["rq_status"] = rq_status
    return state


def run_from_file(csv_path, create_missing_roles=1, dry_run=0):
    """
    Bench entry point:
        bench --site SITE execute permission_tools.api.run_from_file \\
            --kwargs "{'csv_path': '/path/to/file.csv'}"
    """
    with open(csv_path, "rb") as f:
        content = _decode_csv_bytes(f.read())
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
