"""HFC Phone Planting — enumerator data-correction tool.

Enumerators review the errors flagged for their own submissions, see the full
variable label and its dependent variables, enter a correction and an
explanation. Data and corrections are read from and saved to the
``amhara_phone_hfc`` GitHub repository.

Data flow
---------
* read-only errors : ``amhara_phone_hfc`` -> ``phone_planting_constraints.csv`` / ``phone_planting_logic.csv``
* corrections      : ``amhara_phone_hfc`` -> ``phone_planting_corrections.csv`` (append + de-duplicate)

Performance notes
-----------------
Every Streamlit interaction re-runs the script, so all GitHub traffic is
wrapped in ``st.cache_data``. Error matching and farmer grouping are
vectorised (no ``DataFrame.apply`` per row) so a re-run costs milliseconds.
"""

from __future__ import annotations

import base64
import io
import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import pandas as pd
import requests
import streamlit as st

# ============================================================================
# CONFIGURATION
# ============================================================================

st.set_page_config(
    page_title="HFC Planting — Data Correction",
    page_icon="🌱",
    layout="wide",
    initial_sidebar_state="collapsed",
    menu_items={"About": "HFC Data Correction System v5.0"},
)

GITHUB_OWNER = "mohammed-seid"
GITHUB_REPO = "amhara_phone_hfc"
GITHUB_BRANCH = "main"

ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "admin123"
ENUMERATOR_PASSWORD = "1234"

CACHE_TTL = 3600          # core error tables: 1 hour
CORRECTIONS_TTL = 30      # saved corrections: short, so new saves appear quickly
GITHUB_REQUEST_ATTEMPTS = 3

CONSTRAINTS_FILE = "phone_planting_constraints.csv"
LOGIC_FILE = "phone_planting_logic.csv"
CORRECTIONS_FILE = "phone_planting_corrections.csv"

# Reference files shipped with the app, used to enrich labels/help text.
VAR_RENAME_FILE = "variable_renaming_map.csv"
QUESTIONNAIRE_FILE = "survey_questionniare.xlsx"

APP_DIR = Path(__file__).resolve().parent

# Columns written to the corrections file (original schema first, additions after)
CORRECTION_COLUMNS = [
    "error_type", "username", "woreda", "kebele", "village",
    "farmer_name", "phone_no", "unique_id", "variable",
    "original_value", "correct_value", "explanation",
    "corrected_by", "correction_date", "correction_timestamp",
    "outside_range", "error_key",
    "rate_type", "species",
    "seedlings_received", "seedlings_planted", "seedlings_alive",
    # --- v5 additions (label / dependency context) ---
    "label", "expected", "rule", "source", "related_variables",
]

# ============================================================================
# VARIABLE METADATA
# ============================================================================

SPECIES: List[str] = [
    "arzelib", "coffee", "dec", "gesho", "grev", "guava",
    "lemon", "moringa", "neem", "papaya", "wanza",
]

SPECIES_LABELS: Dict[str, str] = {
    "arzelib": "Arzelibanos",
    "coffee": "Coffee",
    "dec": "Decurrens",
    "gesho": "Gesho",
    "grev": "Grevillea",
    "guava": "Guava",
    "lemon": "Lemon",
    "moringa": "Moringa",
    "neem": "Neem",
    "papaya": "Papaya",
    "wanza": "Wanza",
}

# Logical role of a variable inside the seedling accounting chain.
ROLE_LABELS: Dict[str, str] = {
    "got": "Seedlings received by the household",
    "troster": "Nursery distribution record (Troster)",
    "received": "OAF seedlings received",
    "planted_priv": "OAF seedlings planted on private land",
    "planted_comm": "OAF seedlings planted on communal land",
    "planted": "OAF seedlings planted (total)",
    "died": "Seedlings that died before planting",
    "alive_oaf": "OAF seedlings currently alive (private land)",
    "alive_all": "Seedlings alive (all sources)",
    "dead": "Seedlings dead (all sources)",
    "unaccounted": "Unaccounted seedlings",
    "planting_rate": "Planting rate (% of received that was planted)",
    "survival_rate": "Survival rate (% of private planting that is alive)",
    "other": "Related variable",
}

# Display order of the dependency chain.
ROLE_ORDER: List[str] = [
    "got", "troster", "received",
    "planted_priv", "planted_comm", "planted",
    "died", "alive_oaf", "alive_all", "dead",
    "unaccounted", "planting_rate", "survival_rate",
]

COUNT_ROLES: Set[str] = {
    "got", "troster", "received", "planted_priv", "planted_comm",
    "planted", "died", "alive_oaf", "alive_all", "dead", "unaccounted",
}


def species_of(variable: Any) -> Optional[str]:
    """Return the species encoded in a variable name, if any."""
    name = str(variable or "").lower()
    best: Optional[str] = None
    for sp in SPECIES:
        if sp in name and (best is None or len(sp) > len(best)):
            best = sp
    return best


def role_of(variable: Any) -> str:
    """Classify a variable into its role inside the seedling accounting chain."""
    v = str(variable or "").lower()
    if v.startswith("survival_rate_"):
        return "survival_rate"
    if v.startswith("planting_rate_"):
        return "planting_rate"
    if v.endswith("_unaccounted_num") or v == "unaccounted_num":
        return "unaccounted"
    if v.startswith("num_alive_"):
        return "alive_all"
    if v.endswith("_num_got"):
        return "got"
    if v.endswith("_alive"):
        return "alive_oaf"
    if "troster" in v:
        return "troster"
    if v.endswith("_died"):
        return "died"
    if "planted_priv" in v or "planted_private" in v:
        return "planted_priv"
    if "planted_comm" in v:
        return "planted_comm"
    if v.endswith("_received"):
        return "received"
    if v.endswith("_planted"):
        return "planted"
    if v.endswith("_dead"):
        return "dead"
    return "other"


def role_rank(role: str) -> int:
    return ROLE_ORDER.index(role) if role in ROLE_ORDER else len(ROLE_ORDER)


def pretty_variable(variable: Any) -> str:
    """Fallback human label built from the variable name."""
    v = str(variable or "").strip()
    sp = species_of(v)
    role = role_of(v)
    if sp and role in ROLE_LABELS and role != "other":
        return f"{SPECIES_LABELS[sp]} — {ROLE_LABELS[role]}"
    return v.replace("_", " ").strip().capitalize()


def _clean_question_text(text: Any) -> str:
    """Strip XLSForm markup from a questionnaire label."""
    if text is None or (isinstance(text, float) and pd.isna(text)):
        return ""
    t = str(text)
    t = re.sub(r"<output[^>]*/>", "the nursery", t)
    t = re.sub(r"<[^>]+>", "", t)
    t = re.sub(r"\s+", " ", t)
    return t.strip()


@st.cache_data(show_spinner=False)
def load_variable_labels() -> Dict[str, str]:
    """Build ``short_variable_name -> English question text`` from the reference files.

    Best-effort: the app still works if the reference files are absent.
    """
    labels: Dict[str, str] = {}

    rename_map: Dict[str, str] = {}
    rename_path = APP_DIR / VAR_RENAME_FILE
    if rename_path.exists():
        try:
            rn = pd.read_csv(rename_path)
            if {"old_name", "new_name"}.issubset(rn.columns):
                rename_map = {
                    str(o).strip(): str(n).strip()
                    for o, n in zip(rn["old_name"], rn["new_name"])
                }
        except Exception:
            rename_map = {}

    q_path = APP_DIR / QUESTIONNAIRE_FILE
    if q_path.exists():
        try:
            q = pd.read_excel(q_path)
            if "Question" in q.columns and "Text (en)" in q.columns:
                for question, text in zip(q["Question"], q["Text (en)"]):
                    qp = str(question or "").strip()
                    if not qp.startswith("/"):
                        continue
                    # /oaf_trees/oaf_gesho/num_got -> form.oaf_trees.oaf_gesho.num_got
                    form_key = "form" + qp.replace("/", ".")
                    short = rename_map.get(form_key)
                    label = _clean_question_text(text)
                    if short and label and short not in labels:
                        labels[short] = label
        except Exception:
            pass

    return labels


# ============================================================================
# GENERIC HELPERS
# ============================================================================

def parse_optional_number(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    if text == "":
        return None
    try:
        number = float(text)
    except (TypeError, ValueError):
        return None
    return None if number in (-99, -999) else number


def format_display_value(value: Any) -> str:
    if value is None:
        return "Not recorded"
    try:
        if pd.isna(value):
            return "Not recorded"
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    if text in ("", "-99", "-999", "nan", "None", "NaT"):
        return "Not recorded"
    return text


def format_number(value: Optional[float]) -> str:
    if value is None:
        return "—"
    if float(value).is_integer():
        return f"{int(value):,}"
    return f"{value:,.2f}"


def build_error_key(error_type: str, unique_id: Any, variable: Any) -> str:
    return f"{str(error_type).strip()}_{str(unique_id).strip()}_{str(variable).strip()}"


def normalize_timestamp(value: Any) -> pd.Timestamp:
    try:
        if pd.isna(value):
            return pd.Timestamp("1970-01-01")
    except (TypeError, ValueError):
        pass
    try:
        parsed = pd.to_datetime(value, errors="coerce")
        if pd.notna(parsed):
            return parsed
    except Exception:
        pass
    return pd.Timestamp("1970-01-01")


# ============================================================================
# COLUMN DISCOVERY
# ============================================================================

def get_unique_id_column(df: Optional[pd.DataFrame]) -> Optional[str]:
    if df is None or len(df) == 0:
        return None
    for name in ("unique_id", "Unique_id", "UNIQUE_ID", "UniqueID", "id", "ID", "farmer_id"):
        if name in df.columns:
            return name
    for col in df.columns:
        if "id" in str(col).lower():
            return col
    return None


def get_farmer_name_column(df: Optional[pd.DataFrame]) -> Optional[str]:
    if df is None or len(df) == 0:
        return None
    for name in ("farmer_name", "resp_name", "respondent_name", "name", "farmer", "hh_name"):
        if name in df.columns:
            return name
    return None


def get_phone_column(df: Optional[pd.DataFrame]) -> Optional[str]:
    if df is None or len(df) == 0:
        return None
    for name in ("phone_no", "phone", "telephone", "mobile", "contact", "tel"):
        if name in df.columns:
            return name
    return None


def get_location_columns(df: Optional[pd.DataFrame]) -> Dict[str, Optional[str]]:
    locs: Dict[str, Optional[str]] = {"woreda": None, "kebele": None, "village": None}
    if df is None or len(df) == 0:
        return locs
    for name in ("woreda", "Woreda", "district"):
        if name in df.columns:
            locs["woreda"] = name
            break
    for name in ("kebele", "Kebele", "kebele_name"):
        if name in df.columns:
            locs["kebele"] = name
            break
    for name in ("village", "Village", "village_name", "gote"):
        if name in df.columns:
            locs["village"] = name
            break
    return locs


# ============================================================================
# CONSTRAINT / RULE PARSING
# ============================================================================

def extract_constraint_limits(constraint_text: Any) -> Tuple[Optional[float], Optional[float]]:
    """Parse ``soft max is 300`` / ``between 20 and 40`` style rules."""
    text = str(constraint_text or "").lower()
    between = re.search(
        r"between\s*(-?\d+(?:\.\d+)?)\s*(?:and|to|-)\s*(-?\d+(?:\.\d+)?)", text
    )
    if between:
        return float(between.group(1)), float(between.group(2))
    minimum = re.search(
        r"(?:>=|at\s+least|minimum|min(?!\w))\s*(?:is|:|=)?\s*(-?\d+(?:\.\d+)?)", text
    )
    maximum = re.search(
        r"(?:<=|at\s+most|maximum|max(?!\w))\s*(?:is|:|=)?\s*(-?\d+(?:\.\d+)?)", text
    )
    return (
        float(minimum.group(1)) if minimum else None,
        float(maximum.group(1)) if maximum else None,
    )


def format_expected(min_val: Optional[float], max_val: Optional[float]) -> str:
    if min_val is not None and max_val is not None:
        return f"between {format_number(min_val)} and {format_number(max_val)}"
    if min_val is not None:
        return f"at least {format_number(min_val)}"
    if max_val is not None:
        return f"at most {format_number(max_val)}"
    return "no explicit bound"


def rule_text(error: pd.Series, error_type: str) -> str:
    if error_type == "constraint":
        return str(error.get("constraint", "") or "").strip()
    for column in ("logic", "rule", "condition", "expected"):
        value = error.get(column)
        if value is not None and not pd.isna(value) and str(value).strip():
            return str(value).strip()
    return ""


def expected_text(error: pd.Series, error_type: str) -> str:
    if error_type == "constraint":
        lo, hi = extract_constraint_limits(error.get("constraint"))
        return format_expected(lo, hi)
    value = error.get("expected")
    if value is not None and not pd.isna(value) and str(value).strip():
        return str(value).strip()
    return rule_text(error, "logic")


# ============================================================================
# DEPENDENCY GRAPH
# ============================================================================

def related_variables(row: pd.Series, species: Optional[str]) -> List[str]:
    """Every variable in the row that belongs to the same species chain."""
    if not species:
        return []
    found = {
        col
        for col in row.index
        if species_of(col) == species and role_of(col) != "other"
    }
    return sorted(found, key=lambda c: (role_rank(role_of(c)), c))


def role_value(row: pd.Series, species: Optional[str], role: str) -> Tuple[Optional[float], Optional[str]]:
    """First numeric value found in the row for a given (species, role)."""
    if not species:
        return None, None
    for column in related_variables(row, species):
        if role_of(column) == role:
            value = parse_optional_number(row.get(column))
            return value, column
    return None, None


def derived_metrics(row: pd.Series, species: Optional[str]) -> Dict[str, Optional[float]]:
    """Recompute the accounting chain from the raw counts in the row."""
    got, _ = role_value(row, species, "got")
    received, _ = role_value(row, species, "received")
    planted_priv, _ = role_value(row, species, "planted_priv")
    planted_comm, _ = role_value(row, species, "planted_comm")
    planted, _ = role_value(row, species, "planted")
    died, _ = role_value(row, species, "died")
    alive, _ = role_value(row, species, "alive_oaf")

    received_total = received if received is not None else got
    if planted_priv is not None or planted_comm is not None:
        planted_total: Optional[float] = (planted_priv or 0) + (planted_comm or 0)
    else:
        planted_total = planted

    planting_rate = None
    if received_total and planted_total is not None:
        planting_rate = round(planted_total / received_total * 100, 1)

    survival_rate = None
    if planted_priv and alive is not None:
        survival_rate = round(alive / planted_priv * 100, 1)

    unaccounted = None
    if received_total is not None and planted_total is not None:
        unaccounted = received_total - planted_total - (died or 0)

    return {
        "planted_total": planted_total,
        "planting_rate": planting_rate,
        "survival_rate": survival_rate,
        "unaccounted": unaccounted,
    }


# ============================================================================
# GITHUB DATA LAYER (cached)
# ============================================================================

def github_headers() -> Dict[str, str]:
    token = st.secrets.get("github", {}).get("token")
    if not token:
        raise ValueError("GitHub token is not configured in secrets.")
    return {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
    }


def parse_csv_content(content: str) -> Optional[pd.DataFrame]:
    if content is None:
        return None
    cleaned = str(content).strip()
    if not cleaned:
        return None
    return pd.read_csv(io.StringIO(cleaned))


def fetch_file_from_github(filename: str) -> Optional[pd.DataFrame]:
    url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{filename}"
    try:
        headers = github_headers()
    except ValueError as exc:
        st.error(f"GitHub configuration problem while loading {filename}: {exc}")
        st.stop()

    response: Optional[requests.Response] = None
    for attempt in range(GITHUB_REQUEST_ATTEMPTS):
        try:
            response = requests.get(url, headers=headers, timeout=(10, 45))
        except requests.RequestException as exc:
            if attempt == GITHUB_REQUEST_ATTEMPTS - 1:
                st.error(f"Could not reach GitHub for {filename}: {exc}")
                st.stop()
            time.sleep(attempt + 1)
            continue
        if response.status_code < 500 or attempt == GITHUB_REQUEST_ATTEMPTS - 1:
            break
        time.sleep(attempt + 1)

    if response is None:
        st.error(f"No response from GitHub while loading {filename}.")
        st.stop()
    if response.status_code == 404:
        st.error(
            f"GitHub could not find {GITHUB_OWNER}/{GITHUB_REPO}/{filename}. "
            f"Check the file name and that the token can read this private repository."
        )
        st.stop()

    try:
        response.raise_for_status()
        payload = response.json()
        if not payload.get("content"):
            st.error(f"GitHub returned no content for {filename}.")
            st.stop()
        decoded = base64.b64decode(payload["content"]).decode("utf-8")
        dataframe = parse_csv_content(decoded)
        if dataframe is None or dataframe.empty:
            st.error(f"{filename} exists but contains no rows.")
            st.stop()
        if "username" not in dataframe.columns:
            st.error(f"{filename} is missing the required 'username' column.")
            st.stop()
        return dataframe
    except (requests.RequestException, ValueError, UnicodeDecodeError) as exc:
        st.error(f"Could not import {filename}: {exc}")
        st.stop()


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def load_core_data() -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Read-only error tables. Cached for CACHE_TTL so re-runs cost nothing."""
    return fetch_file_from_github(CONSTRAINTS_FILE), fetch_file_from_github(LOGIC_FILE)


def _empty_corrections() -> pd.DataFrame:
    return pd.DataFrame(columns=CORRECTION_COLUMNS)


def prepare_corrections(df: Optional[pd.DataFrame]) -> pd.DataFrame:
    if df is None or len(df) == 0:
        return _empty_corrections()
    out = df.copy()
    if "correction_timestamp" in out.columns:
        out["correction_timestamp"] = out["correction_timestamp"].apply(normalize_timestamp)
    if "outside_range" in out.columns:
        out["outside_range"] = out["outside_range"].apply(
            lambda x: str(x).lower() in ("true", "1") if pd.notna(x) else False
        )
    if "error_key" not in out.columns and {"error_type", "variable"}.issubset(out.columns):
        id_col = "unique_id" if "unique_id" in out.columns else None
        if id_col:
            out["error_key"] = [
                build_error_key(et, uid, var)
                for et, uid, var in zip(out["error_type"], out[id_col], out["variable"])
            ]
    return out


def deduplicate_corrections(df: Optional[pd.DataFrame]) -> pd.DataFrame:
    out = prepare_corrections(df)
    if out.empty or "error_key" not in out.columns:
        return out.reset_index(drop=True)
    if "correction_timestamp" in out.columns:
        out = out.sort_values("correction_timestamp", ascending=False, kind="mergesort")
    return out.drop_duplicates(subset=["error_key"], keep="first").reset_index(drop=True)


@st.cache_data(ttl=CORRECTIONS_TTL, show_spinner=False)
def load_corrections() -> pd.DataFrame:
    """Saved corrections from GitHub. Empty DataFrame if none yet."""
    headers = github_headers()
    url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{CORRECTIONS_FILE}"
    try:
        response = requests.get(url, headers=headers, timeout=(5, 25))
    except requests.RequestException as exc:
        st.error(f"Could not load saved corrections: {exc}")
        st.stop()
    if response.status_code == 404:
        return _empty_corrections()
    try:
        response.raise_for_status()
        content = base64.b64decode(response.json()["content"]).decode("utf-8")
        return deduplicate_corrections(parse_csv_content(content))
    except Exception as exc:
        st.error(f"Could not read saved corrections: {exc}")
        st.stop()


def save_corrections_to_github(new_rows: pd.DataFrame) -> bool:
    """Append rows to the corrections file, de-duplicating by error_key."""
    headers = github_headers()
    url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{CORRECTIONS_FILE}"
    try:
        response = requests.get(url, headers=headers, timeout=(5, 25))
        sha = None
        existing = _empty_corrections()
        if response.status_code == 200:
            sha = response.json()["sha"]
            existing = parse_csv_content(base64.b64decode(response.json()["content"]).decode("utf-8"))
            if existing is None:
                existing = _empty_corrections()
        elif response.status_code != 404:
            response.raise_for_status()

        combined = pd.concat([existing, new_rows], ignore_index=True)
        combined = deduplicate_corrections(combined)
        # keep a stable, readable column order
        ordered = [c for c in CORRECTION_COLUMNS if c in combined.columns]
        ordered += [c for c in combined.columns if c not in ordered]
        combined = combined[ordered].fillna("")

        encoded = base64.b64encode(combined.to_csv(index=False).encode()).decode()
        payload = {
            "message": f"Corrections update — {datetime.now():%Y-%m-%d %H:%M}",
            "content": encoded,
            "branch": GITHUB_BRANCH,
        }
        if sha:
            payload["sha"] = sha

        put = requests.put(url, headers=headers, json=payload, timeout=20)
        put.raise_for_status()
        if put.status_code not in (200, 201):
            st.error(f"GitHub rejected the save: HTTP {put.status_code}")
            return False
        return True
    except Exception as exc:  # noqa: BLE001 - surfaced to the enumerator
        st.error(f"Error saving corrections: {exc}")
        return False


def clear_data_cache() -> None:
    load_core_data.clear()
    load_corrections.clear()


# ============================================================================
# ERROR MATCHING (vectorised)
# ============================================================================

def error_keys(df: Optional[pd.DataFrame], error_type: str) -> pd.Series:
    """Vectorised error key ``<type>_<unique_id>_<variable>`` for a table."""
    if df is None or len(df) == 0:
        return pd.Series([], dtype=str)
    id_col = get_unique_id_column(df)
    if id_col is None or "variable" not in df.columns:
        return pd.Series([""] * len(df), index=df.index, dtype=str)
    return (
        str(error_type)
        + "_"
        + df[id_col].astype(str).str.strip()
        + "_"
        + df["variable"].astype(str).str.strip()
    )


def corrected_key_set(corrections: Optional[pd.DataFrame]) -> Set[str]:
    if corrections is None or len(corrections) == 0:
        return set()
    keys: Set[str] = set()
    if "error_key" in corrections.columns:
        keys |= {k for k in corrections["error_key"].dropna().astype(str).str.strip()}
    if {"error_type", "variable", "unique_id"}.issubset(corrections.columns):
        keys |= {
            build_error_key(et, uid, var)
            for et, uid, var in zip(
                corrections["error_type"], corrections["unique_id"], corrections["variable"]
            )
        }
    return {k for k in keys if k and k != "nan"}


def uncorrected(df: Optional[pd.DataFrame], error_type: str, done: Set[str]) -> pd.DataFrame:
    if df is None or len(df) == 0:
        return pd.DataFrame()
    keys = error_keys(df, error_type)
    return df[~keys.isin(done)]


# ============================================================================
# STYLES
# ============================================================================

def inject_css() -> None:
    st.markdown(
        """
        <style>
        :root {
            --brand: #1f7a44;
            --brand-2: #2fa562;
            --ink: #14231a;
            --muted: #647a6c;
            --line: #e3ece6;
            --card: #ffffff;
            --soft: #f4f9f5;
            --warn: #c9761a;
            --danger: #c0392b;
        }

        .block-container {
            padding-top: 2.2rem;
            padding-bottom: 4rem;
            max-width: 960px;
        }

        /* ---------- typography ---------- */
        h1, h2, h3 { letter-spacing: -0.01em; }

        /* ---------- app header ---------- */
        .app-header {
            background: linear-gradient(135deg, #1f7a44 0%, #2fa562 55%, #7bc47f 100%);
            color: #fff;
            border-radius: 16px;
            padding: 22px 26px;
            margin-bottom: 18px;
            box-shadow: 0 10px 24px rgba(31, 122, 68, .22);
        }
        .app-header .title { font-size: 1.5rem; font-weight: 700; line-height: 1.2; }
        .app-header .subtitle { font-size: .92rem; opacity: .92; margin-top: 4px; }
        .app-header .who {
            display: inline-block; margin-top: 12px; padding: 5px 12px;
            background: rgba(255,255,255,.18); border-radius: 999px;
            font-size: .82rem; font-weight: 600;
        }

        /* ---------- stat strip ---------- */
        .stat-grid {
            display: grid; gap: 12px; margin: 4px 0 18px 0;
            grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
        }
        .stat {
            background: var(--card); border: 1px solid var(--line);
            border-radius: 14px; padding: 14px 16px;
            box-shadow: 0 1px 2px rgba(20,35,26,.04);
        }
        .stat .value { font-size: 1.55rem; font-weight: 700; color: var(--ink); }
        .stat .label {
            font-size: .74rem; text-transform: uppercase; letter-spacing: .05em;
            color: var(--muted); font-weight: 600; margin-top: 2px;
        }

        /* ---------- farmer card ---------- */
        .farmer-card {
            background: var(--soft); border: 1px solid var(--line);
            border-left: 5px solid var(--brand);
            border-radius: 14px; padding: 16px 18px; margin-bottom: 14px;
        }
        .farmer-card .name { font-size: 1.15rem; font-weight: 700; color: var(--ink); }
        .farmer-card .meta { font-size: .85rem; color: var(--muted); margin-top: 6px; }
        .farmer-card .meta a { color: var(--brand); font-weight: 600; text-decoration: none; }

        /* ---------- chips & badges ---------- */
        .chip {
            display: inline-block; padding: 3px 10px; border-radius: 999px;
            font-size: .74rem; font-weight: 600; margin: 0 6px 6px 0;
            background: #e8f1ea; color: #235f3a; border: 1px solid #d3e5d8;
        }
        .chip.sp { background: #eaf1fb; color: #1f4e79; border-color: #d5e3f5; }
        .badge {
            display: inline-block; padding: 4px 12px; border-radius: 999px;
            font-size: .74rem; font-weight: 700; letter-spacing: .02em;
        }
        .badge.constraint { background: #fdecea; color: #a5281b; }
        .badge.logic      { background: #fff3e0; color: #9a5b06; }
        .badge.ok         { background: #e6f6ec; color: #1f7a44; }
        .badge.pending    { background: #f2f4f3; color: #55655c; }

        /* ---------- error card ---------- */
        .err-head { margin-bottom: 6px; }
        .err-title { font-size: 1.02rem; font-weight: 700; color: var(--ink); margin: 6px 0 2px 0; }
        .err-code {
            font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
            font-size: .76rem; color: var(--muted);
        }

        /* ---------- info blocks ---------- */
        .info-block {
            background: var(--card); border: 1px solid var(--line);
            border-radius: 12px; padding: 12px 14px; margin: 10px 0;
            font-size: .9rem;
        }
        .info-block .k { color: var(--muted); font-weight: 600; }
        .rule {
            font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
            font-size: .8rem; background: #f7f9f8; border: 1px dashed var(--line);
            border-radius: 8px; padding: 8px 10px; margin-top: 6px;
            color: #2c3e33; word-break: break-word;
        }

        /* ---------- dependency list ---------- */
        .dep-row {
            display: flex; justify-content: space-between; gap: 12px;
            padding: 7px 0; border-bottom: 1px dashed var(--line); font-size: .87rem;
        }
        .dep-row:last-child { border-bottom: none; }
        .dep-row .var { color: var(--muted); }
        .dep-row .val { font-weight: 700; color: var(--ink); white-space: nowrap; }
        .dep-row .val.calc { color: var(--brand); }
        .dep-sub {
            font-size: .72rem; text-transform: uppercase; letter-spacing: .05em;
            color: var(--muted); font-weight: 700; margin: 12px 0 4px 0;
        }

        /* ---------- progress ---------- */
        .pbar { background: #e6ece8; border-radius: 999px; height: 9px; overflow: hidden; }
        .pbar > span { display: block; height: 100%; background: linear-gradient(90deg, #1f7a44, #57bd7d); }
        .pbar-label { font-size: .78rem; color: var(--muted); margin-top: 6px; }

        /* ---------- controls: mobile-first touch targets ---------- */
        .stButton > button, .stDownloadButton > button, .stFormSubmitButton > button {
            width: 100%; min-height: 50px; border-radius: 12px;
            font-size: 1rem; font-weight: 600;
        }
        .stTextInput input, .stNumberInput input, .stTextArea textarea,
        .stSelectbox div[data-baseweb="select"] {
            font-size: 16px !important;
        }
        .stTextInput input, .stNumberInput input { min-height: 46px; }
        .stTextArea textarea { min-height: 96px; }

        @media (max-width: 700px) {
            .block-container { padding-left: .8rem; padding-right: .8rem; }
            .app-header { padding: 18px; }
            .app-header .title { font-size: 1.25rem; }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


# ============================================================================
# SMALL UI BUILDERS
# ============================================================================

def render_app_header(title: str, subtitle: str = "", who: str = "") -> None:
    who_html = f'<div class="who">{who}</div>' if who else ""
    sub_html = f'<div class="subtitle">{subtitle}</div>' if subtitle else ""
    st.markdown(
        f"""
        <div class="app-header">
            <div class="title">{title}</div>
            {sub_html}
            {who_html}
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_stat_strip(items: List[Tuple[str, str]]) -> None:
    cells = "".join(
        f'<div class="stat"><div class="value">{value}</div><div class="label">{label}</div></div>'
        for label, value in items
    )
    st.markdown(f'<div class="stat-grid">{cells}</div>', unsafe_allow_html=True)


def render_progress(current: int, total: int, label: str = "") -> None:
    pct = (current / total * 100) if total > 0 else 0.0
    text = label or f"{current} of {total} ready"
    st.markdown(
        f"""
        <div class="pbar"><span style="width:{pct:.1f}%"></span></div>
        <div class="pbar-label">{text} &nbsp;·&nbsp; {pct:.0f}%</div>
        """,
        unsafe_allow_html=True,
    )


def render_farmer_card(name: str, phone: str, woreda: str, kebele: str, village: str,
                       error_count: int, ready_count: int) -> None:
    pending = max(error_count - ready_count, 0)
    phone_html = (
        f'<a href="tel:{phone}">{phone}</a>' if phone and phone != "Not recorded" else "Not recorded"
    )
    location_bits = " · ".join(
        part for part in (
            f"Woreda {woreda}" if woreda else "",
            f"Kebele {kebele}" if kebele else "",
            f"Village {village}" if village else "",
        ) if part
    )
    st.markdown(
        f"""
        <div class="farmer-card">
            <div class="name">{name}</div>
            <div class="meta">Phone: {phone_html}</div>
            <div class="meta">{location_bits or "Location not recorded"}</div>
            <div style="margin-top:10px">
                <span class="badge ok">{ready_count} ready</span>
                <span class="badge pending">{pending} pending</span>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_dependency_panel(row: pd.Series, species: Optional[str], current_variable: str,
                            key_prefix: Optional[str] = None) -> Dict[str, Any]:
    """Show the seedling accounting chain and optionally let the user amend it.

    Returns ``{"originals": {...}, "updates": {...}}`` where ``updates`` holds
    only the variables whose value the enumerator actually changed.
    """
    result: Dict[str, Any] = {"originals": {}, "updates": {}}
    if not species:
        return result

    variables = [v for v in related_variables(row, species) if v != current_variable]
    if not variables:
        return result

    derived = derived_metrics(row, species)

    # read-only chain ------------------------------------------------
    chain_rows = []
    for var in variables:
        value = parse_optional_number(row.get(var))
        result["originals"][var] = value
        chain_rows.append(
            f'<div class="dep-row"><span class="var">{role_label_for(var, species)}'
            f'<br><span class="err-code">{var}</span></span>'
            f'<span class="val">{format_number(value)}</span></div>'
        )

    computed_rows = []
    if derived["planted_total"] is not None:
        computed_rows.append(
            f'<div class="dep-row"><span class="var">Planted (private + communal)</span>'
            f'<span class="val calc">{format_number(derived["planted_total"])}</span></div>'
        )
    if derived["planting_rate"] is not None:
        computed_rows.append(
            f'<div class="dep-row"><span class="var">Planting rate</span>'
            f'<span class="val calc">{derived["planting_rate"]:.1f}%</span></div>'
        )
    if derived["survival_rate"] is not None:
        computed_rows.append(
            f'<div class="dep-row"><span class="var">Survival rate</span>'
            f'<span class="val calc">{derived["survival_rate"]:.1f}%</span></div>'
        )
    if derived["unaccounted"] is not None:
        computed_rows.append(
            f'<div class="dep-row"><span class="var">Unaccounted (received − planted − died)</span>'
            f'<span class="val calc">{format_number(derived["unaccounted"])}</span></div>'
        )

    st.markdown(
        '<div class="dep-sub">Related variables for {} </div>'.format(SPECIES_LABELS[species])
        + "".join(chain_rows),
        unsafe_allow_html=True,
    )
    if computed_rows:
        st.markdown(
            '<div class="dep-sub">Recomputed from the values above</div>' + "".join(computed_rows),
            unsafe_allow_html=True,
        )

    # optional edits -------------------------------------------------
    with st.expander("Adjust dependent variables (optional)"):
        st.caption(
            "Change a linked value only if it is also wrong. Anything you change here "
            "is saved as its own correction so this household stays consistent."
        )
        for var in variables:
            original = result["originals"][var]
            new_value = st.number_input(
                f"{role_label_for(var, species)}",
                value=float(original) if original is not None else 0.0,
                step=1.0,
                format="%g",
                key=f"rel_{key_prefix or current_variable}_{var}",
                help=var,
            )
            if (original is None and new_value != 0.0) or (
                original is not None and float(new_value) != float(original)
            ):
                result["updates"][var] = new_value

    return result


def role_label_for(variable: str, species: Optional[str]) -> str:
    role = role_of(variable)
    label = ROLE_LABELS.get(role, ROLE_LABELS["other"])
    if species:
        return f"{SPECIES_LABELS.get(species, species)} — {label}"
    return label


def resolve_labels(error: pd.Series, species: Optional[str]) -> Tuple[str, str]:
    """Return (primary label, secondary/code line) for an error."""
    variable = str(error.get("variable", "") or "")
    data_label = str(error.get("label", "") or "").strip()
    questionnaire = load_variable_labels().get(variable, "")
    generated = pretty_variable(variable)

    primary = ""
    if data_label and data_label.lower() not in ("nan", "none") and data_label.lower() != variable.lower():
        primary = data_label
    elif questionnaire:
        primary = questionnaire
    else:
        primary = generated

    if not primary:
        primary = generated
    return primary, variable


# ============================================================================
# CORRECTION ENTRY
# ============================================================================

def ensure_entry(error_key: str, error_type: str, error: pd.Series,
                 unique_id: Any, species: Optional[str]) -> Dict[str, Any]:
    return st.session_state.all_corrections_data.setdefault(
        error_key,
        {
            "error_type": error_type,
            "error": error,
            "unique_id": unique_id,
            "species": species,
            "correct_value": None,
            "explanation": "",
            "outside_range": False,
            "differs_from_both": False,
            "related_updates": {},
            "label": "",
            "expected": "",
            "rule": "",
        },
    )


def entry_status(entry: Dict[str, Any]) -> Tuple[bool, str]:
    """Return (ready, message) for a single correction entry."""
    explanation = str(entry.get("explanation", "")).strip()
    value = entry.get("correct_value")
    if value is None:
        return False, "Enter a corrected value."
    if not explanation:
        return False, "An explanation is required."
    if entry.get("outside_range") and len(explanation) < 20:
        return False, "Value is outside the expected range — explain in at least 20 characters."
    if entry.get("differs_from_both") and len(explanation) < 15:
        return False, "Value differs from both records — explain in at least 15 characters."
    return True, "Ready to save."


def render_constraint_entry(error: pd.Series, error_key: str, unique_id: Any,
                            context: pd.Series, species: Optional[str]) -> Dict[str, Any]:
    entry = ensure_entry(error_key, "constraint", error, unique_id, species)
    primary, code = resolve_labels(error, species)
    lo, hi = extract_constraint_limits(error.get("constraint"))
    reported = parse_optional_number(error.get("value"))

    entry["label"] = primary
    entry["expected"] = format_expected(lo, hi)
    entry["rule"] = str(error.get("constraint", "") or "")

    st.markdown(
        f'<div class="err-head"><span class="badge constraint">Out of range</span>'
        f'{"<span class=\'chip sp\'>" + SPECIES_LABELS[species] + "</span>" if species else ""}'
        f'</div>'
        f'<div class="err-title">{primary}</div>'
        f'<div class="err-code">{code}</div>',
        unsafe_allow_html=True,
    )
    st.markdown(
        f'<div class="info-block">'
        f'<span class="k">Reported value:</span> <b>{format_display_value(error.get("value"))}</b><br>'
        f'<span class="k">Rule:</span> {entry["rule"]}<br>'
        f'<span class="k">Expected:</span> {entry["expected"]}'
        f'<div class="rule">{entry["rule"]}</div>'
        f'</div>',
        unsafe_allow_html=True,
    )

    if species:
        deps = render_dependency_panel(context, species, code, error_key)
        entry["related_updates"] = deps["updates"]

    is_count = role_of(error.get("variable")) in COUNT_ROLES
    if is_count:
        correct = st.number_input(
            "Corrected value", value=int(round(reported)) if reported is not None else 0,
            step=1, format="%d", key=f"value_{error_key}",
        )
    else:
        correct = st.number_input(
            "Corrected value", value=float(reported) if reported is not None else 0.0,
            step=0.1, format="%g", key=f"value_{error_key}",
        )
    outside = False
    if lo is not None and correct < lo:
        outside = True
    if hi is not None and correct > hi:
        outside = True
    if outside:
        st.warning(f"Still outside {entry['expected']}. A detailed explanation is required.")

    entry["correct_value"] = correct
    entry["outside_range"] = outside
    return entry


def render_logic_entry(error: pd.Series, error_key: str, unique_id: Any,
                       context: pd.Series, species: Optional[str]) -> Dict[str, Any]:
    entry = ensure_entry(error_key, "logic", error, unique_id, species)
    primary, code = resolve_labels(error, species)
    rule = rule_text(error, "logic")
    expected = expected_text(error, "logic")

    entry["label"] = primary
    entry["expected"] = expected
    entry["rule"] = rule

    reported = parse_optional_number(error.get("value"))
    troster, _ = role_value(context, species, "troster") if species else (None, None)
    role = role_of(error.get("variable"))

    st.markdown(
        f'<div class="err-head"><span class="badge logic">Logic check</span>'
        f'{"<span class=\'chip sp\'>" + SPECIES_LABELS[species] + "</span>" if species else ""}'
        f'</div>'
        f'<div class="err-title">{primary}</div>'
        f'<div class="err-code">{code}</div>',
        unsafe_allow_html=True,
    )

    reference = []
    reference.append(("Reported value", format_display_value(error.get("value"))))
    if troster is not None:
        reference.append(("Distribution record (Troster)", format_number(troster)))
    if expected:
        reference.append(("Expected", expected))
    if reported is not None and troster is not None:
        reference.append(("Difference", format_number(reported - troster)))
    rows = "".join(
        f'<div class="dep-row"><span class="var">{k}</span><span class="val">{v}</span></div>'
        for k, v in reference
    )
    st.markdown(
        f'<div class="info-block">{rows}'
        + (f'<div class="rule">{rule}</div>' if rule else "")
        + "</div>",
        unsafe_allow_html=True,
    )

    related_updates: Dict[str, Any] = {}
    if species:
        deps = render_dependency_panel(context, species, code, error_key)
        related_updates = deps["updates"]

    correct_value: Optional[float] = None
    rate_type: Optional[str] = None

    if role in ("survival_rate", "planting_rate"):
        rate_type = role
        st.markdown(f"**Recompute the {role.replace('_', ' ')}**")
        computed = derived_metrics(context, species).get(role)
        if computed is not None:
            st.info(f"Computed from the linked values: **{computed:.1f}%**")
        else:
            st.info(
                "The linked counts are incomplete. Adjust them under "
                "'Adjust dependent variables' above, or enter the value directly."
            )
        override = st.checkbox("Override the computed value", key=f"override_{error_key}")
        if override or computed is None:
            correct_value = st.number_input(
                "Corrected value (%)", value=float(reported if reported is not None else (computed or 0.0)),
                step=0.1, format="%.2f", key=f"value_{error_key}",
            )
        else:
            correct_value = computed
    elif role in COUNT_ROLES:
        default_int = int(round(reported)) if reported is not None else 0
        correct_value = st.number_input(
            "Corrected value", value=default_int, step=1, format="%d", key=f"value_{error_key}"
        )
    else:
        correct_value = st.number_input(
            "Corrected value", value=float(reported) if reported is not None else 0.0,
            step=0.1, format="%g", key=f"value_{error_key}",
        )

    differs = (
        correct_value is not None
        and reported is not None
        and troster is not None
        and correct_value != reported
        and correct_value != troster
    )
    if differs:
        st.info("This value differs from both the reported and the distribution record.")

    entry["correct_value"] = correct_value
    entry["rate_type"] = rate_type
    entry["differs_from_both"] = differs
    entry["related_updates"] = related_updates
    return entry


# ============================================================================
# SESSION STATE
# ============================================================================

def initialize_session_state() -> None:
    defaults = {
        "all_corrections_data": {},
        "is_admin": False,
        "is_authenticated": False,
        "selected_enumerator": None,
        "current_farmer_idx": 0,
        "search_query": "",
        "enum_error_types": {},   # variable -> {error types} for the signed-in enumerator
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


# ============================================================================
# AUTHENTICATION
# ============================================================================

def render_login_page(all_enumerators: List[str]) -> None:
    render_app_header(
        "HFC Planting — Data Correction",
        "Review and correct the errors flagged on your submissions.",
    )
    enumerator_tab, admin_tab = st.tabs(["Enumerator", "Administrator"])

    with enumerator_tab:
        with st.form("enumerator_login"):
            username = st.selectbox("Select your username", options=[""] + all_enumerators)
            password = st.text_input("Password", type="password")
            submitted = st.form_submit_button("Sign in", type="primary")
            if submitted:
                if username and password == ENUMERATOR_PASSWORD:
                    st.session_state.update(
                        is_authenticated=True, is_admin=False, selected_enumerator=username
                    )
                    st.rerun()
                elif not username:
                    st.error("Choose your username.")
                else:
                    st.error("Incorrect password.")

    with admin_tab:
        with st.form("admin_login"):
            admin_user = st.text_input("Username")
            admin_pass = st.text_input("Password", type="password")
            submitted = st.form_submit_button("Sign in as administrator")
            if submitted:
                if admin_user == ADMIN_USERNAME and admin_pass == ADMIN_PASSWORD:
                    st.session_state.update(
                        is_authenticated=True, is_admin=True, selected_enumerator="admin"
                    )
                    st.rerun()
                else:
                    st.error("Incorrect administrator credentials.")


# ============================================================================
# ADMIN DASHBOARD
# ============================================================================

def enumerator_statistics(constraints_df: pd.DataFrame, logic_df: pd.DataFrame,
                          all_enumerators: List[str], corrections: pd.DataFrame) -> pd.DataFrame:
    done = corrected_key_set(corrections)
    rows = []
    for enumerator in sorted(all_enumerators):
        c_keys = set(error_keys(constraints_df[constraints_df["username"] == enumerator], "constraint"))
        l_keys = set(error_keys(logic_df[logic_df["username"] == enumerator], "logic"))
        all_keys = c_keys | l_keys
        total = len(all_keys)
        solved = len(all_keys & done)
        rows.append(
            {
                "Username": enumerator,
                "Total Errors": total,
                "Solved": solved,
                "Remaining": total - solved,
                "Progress (%)": round(solved / total * 100, 1) if total else 0.0,
            }
        )
    return pd.DataFrame(rows).sort_values("Remaining", ascending=False)


def render_admin_dashboard(constraints_df: pd.DataFrame, logic_df: pd.DataFrame,
                           all_enumerators: List[str]) -> None:
    render_app_header("Administrator dashboard", "Correction progress across all enumerators.")
    col_a, col_b = st.columns([3, 1])
    with col_b:
        if st.button("Sign out"):
            st.session_state.update(is_authenticated=False, is_admin=False)
            st.rerun()

    corrections = load_corrections()
    stats = enumerator_statistics(constraints_df, logic_df, all_enumerators, corrections)

    overview_tab, enumerator_tab, export_tab = st.tabs(["Overview", "Enumerators", "Export"])

    with overview_tab:
        total = int(stats["Total Errors"].sum())
        solved = int(stats["Solved"].sum())
        active = int((stats["Remaining"] > 0).sum())
        render_stat_strip([
            ("Flagged errors", f"{total:,}"),
            ("Corrections saved", f"{solved:,}"),
            ("Enumerators with work left", f"{active:,}"),
            ("Completion", f"{(solved / total * 100 if total else 0):.0f}%"),
        ])
        render_progress(solved, total, label=f"{solved:,} of {total:,} errors corrected")

    with enumerator_tab:
        col1, col2 = st.columns(2)
        with col1:
            show_done = st.checkbox("Show enumerators with no errors left", value=False)
        with col2:
            sort_by = st.selectbox(
                "Sort by", ["Remaining", "Progress (%)", "Username"], index=0
            )
        view = stats.copy()
        if not show_done:
            view = view[view["Total Errors"] > 0]
        view = view.sort_values(sort_by, ascending=(sort_by != "Remaining"))
        st.dataframe(view, width="stretch", hide_index=True)

    with export_tab:
        if corrections.empty:
            st.info("No corrections have been saved yet.")
        else:
            stamp = datetime.now().strftime("%Y%m%d")
            col1, col2 = st.columns(2)
            with col1:
                st.download_button(
                    "Download CSV",
                    data=corrections.to_csv(index=False),
                    file_name=f"phone_planting_corrections_{stamp}.csv",
                    mime="text/csv",
                )
            with col2:
                output = io.BytesIO()
                with pd.ExcelWriter(output, engine="openpyxl") as writer:
                    corrections.to_excel(writer, index=False, sheet_name="Corrections")
                st.download_button(
                    "Download Excel",
                    data=output.getvalue(),
                    file_name=f"phone_planting_corrections_{stamp}.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            st.caption(
                f"Corrections are stored in {GITHUB_OWNER}/{GITHUB_REPO}; "
                "the file is de-duplicated by error key."
            )


# ============================================================================
# ENUMERATOR INTERFACE
# ============================================================================

def build_farmer_index(df: pd.DataFrame) -> Dict[str, pd.DataFrame]:
    if df is None or len(df) == 0:
        return {}
    id_col = get_unique_id_column(df)
    if id_col is None:
        return {}
    return {str(uid): group for uid, group in df.groupby(id_col, sort=False)}


def farmer_display(df: pd.DataFrame) -> Tuple[str, str]:
    if df is None or len(df) == 0:
        return "Unnamed household", ""
    row = df.iloc[0]
    name_col = get_farmer_name_column(df)
    phone_col = get_phone_column(df)
    name = str(row.get(name_col, "")) if name_col else ""
    phone = format_display_value(row.get(phone_col)) if phone_col else ""
    return (name.strip() or "Unnamed household"), ("" if phone == "Not recorded" else phone)


def render_enumerator_interface(constraints_df: pd.DataFrame, logic_df: pd.DataFrame) -> None:
    enumerator = st.session_state.selected_enumerator
    render_app_header(
        "HFC Planting — Data Correction",
        "Fix the errors flagged on your submissions. Each fix needs a short explanation.",
        who=f"Signed in as {enumerator}",
    )

    col_a, col_b = st.columns([3, 1])
    with col_b:
        if st.button("Sign out"):
            st.session_state.update(is_authenticated=False, selected_enumerator=None)
            st.session_state.all_corrections_data = {}
            st.rerun()

    id_col = get_unique_id_column(constraints_df) or get_unique_id_column(logic_df)
    if id_col is None:
        st.error("The error tables do not contain a household identifier column.")
        return

    corrections = load_corrections()
    done = corrected_key_set(corrections)

    enum_constraints = constraints_df[constraints_df["username"] == enumerator]
    enum_logic = logic_df[logic_df["username"] == enumerator]
    pending_constraints = uncorrected(enum_constraints, "constraint", done)
    pending_logic = uncorrected(enum_logic, "logic", done)

    # variable -> error types, used when saving dependent-variable edits
    type_index: Dict[str, Set[str]] = {}
    for var in pending_constraints.get("variable", pd.Series(dtype=str)).astype(str):
        type_index.setdefault(var, set()).add("constraint")
    for var in pending_logic.get("variable", pd.Series(dtype=str)).astype(str):
        type_index.setdefault(var, set()).add("logic")
    st.session_state.enum_error_types = type_index

    c_index = build_farmer_index(pending_constraints)
    l_index = build_farmer_index(pending_logic)
    all_ids = sorted(set(c_index) | set(l_index), key=lambda x: (len(x), x))

    saved_by_me = int((corrections["corrected_by"] == enumerator).sum()) if not corrections.empty else 0
    total_pending = len(pending_constraints) + len(pending_logic)
    render_stat_strip([
        ("Households to fix", f"{len(all_ids):,}"),
        ("Errors remaining", f"{total_pending:,}"),
        ("Corrections you saved", f"{saved_by_me:,}"),
    ])

    if not all_ids:
        st.success("No pending errors on your submissions. Nothing to correct.")
        st.balloons()
        return

    # ---- search ------------------------------------------------------
    query = st.text_input(
        "Search household by name or phone", value=st.session_state.search_query
    ).strip().lower()
    st.session_state.search_query = query

    rows = []
    for uid in all_ids:
        group = c_index.get(uid)
        if group is None or group.empty:
            group = l_index[uid]
        name, phone = farmer_display(group)
        count = len(c_index.get(uid, pd.DataFrame())) + len(l_index.get(uid, pd.DataFrame()))
        rows.append((uid, name, phone, count))

    if query:
        rows = [
            r for r in rows
            if query in r[1].lower() or query in r[2].lower() or query in r[0].lower()
        ]
    if not rows:
        st.warning("No households match that search.")
        return

    # ---- selection ---------------------------------------------------
    if st.session_state.current_farmer_idx >= len(rows):
        st.session_state.current_farmer_idx = 0
    labels = [
        f"{name} — {phone or 'no phone'} ({count} error{'s' if count != 1 else ''})"
        for _, name, phone, count in rows
    ]
    selected = st.selectbox(
        "Household", options=list(range(len(rows))),
        index=st.session_state.current_farmer_idx,
        format_func=lambda i: labels[i],
    )
    if selected != st.session_state.current_farmer_idx:
        st.session_state.current_farmer_idx = selected

    nav_prev, nav_info, nav_next = st.columns([1, 2, 1])
    idx = st.session_state.current_farmer_idx
    with nav_prev:
        if st.button("Previous", disabled=idx == 0):
            st.session_state.current_farmer_idx = max(idx - 1, 0)
            st.rerun()
    with nav_info:
        st.markdown(
            f"<div style='text-align:center;padding-top:12px;color:#647a6c;font-size:.85rem'>"
            f"Household {idx + 1} of {len(rows)}</div>",
            unsafe_allow_html=True,
        )
    with nav_next:
        if st.button("Next", disabled=idx == len(rows) - 1):
            st.session_state.current_farmer_idx = min(idx + 1, len(rows) - 1)
            st.rerun()

    uid, name, phone, _ = rows[idx]
    c_group = c_index.get(uid, pd.DataFrame())
    l_group = l_index.get(uid, pd.DataFrame())

    # ---- household header -------------------------------------------
    loc_group = c_group if len(c_group) else l_group
    if len(loc_group):
        locs = get_location_columns(loc_group)
        loc_row = loc_group.iloc[0]
        woreda = format_display_value(loc_row.get(locs["woreda"])) if locs["woreda"] else ""
        kebele = format_display_value(loc_row.get(locs["kebele"])) if locs["kebele"] else ""
        village = format_display_value(loc_row.get(locs["village"])) if locs["village"] else ""
    else:
        woreda = kebele = village = ""

    household_errors = []  # (error_type, error_key, row, context)
    for _, err in c_group.iterrows():
        household_errors.append(("constraint", err))
    for _, err in l_group.iterrows():
        household_errors.append(("logic", err))

    ready = sum(
        1 for etype, err in household_errors
        if entry_status(st.session_state.all_corrections_data.get(
            build_error_key(etype, uid, err.get("variable")), {})
        )[0]
    )
    render_farmer_card(
        name, phone,
        woreda if woreda != "Not recorded" else "",
        kebele if kebele != "Not recorded" else "",
        village if village != "Not recorded" else "",
        len(household_errors), ready,
    )

    # ---- correction cards -------------------------------------------
    for index, (etype, err) in enumerate(household_errors, start=1):
        variable = str(err.get("variable", ""))
        error_key = build_error_key(etype, uid, variable)
        species = species_of(variable)

        # context row: logic rows already carry the full record; for
        # constraint errors pull the matching logic row when available
        if etype == "logic":
            context = err
        else:
            context_group = l_index.get(uid)
            context = context_group.iloc[0] if context_group is not None and len(context_group) else err

        with st.container(border=True):
            st.markdown(
                f"<div class='err-code'>Error {index} of {len(household_errors)}</div>",
                unsafe_allow_html=True,
            )
            if etype == "constraint":
                entry = render_constraint_entry(err, error_key, uid, context, species)
            else:
                entry = render_logic_entry(err, error_key, uid, context, species)

            entry["explanation"] = st.text_area(
                "Explanation (required)",
                value=entry.get("explanation", ""),
                placeholder="Why is this correction needed?",
                key=f"explain_{error_key}",
            )

            ok, message = entry_status(entry)
            if ok:
                st.success(message)
            else:
                st.info(message)

    # ---- save --------------------------------------------------------
    all_ok = all(
        entry_status(st.session_state.all_corrections_data.get(build_error_key(etype, uid, err.get("variable")), {}))[0]
        for etype, err in household_errors
    )

    if all_ok and household_errors:
        if st.button("Save this household's corrections", type="primary"):
            _save_household(uid, name, phone, woreda, kebele, village, enumerator)
    else:
        st.caption("Complete every field with an explanation to enable saving.")


def _save_household(uid: str, name: str, phone: str, woreda: str, kebele: str, village: str,
                    enumerator: str) -> None:
    """Write the primary corrections plus any dependent-variable edits."""
    now = datetime.now()
    records: List[Dict[str, Any]] = []
    consumed_keys: List[str] = []
    type_index: Dict[str, Set[str]] = st.session_state.enum_error_types

    for error_key, entry in list(st.session_state.all_corrections_data.items()):
        if str(entry.get("unique_id")) != str(uid):
            continue
        error = entry["error"]
        variable = str(error.get("variable", ""))
        etype = entry["error_type"]

        records.append(_correction_record(
            error_type=etype, variable=variable, original_value=error.get("value"),
            correct_value=entry.get("correct_value"), explanation=entry.get("explanation", ""),
            entry=entry, name=name, phone=phone, woreda=woreda, kebele=kebele, village=village,
            enumerator=enumerator, uid=uid, now=now, source="primary",
        ))
        consumed_keys.append(error_key)

        # dependent variables the enumerator amended
        for related_var, new_value in (entry.get("related_updates") or {}).items():
            for rel_type in (type_index.get(related_var) or {"related"}):
                rel_entry = {
                    "label": role_label_for(related_var, species_of(related_var)),
                    "expected": "", "rule": "",
                    "species": species_of(related_var),
                    "error_type": rel_type,
                }
                records.append(_correction_record(
                    error_type=rel_type, variable=related_var, original_value=None,
                    correct_value=new_value,
                    explanation=f"Adjusted while correcting {variable}: {entry.get('explanation', '')}",
                    entry=rel_entry, name=name, phone=phone, woreda=woreda, kebele=kebele,
                    village=village, enumerator=enumerator, uid=uid, now=now, source="related",
                ))

    if not records:
        st.warning("Nothing to save for this household.")
        return

    if save_corrections_to_github(pd.DataFrame(records)):
        for error_key in consumed_keys:
            st.session_state.all_corrections_data.pop(error_key, None)
        load_corrections.clear()
        st.success(f"Corrections saved to {GITHUB_OWNER}/{GITHUB_REPO}.")
        st.rerun()


def _correction_record(error_type: str, variable: str, original_value: Any, correct_value: Any,
                       explanation: str, entry: Dict[str, Any], name: str, phone: str,
                       woreda: str, kebele: str, village: str, enumerator: str,
                       uid: str, now: datetime, source: str) -> Dict[str, Any]:
    return {
        "error_type": error_type,
        "username": st.session_state.get("selected_enumerator", ""),
        "woreda": woreda, "kebele": kebele, "village": village,
        "farmer_name": name, "phone_no": phone,
        "unique_id": uid, "variable": variable,
        "original_value": original_value if original_value is not None else "",
        "correct_value": correct_value,
        "explanation": explanation,
        "corrected_by": enumerator,
        "correction_date": now.strftime("%d-%b-%y"),
        "correction_timestamp": now.isoformat(),
        "outside_range": bool(entry.get("outside_range", False)),
        "error_key": build_error_key(error_type, uid, variable),
        "label": entry.get("label", ""),
        "expected": entry.get("expected", ""),
        "rule": entry.get("rule", ""),
        "source": source,
        "species": entry.get("species", "") or "",
        "related_variables": json.dumps(entry.get("related_updates", {}) or {}),
    }


# ============================================================================
# MAIN
# ============================================================================

def main() -> None:
    inject_css()
    initialize_session_state()

    with st.spinner("Loading error data from GitHub…"):
        constraints_df, logic_df = load_core_data()

    all_enumerators = sorted(
        set(constraints_df["username"].dropna().astype(str))
        | set(logic_df["username"].dropna().astype(str))
    )

    if not st.session_state.is_authenticated:
        render_login_page(all_enumerators)
        return

    if st.session_state.is_admin:
        render_admin_dashboard(constraints_df, logic_df, all_enumerators)
    else:
        render_enumerator_interface(constraints_df, logic_df)

    with st.expander("Data source"):
        st.caption(
            f"{CONSTRAINTS_FILE}: {len(constraints_df):,} rows · "
            f"{LOGIC_FILE}: {len(logic_df):,} rows · repository {GITHUB_OWNER}/{GITHUB_REPO}"
        )
        st.caption(
            f"Saved corrections are written to "
            f"{GITHUB_OWNER}/{GITHUB_REPO}/{CORRECTIONS_FILE}."
        )
        if st.button("Refresh data from GitHub"):
            clear_data_cache()
            st.rerun()


if __name__ == "__main__":
    main()
