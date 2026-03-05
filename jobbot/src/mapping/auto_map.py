"""Auto-map form fields to answers using the mapping DB + profile.yaml fallback.

Resolution hierarchy:
  1. DB mapping: schema_hash → company → global
  2. Profile.yaml direct keys (first_name, last_name, email, phone, linkedin, github, portfolio, location)
  3. Built-in heuristic answers (work authorization, sponsorship, etc.)
  4. Context inheritance: unknown_* fields inherit the answer from the preceding labeled field
"""
from __future__ import annotations

import re
from typing import Optional

from src.storage.db import get_connection, resolve_field_value
from src.utils.logging import setup_logging

logger = setup_logging("jobbot.mapping.auto_map")

# Profile.yaml key → common form field names (and prefixes)
_PROFILE_FIELD_MAP = {
    "first_name":          ["first_name", "firstname", "applicant[first_name]", "given_name"],
    "last_name":           ["last_name", "lastname", "applicant[last_name]", "family_name", "surname"],
    "email":               ["email", "applicant[email]", "email_address"],
    "phone":               ["phone", "phone_number", "applicant[phone]", "telephone", "mobile"],
    "linkedin":            ["linkedin", "linkedin_url", "linkedin_profile", "linkedin_profile_url",
                            "question_60609512", "linkedin_profile_url"],
    "github":              ["github", "github_url", "github_profile", "github_username",
                            "question_60609513"],
    "portfolio":           ["portfolio", "website", "personal_website", "portfolio_url", "website_url"],
    "location":            ["location", "city", "address", "current_location", "current_city",
                            "candidate_location"],
    "country":             ["country", "country_of_residence", "country_of_citizenship"],
    "university":          ["university", "school", "college", "institution", "university_name",
                            "school_name", "school_0"],
    "degree":              ["degree", "degree_type", "degree_0"],
    "major":               ["major", "field_of_study", "concentration", "program", "discipline",
                            "discipline_0"],
    "graduation_date":     ["graduation_date", "expected_graduation"],
    "graduation_year":     ["graduation_year", "expected_graduation_year", "class_year",
                            "end_year_0", "graduation_year_0"],
    "gpa":                 ["gpa", "grade_point_average", "cumulative_gpa", "gpa_0"],
    "work_authorization":  ["work_authorization", "authorized_to_work", "work_auth"],
    "requires_sponsorship":["requires_sponsorship", "need_sponsorship", "visa_sponsorship"],
    "available_start":     ["available_start", "start_date", "available_start_date"],
    "years_of_experience": ["years_of_experience", "years_experience"],
    "referral_source":     ["referral_source", "how_did_you_hear", "source"],
}

# Field key prefix patterns that map to profile keys (handles _0, _1 indexed fields)
_PREFIX_PROFILE_MAP = {
    "school":      "university",
    "degree":      "degree",
    "discipline":  "major",
    "gpa":         "gpa",
}

# Built-in heuristic answers for common custom questions (keyword → answer)
_HEURISTIC_ANSWERS = {
    # Work authorization
    "authorized to work":          "Yes",
    "legally authorized":          "Yes",
    "legally eligible":            "Yes",
    "work authorization":          "Yes",
    "right to work":               "Yes",
    "eligible to work":            "Yes",
    "permission to work":          "No",    # EU-specific questions → No (not in EU)
    "located in the european":     "No",
    "located in europe":           "No",
    "currently located in the eu": "No",
    # Sponsorship
    "visa sponsorship":            "Yes",
    "require sponsorship":         "Yes",
    "immigration sponsorship":     "Yes",
    "need sponsorship":            "Yes",
    "will you require":            "Yes",
    "now or in the future require": "Yes",
    "sponsorship for employment":  "Yes",
    # Relocation
    "willing to relocate":         "Yes",
    "open to relocation":          "Yes",
    "relocate":                    "Yes",
    # Timing
    "start date":                  "ASAP",
    "earliest start":              "ASAP",
    "available to start":          "Immediately",
    # Referral
    "how did you hear":            "Online job board",
    "how did you find":            "Online job board",
    "where did you hear":          "Online job board",
    "referral source":             "Online job board",
    "how did you learn":           "Online job board",
    # EEO / demographics
    "gender":                      "Prefer not to say",
    "race":                        "Prefer not to say",
    "ethnicity":                   "Prefer not to say",
    "veteran":                     "Prefer not to say",
    "disability":                  "Prefer not to say",
    "sexual orientation":          "Prefer not to say",
    "transgender":                 "Prefer not to say",
    # Education
    "grading scale":               "4.0",
    "gpa scale":                   "4.0",
    "overall gpa":                 None,    # resolved from profile
    "pursuing further education":  "No",
    "outstanding offers":          "No",
    "holding any offer":           "No",
    # Employment
    "years of experience":         "2",
    # Salary
    "salary":                      "Flexible",
    "compensation":                "Flexible",
    "expected salary":             "Flexible",
    "desired salary":              "Flexible",
    # Privacy / acknowledgments
    "acknowledge":                 "Yes",
    "privacy policy":              "Yes",
    "agree to":                    "Yes",
    "consent":                     "Yes",
    # Enrollment
    "currently enrolled":          "Yes",
    "enrolled in a university":    "Yes",
    "enrolled in university":      "Yes",
}

# Pattern → profile key for heuristic-label-based profile lookups
_LABEL_TO_PROFILE = {
    "overall gpa":          "gpa",
    "grade point average":  "gpa",
    "cumulative gpa":       "gpa",
    "school":               "university",
    "university":           "university",
    "degree":               "degree",
    "major":                "major",
    "discipline":           "major",
    "field of study":       "major",
    "graduation":           "graduation_year",
    "class year":           "graduation_year",
    "linkedin":             "linkedin",
    "github":               "github",
    "location":             "location",
    "city":                 "location",
}

_UNKNOWN_RE = re.compile(r"^unknown_\d+$")


def resolve_all_fields(
    fields: list[dict],
    profile: dict,
    company: str = "",
    schema_hash: str = "",
    role_family: str = "fullstack",
    role_title: str = "",
    db_path=None,
) -> list[dict]:
    """For each field in the schema, resolve the best answer.

    Resolution order:
      1. DB (schema_hash → company → global)
      2. Profile.yaml direct/prefix match
      3. Heuristic answers keyed on label text
      4. Context inheritance: unknown_* inherits from the preceding labeled field

    Returns list of dicts with keys: field_key, label, value, source, required.
    """
    conn = get_connection(db_path)
    results: list[dict] = []

    for i, field in enumerate(fields):
        fk       = field["field_key"]
        label    = field.get("label", "")
        ftype    = field.get("field_type", "text")
        required = bool(field.get("required", False))

        # Skip file inputs — resume is uploaded separately
        if ftype == "file":
            results.append({"field_key": fk, "label": label, "value": None,
                             "source": "file_upload", "required": required})
            continue

        # 1. DB mapping hierarchy
        db_val = resolve_field_value(conn, fk, company=company, schema_hash=schema_hash)
        if db_val is not None:
            source = "db_global"
            if schema_hash and resolve_field_value(conn, fk, schema_hash=schema_hash) == db_val:
                source = "db_schema"
            elif company and resolve_field_value(conn, fk, company=company) == db_val:
                source = "db_company"
            results.append({"field_key": fk, "label": label, "value": db_val,
                             "source": source, "required": required})
            continue

        # 2. Profile.yaml direct / prefix match
        profile_val = _match_profile(fk, label, profile)
        if profile_val is not None:
            results.append({"field_key": fk, "label": label, "value": profile_val,
                             "source": "profile", "required": required})
            continue

        # 3. Heuristic answer from label (also try profile for label-based lookups)
        h_val = _match_heuristic(label, profile)
        if h_val is not None:
            results.append({"field_key": fk, "label": label, "value": h_val,
                             "source": "heuristic", "required": required})
            continue

        # 4. Context inheritance for unknown_* — inherit answer from preceding field
        if _UNKNOWN_RE.match(fk) and i > 0:
            ctx_val = _inherit_from_context(fields, results, i, profile)
            if ctx_val is not None:
                results.append({"field_key": fk, "label": label, "value": ctx_val,
                                 "source": "heuristic_ctx", "required": required})
                continue

        # 5. Select/radio option matching: pick best option deterministically
        options = field.get("options", [])
        if options and ftype in ("select", "radio"):
            opt_val = _match_select_option(fk, label, options, profile)
            if opt_val is not None:
                results.append({"field_key": fk, "label": label, "value": opt_val,
                                 "source": "heuristic_option", "required": required})
                continue

        # 6. LLM generation (last resort) for open-ended required questions
        #    If LLM is uncertain → mark NEEDS_REVIEW instead of using a bad answer
        if required and label and ftype in ("text", "textarea"):
            try:
                from src.llm.answer_gen import can_generate, generate_answer
                if can_generate(label):
                    llm_val = generate_answer(
                        question=label,
                        profile=profile,
                        company=company,
                        role_title=role_title,
                        role_family=role_family,
                    )
                    if llm_val:
                        # If the LLM signals uncertainty, mark as needs_review
                        if llm_val.strip().upper() in ("UNSURE", "NEEDS_REVIEW", "IDK"):
                            results.append({"field_key": fk, "label": label, "value": None,
                                            "source": "needs_review", "required": required})
                        else:
                            results.append({"field_key": fk, "label": label, "value": llm_val,
                                            "source": "llm", "required": required})
                        continue
            except Exception as e:
                logger.debug(f"LLM generation skipped for '{label[:40]}': {e}")

        results.append({"field_key": fk, "label": label, "value": None,
                         "source": "unmapped", "required": required})

    conn.close()

    # Post-process: resolve checkbox groups (referral source "check one" style)
    results = _resolve_checkbox_groups(fields, results)

    return results


def get_missing_required(resolved: list[dict]) -> list[dict]:
    """Return resolved entries where value is None and the field is required.

    Each returned entry includes a 'reason' string describing why the field
    could not be resolved (e.g. "No profile key, heuristic, or LLM match for
    'Why do you want to work here?'").

    Checkbox group members are excluded if at least one member in the group
    has been resolved (group-level required means at least one must be checked).
    """
    # Identify checkbox group base keys (e.g. question_9548757008 → base)
    # Groups share the same base key with _N suffix
    _CB_GROUP_RE = re.compile(r"^(.+)_\d+$")
    resolved_cb_bases: set[str] = set()
    for r in resolved:
        if r.get("source") == "checkbox_group" and r.get("value"):
            m = _CB_GROUP_RE.match(r["field_key"])
            if m:
                resolved_cb_bases.add(m.group(1))
            else:
                resolved_cb_bases.add(r["field_key"])

    missing = []
    for r in resolved:
        if r["value"] is not None or not r.get("required") or r["source"] == "file_upload":
            continue
        # Skip checkbox group members if the group base is resolved
        fk = r["field_key"]
        m = _CB_GROUP_RE.match(fk)
        base = m.group(1) if m else fk
        if base in resolved_cb_bases or fk in resolved_cb_bases:
            continue

        # Build a descriptive reason for the miss
        label = r.get("label", fk)
        source = r.get("source", "unmapped")
        if source == "needs_review":
            reason = f"LLM uncertain for '{label}' — needs human review"
        elif source == "unmapped":
            reason = f"No profile key, heuristic, or LLM match for '{label}'"
        else:
            reason = f"Unresolved required field '{label}' (source={source})"
        r["reason"] = reason
        missing.append(r)
    return missing


def _resolve_checkbox_groups(fields: list[dict], results: list[dict]) -> list[dict]:
    """For checkbox groups (e.g. 'how did you hear'), resolve the best option.

    Greenhouse renders multi-option checkbox groups as separate fields with keys
    like question_X, question_X_1, question_X_2 ... We auto-select the most
    appropriate one and mark the whole group as resolved.
    """
    # Find groups: fields sharing the same base key (question_X, question_X_1, ...)
    _CB_SUFFIX_RE = re.compile(r"^(.+)_(\d+)$")
    base_to_indices: dict[str, list[int]] = {}
    for i, f in enumerate(fields):
        if f.get("field_type") != "checkbox":
            continue
        fk = f["field_key"]
        m = _CB_SUFFIX_RE.match(fk)
        base = m.group(1) if m else fk
        base_to_indices.setdefault(base, []).append(i)

    # Also check if the base itself is a checkbox
    for i, f in enumerate(fields):
        if f.get("field_type") != "checkbox":
            continue
        fk = f["field_key"]
        if not _CB_SUFFIX_RE.match(fk):
            base_to_indices.setdefault(fk, []).append(i)

    # Referral keywords we want to select
    _REFERRAL_PREFER = ["linkedin", "online", "internet", "job board", "website"]
    _REFERRAL_QUESTION_KW = ["hear", "find", "learn", "referral", "source", "coffee chat",
                              "handshake", "github", "conference", "career fair"]

    for base, indices in base_to_indices.items():
        if len(indices) < 2:
            continue  # Not a group

        # Check if any in the group is already resolved
        already_resolved = any(results[i].get("value") for i in indices if i < len(results))
        if already_resolved:
            continue

        # Is this a referral source group? Check labels
        labels = [fields[i].get("label", "").lower() for i in indices]
        group_label = (fields[min(indices)].get("label", "") or "").lower()

        is_referral = (
            any(k in group_label for k in _REFERRAL_QUESTION_KW)
            or any(any(k in l for k in _REFERRAL_PREFER) for l in labels)
            or any(any(k in l for k in _REFERRAL_QUESTION_KW) for l in labels)
        )

        if not is_referral:
            # For non-referral checkbox groups, mark first as resolved (consent-type)
            if indices and indices[0] < len(results):
                results[indices[0]]["value"] = "Yes"
                results[indices[0]]["source"] = "checkbox_group"
            continue

        # Find the best referral option (prefer LinkedIn or online job board)
        best_idx = None
        for pref in _REFERRAL_PREFER:
            for i in indices:
                if pref in labels[indices.index(i)] if i in indices else "":
                    best_idx = i
                    break
            if best_idx is not None:
                break

        # Fallback: pick first
        if best_idx is None and indices:
            best_idx = indices[0]

        if best_idx is not None and best_idx < len(results):
            results[best_idx]["value"] = "Yes"
            results[best_idx]["source"] = "checkbox_group"

    return results


# ── Private helpers ────────────────────────────────────────────────────────────

def _match_profile(field_key: str, label: str, profile: dict) -> Optional[str]:
    """Match a field to a profile.yaml value via direct key list or prefix."""
    fk_lower = field_key.lower()

    # Direct match in field name lists
    for profile_key, field_names in _PROFILE_FIELD_MAP.items():
        if fk_lower in field_names:
            val = profile.get(profile_key)
            if val:
                return str(val)

    # Prefix match: school_0 → school → university
    base = re.sub(r"_\d+$", "", fk_lower)  # strip trailing _0, _1 etc.
    if base in _PREFIX_PROFILE_MAP:
        profile_key = _PREFIX_PROFILE_MAP[base]
        val = profile.get(profile_key)
        if val:
            return str(val)

    # Label-based match
    label_lower = label.lower()
    for profile_key, field_names in _PROFILE_FIELD_MAP.items():
        for fn in field_names:
            if fn in label_lower:
                val = profile.get(profile_key)
                if val:
                    return str(val)

    return None


def _match_heuristic(label: str, profile: dict | None = None) -> Optional[str]:
    """Match a label to a heuristic answer, with optional profile fallback."""
    lt = label.lower()
    for keyword, answer in _HEURISTIC_ANSWERS.items():
        if keyword in lt:
            if answer is None and profile:
                # Resolve from profile via label-to-profile mapping
                for lbl_pattern, prof_key in _LABEL_TO_PROFILE.items():
                    if lbl_pattern in lt:
                        val = profile.get(prof_key)
                        return str(val) if val else None
            return answer

    # Label-based profile lookup (e.g., "What is your GPA?" → profile.gpa)
    if profile:
        for lbl_pattern, prof_key in _LABEL_TO_PROFILE.items():
            if lbl_pattern in lt:
                val = profile.get(prof_key)
                if val:
                    return str(val)

    return None


def _inherit_from_context(
    fields: list[dict],
    results: list[dict],
    idx: int,
    profile: dict,
) -> Optional[str]:
    """For an unknown_* field, try to infer an answer from the preceding field's label/value."""
    # Walk back to find the nearest preceding resolved or labeled field
    for j in range(idx - 1, -1, -1):
        prev_field  = fields[j]
        prev_label  = prev_field.get("label", "")
        prev_fk     = prev_field["field_key"]

        # If the preceding field was already resolved, use its label to drive a heuristic,
        # or directly inherit its value if it came from LLM (open-ended paired input)
        if j < len(results) and results[j]["value"] is not None:
            # Try heuristic on the preceding field's label
            h = _match_heuristic(prev_label, profile)
            if h is not None:
                return h
            # Try profile match on the preceding field's key
            p = _match_profile(prev_fk, prev_label, profile)
            if p is not None:
                return p
            # If preceding field was answered by LLM, inherit that answer
            # (unknown_* is a paired input for the same question)
            if results[j].get("source") in ("llm", "heuristic"):
                return results[j]["value"]
            break  # only look one level back

        if prev_label:
            h = _match_heuristic(prev_label, profile)
            if h is not None:
                return h
            p = _match_profile(prev_fk, prev_label, profile)
            if p is not None:
                return p
            break

    return None



def _match_select_option(
    field_key: str, label: str, options: list[str], profile: dict,
) -> Optional[str]:
    """Deterministically pick the best option from a select/radio field.

    Strategies:
      1. Profile value exact/substring match against option text.
      2. Heuristic keyword match (authorization, sponsorship, etc.).
      3. EEO "Prefer not to say" / "Decline" matching.
    """
    label_lower = label.lower()
    opts_lower = [o.lower() for o in options]

    # Profile-based: check if a profile value matches any option
    profile_val = _match_profile(field_key, label, profile)
    if profile_val:
        pv = profile_val.lower()
        for i, ol in enumerate(opts_lower):
            if pv == ol or pv in ol or ol in pv:
                return options[i]

    # Heuristic answer: check if the heuristic answer matches an option
    h_val = _match_heuristic(label, profile)
    if h_val:
        hv = h_val.lower()
        for i, ol in enumerate(opts_lower):
            if hv == ol or hv in ol:
                return options[i]
        # For Yes/No heuristic answers, find the Yes/No option
        if hv in ("yes", "no"):
            for i, ol in enumerate(opts_lower):
                if ol.strip() == hv:
                    return options[i]

    # EEO / demographics: prefer "Prefer not to say" or "Decline"
    _EEO_KEYWORDS = ["gender", "race", "ethnicity", "veteran", "disability",
                     "sexual orientation", "demographic", "transgender"]
    if any(k in label_lower for k in _EEO_KEYWORDS):
        for i, ol in enumerate(opts_lower):
            if "prefer not" in ol or "decline" in ol or "not disclose" in ol:
                return options[i]

    # Country: look for "United States" or "US"
    if "country" in label_lower:
        for i, ol in enumerate(opts_lower):
            if "united states" in ol or ol == "us" or ol == "usa":
                return options[i]

    return None