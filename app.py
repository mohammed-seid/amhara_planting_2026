import streamlit as st
import pandas as pd
from datetime import datetime
import io
import re
import requests
import base64
from typing import Tuple, Optional, List, Dict

# ============================================================================
# CONFIGURATION
# ============================================================================

st.set_page_config(
    page_title="HFC Data Correction",
    layout="wide",
    initial_sidebar_state="collapsed",
    menu_items={
        'About': "HFC Data Correction System v4.0"
    }
)

# Constants
GITHUB_OWNER = "mohammed-seid"
GITHUB_REPO = "hfc-data-private"
ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "admin123"
ENUMERATOR_PASSWORD = "1234"
CACHE_TTL = 3600  # 1 hour

CONSTRAINTS_FILE = "constraints_survival_amhara.csv"
LOGIC_FILE = "logic_survival_amhara.csv"
CORRECTIONS_FILE = "survival_corrections.csv"

# ============================================================================
# STYLING - Mobile-First & Adaptive Design
# ============================================================================

st.markdown("""
    <style>
    /* Mobile-optimized touch targets */
    .stButton>button {
        width: 100%;
        border-radius: 8px;
        height: 54px;
        font-size: 16px;
        font-weight: 600;
        margin-bottom: 10px;
    }
    
    .stNumberInput input, .stTextInput input, .stTextArea textarea {
        padding: 12px !important;
        font-size: 16px !important;
    }
    
    .stExpander {
        border: 1px solid #e0e0e0;
        border-radius: 8px;
        margin-bottom: 12px;
        box-shadow: 0 2px 4px rgba(0,0,0,0.05);
    }
    
    .farmer-card {
        background: var(--secondary-background-color);
        padding: 16px;
        border-radius: 8px;
        margin-bottom: 12px;
        border-left: 5px solid #4CAF50;
    }
    
    .farmer-info-row {
        display: flex;
        flex-wrap: wrap;
        gap: 15px;
        margin-top: 8px;
    }
    
    .farmer-info-item {
        display: flex;
        align-items: center;
        gap: 5px;
        font-size: 13px;
        color: var(--text-color);
    }
    
    .location-badge {
        background: #e3f2fd;
        color: #1565c0;
        padding: 4px 10px;
        border-radius: 6px;
        font-size: 13px;
        font-weight: 600;
    }
    
    .error-badge {
        background: #ff6b6b;
        color: white;
        padding: 6px 14px;
        border-radius: 12px;
        font-size: 13px;
        font-weight: 600;
        display: inline-block;
    }
    
    .success-badge {
        background: #51cf66;
        color: white;
        padding: 6px 14px;
        border-radius: 12px;
        font-size: 13px;
        font-weight: 600;
        display: inline-block;
        margin-right: 5px;
    }
    
    .metric-card {
        background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
        padding: 20px;
        border-radius: 12px;
        color: white;
        text-align: center;
        box-shadow: 0 4px 6px rgba(0,0,0,0.1);
        margin-bottom: 15px;
    }
    
    .login-box {
        background: var(--secondary-background-color);
        padding: 30px;
        border-radius: 12px;
        box-shadow: 0 4px 6px rgba(0,0,0,0.05);
        margin: 20px 0;
        border: 1px solid #e0e0e0;
    }
    
    .progress-bar {
        height: 10px;
        background: #e0e0e0;
        border-radius: 5px;
        overflow: hidden;
        margin: 16px 0;
    }
    
    .progress-fill {
        height: 100%;
        background: linear-gradient(90deg, #4CAF50, #8BC34A);
        transition: width 0.3s ease;
    }
    
    @media (max-width: 768px) {
        .farmer-info-row { flex-direction: column; gap: 8px; }
        .location-badge { display: inline-block; margin-bottom: 4px; }
    }
    </style>
""", unsafe_allow_html=True)

# ============================================================================
# SESSION STATE INITIALIZATION
# ============================================================================

def initialize_session_state():
    defaults = {
        'corrected_errors': set(),
        'all_corrections_data': {},
        'is_admin': False,
        'is_authenticated': False,
        'selected_enumerator': None,
        'current_farmer_idx': 0,
        'search_query': ''
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value

initialize_session_state()

# ============================================================================
# GITHUB & DATA HANDLING FUNCTIONS
# ============================================================================

def get_github_headers() -> Dict[str, str]:
    token = st.secrets.get("github", {}).get("token")
    if not token: raise ValueError("GitHub token not configured in secrets")
    return {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}

def parse_csv_content(content: str) -> Optional[pd.DataFrame]:
    if content is None: return None
    cleaned = str(content).strip()
    if not cleaned: return None
    try: return pd.read_csv(io.StringIO(cleaned))
    except Exception as exc: raise ValueError(f"Unable to parse CSV: {exc}") from exc

def normalize_timestamp_value(value) -> pd.Timestamp:
    if pd.isna(value): return pd.Timestamp('1970-01-01 00:00:00')
    if isinstance(value, str):
        value = value.strip()
        if not value: return pd.Timestamp('1970-01-01 00:00:00')
    try:
        parsed = pd.to_datetime(value, errors='coerce')
        if pd.notna(parsed): return parsed
    except Exception: pass
    try: return pd.Timestamp(value)
    except Exception: return pd.Timestamp('1970-01-01 00:00:00')

def build_error_key(error_type: str, row: pd.Series, id_col: Optional[str] = None) -> str:
    if row is None: return ""
    if id_col is None: id_col = get_unique_id_column(pd.DataFrame([row]))
    raw_id = row.get(id_col, row.get('unique_id', row.get('id', '')))
    raw_variable = row.get('variable', '')
    return f"{str(error_type).strip()}_{str(raw_id).strip()}_{str(raw_variable).strip()}"

def prepare_corrections_dataframe(df: Optional[pd.DataFrame]) -> pd.DataFrame:
    if df is None or len(df) == 0: return pd.DataFrame()
    cleaned = df.copy()
    if 'correction_timestamp' in cleaned.columns:
        cleaned['correction_timestamp'] = cleaned['correction_timestamp'].apply(normalize_timestamp_value)
    if 'outside_range' in cleaned.columns:
        cleaned['outside_range'] = cleaned['outside_range'].apply(lambda x: str(x).lower() in ['true', '1'] if pd.notna(x) else False)
    if 'error_key' not in cleaned.columns:
        cleaned['error_key'] = cleaned.apply(lambda r: build_error_key(r.get('error_type', ''), r, r.get('id_column', 'unique_id')), axis=1)
    return cleaned

def deduplicate_corrections(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or len(df) == 0: return pd.DataFrame()
    cleaned = prepare_corrections_dataframe(df.copy())
    if 'error_key' not in cleaned.columns: return cleaned.reset_index(drop=True)
    if 'correction_timestamp' in cleaned.columns:
        cleaned = cleaned.sort_values('correction_timestamp', ascending=False, kind='mergesort')
    return cleaned.drop_duplicates(subset=['error_key'], keep='first').reset_index(drop=True)

def fetch_file_from_github(filename: str) -> Optional[pd.DataFrame]:
    headers = get_github_headers()
    url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{filename}"
    try:
        response = requests.get(url, headers=headers, timeout=(5, 25))
        response.raise_for_status()
        payload = response.json()
        if not payload.get("content"): return None
        decoded = base64.b64decode(payload["content"]).decode("utf-8")
        return parse_csv_content(decoded)
    except Exception as exc:
        st.error(f"Error loading {filename}: {str(exc)}")
        return None

@st.cache_data(ttl=CACHE_TTL)
def load_data_from_github() -> Tuple[Optional[pd.DataFrame], Optional[pd.DataFrame]]:
    constraints_df = fetch_file_from_github(CONSTRAINTS_FILE)
    logic_df = fetch_file_from_github(LOGIC_FILE)
    return constraints_df, logic_df

def load_existing_corrections() -> Optional[pd.DataFrame]:
    try:
        headers = get_github_headers()
        url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{CORRECTIONS_FILE}"
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            content = base64.b64decode(response.json()['content']).decode('utf-8')
            return deduplicate_corrections(parse_csv_content(content))
        return None
    except Exception: return None

def save_corrections_to_github(corrections_df: pd.DataFrame) -> bool:
    try:
        headers = get_github_headers()
        url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{CORRECTIONS_FILE}"
        response = requests.get(url, headers=headers)
        sha = None
        
        if response.status_code == 200:
            sha = response.json()['sha']
            existing_content = base64.b64decode(response.json()['content']).decode('utf-8')
            existing_df = pd.read_csv(io.StringIO(existing_content))
            corrections_df = pd.concat([existing_df, corrections_df], ignore_index=True)
            
        csv_data = corrections_df.to_csv(index=False)
        encoded_data = base64.b64encode(csv_data.encode()).decode()
        
        payload = {
            "message": f"Add corrections - {datetime.now().strftime('%Y-%m-%d %H:%M')}",
            "content": encoded_data, "branch": "main"
        }
        if sha: payload["sha"] = sha
        
        response = requests.put(url, headers=headers, json=payload, timeout=10)
        return response.status_code in [200, 201]
    except Exception as e:
        st.error(f"Error saving to GitHub: {str(e)}")
        return False

# ============================================================================
# COLUMN HELPERS
# ============================================================================

def get_unique_id_column(df: pd.DataFrame) -> Optional[str]:
    if df is None or len(df) == 0: return None
    for col_name in ['unique_id', 'Unique_id', 'UNIQUE_ID', 'UniqueID', 'id', 'ID', 'farmer_id']:
        if col_name in df.columns: return col_name
    for col in df.columns:
        if 'id' in col.lower(): return col
    return None

def get_farmer_name_column(df: pd.DataFrame) -> Optional[str]:
    if df is None or len(df) == 0: return None
    for col_name in ['farmer_name', 'resp_name', 'respondent_name', 'name', 'farmer', 'hh_name']:
        if col_name in df.columns: return col_name
    return None

def get_phone_column(df: pd.DataFrame) -> Optional[str]:
    if df is None or len(df) == 0: return None
    for col_name in ['phone_no', 'phone', 'telephone', 'mobile', 'contact', 'tel']:
        if col_name in df.columns: return col_name
    return None

def get_location_columns(df: pd.DataFrame) -> Dict[str, Optional[str]]:
    locs = {'woreda': None, 'kebele': None, 'village': None}
    if df is None or len(df) == 0: return locs
    for n in ['woreda', 'Woreda', 'district']:
        if n in df.columns: locs['woreda'] = n; break
    for n in ['kebele', 'Kebele', 'kebele_name']:
        if n in df.columns: locs['kebele'] = n; break
    for n in ['village', 'Village', 'village_name', 'gote']:
        if n in df.columns: locs['village'] = n; break
    return locs

def safe_get_unique_ids(df: pd.DataFrame) -> set:
    if df is None or len(df) == 0: return set()
    id_col = get_unique_id_column(df)
    if id_col is None: return set()
    return set(df[id_col].unique())

def format_display_value(value) -> str:
    if pd.isna(value) or value is None: return 'N/A'
    val = str(value).strip()
    if val in ['-99', '-999', 'nan', 'None', '']: return 'N/A'
    return val

# ============================================================================
# PROCESSING & VALIDATION
# ============================================================================

def extract_constraint_limits(constraint_text: str) -> Tuple[int, int]:
    min_val, max_val = 0, 100000
    try:
        text = str(constraint_text).lower()
        nums = re.findall(r'\d+', text)
        if 'max' in text and nums: max_val = int(nums[-1])
        if 'min' in text and nums: min_val = int(nums[-1])
        if 'between' in text and len(nums) >= 2:
            min_val, max_val = int(nums[0]), int(nums[1])
    except Exception: pass
    return min_val, max_val

def get_corrected_error_keys(enumerator: str) -> set:
    existing = load_existing_corrections()
    if existing is None or len(existing) == 0: return set()
    enum_corr = existing[existing['corrected_by'] == enumerator]
    return set(enum_corr['error_key'].dropna().unique())

def filter_uncorrected_errors(df: pd.DataFrame, error_type: str, enumerator: str) -> pd.DataFrame:
    if df is None or len(df) == 0: return pd.DataFrame()
    id_col = get_unique_id_column(df)
    if id_col is None: return pd.DataFrame()
    
    corrected_keys = get_corrected_error_keys(enumerator).union(st.session_state.corrected_errors)
    return df[~df.apply(lambda x: build_error_key(error_type, x, id_col) in corrected_keys, axis=1)]

def get_enumerator_statistics(constraints_df: pd.DataFrame, logic_df: pd.DataFrame, all_enumerators: list) -> pd.DataFrame:
    stats = []
    existing = load_existing_corrections()
    
    for enum in sorted(all_enumerators):
        c_errs = len(constraints_df[constraints_df['username'] == enum]) if constraints_df is not None else 0
        l_errs = len(logic_df[logic_df['username'] == enum]) if logic_df is not None else 0
        total = c_errs + l_errs
        solved = len(existing[existing['corrected_by'] == enum]) if existing is not None else 0
        
        stats.append({
            'Username': enum, 'Total Errors': total, 'Solved': solved,
            'Remaining': total - solved, 'Progress (%)': round((solved/total*100) if total > 0 else 0, 1)
        })
    return pd.DataFrame(stats).sort_values('Remaining', ascending=False)

def validate_farmer_corrections(farmer_id: str, id_col: str) -> Tuple[bool, List[str], int, int]:
    farmer_corrections = {k: v for k, v in st.session_state.all_corrections_data.items() if str(v['error_data'].get(id_col)) == str(farmer_id)}
    total_errors = len(farmer_corrections)
    completed = 0
    missing = []
    
    for k, d in farmer_corrections.items():
        expl = d.get('explanation', '').strip()
        var_name = d['error_data']['variable']
        if not expl:
            missing.append(var_name)
            continue
        if d['error_type'] == 'constraint' and d.get('outside_range', False) and len(expl) < 20:
            missing.append(f"{var_name} - Needs detailed explanation")
            continue
        if d['error_type'] == 'logic' and d.get('differs_from_both', False) and len(expl) < 15:
            missing.append(f"{var_name} - Needs better explanation")
            continue
        completed += 1
    return completed == total_errors, missing, completed, total_errors

def validate_corrections() -> Tuple[bool, List[str], int, int]:
    total_errors = len(st.session_state.all_corrections_data)
    completed = 0
    missing = []
    
    for k, d in st.session_state.all_corrections_data.items():
        expl = d.get('explanation', '').strip()
        var_name = d['error_data']['variable']
        etype = "Constraint" if d['error_type'] == 'constraint' else "Logic"
        
        if not expl:
            missing.append(f"{etype}: {var_name} - No explanation")
            continue
        if d['error_type'] == 'constraint' and d.get('outside_range', False) and len(expl) < 20:
            missing.append(f"Constraint: {var_name} - Out-of-range needs detail")
            continue
        if d['error_type'] == 'logic' and d.get('differs_from_both', False) and len(expl) < 15:
            missing.append(f"Logic: {var_name} - Discrepancy needs detail")
            continue
        completed += 1
    return completed == total_errors, missing, completed, total_errors

# ============================================================================
# UI COMPONENTS
# ============================================================================

def render_progress_bar(current: int, total: int):
    percentage = (current / total * 100) if total > 0 else 0
    st.markdown(f"""
        <div class="progress-bar"><div class="progress-fill" style="width: {percentage}%"></div></div>
        <p style="text-align: center; color: var(--text-color); font-size: 14px; opacity: 0.8;">
            {current} of {total} completed ({percentage:.0f}%)
        </p>
    """, unsafe_allow_html=True)

def render_metric_card(label: str, value: str, icon: str = "📊"):
    st.markdown(f"""
        <div class="metric-card">
            <div style="font-size: 32px; margin-bottom: 8px;">{icon}</div>
            <div style="font-size: 28px; font-weight: 700;">{value}</div>
            <div style="font-size: 14px; opacity: 0.9;">{label}</div>
        </div>
    """, unsafe_allow_html=True)

def render_farmer_header(farmer_name: str, phone_no: str, woreda: str, kebele: str, village: str, error_count: int, completed_count: int = 0):
    badge = f'<span class="success-badge">{completed_count} ready</span> <span class="error-badge">{error_count - completed_count} pending</span>' if completed_count > 0 else f'<span class="error-badge">{error_count} pending</span>'
    
    p_disp = format_display_value(phone_no)
    w_disp = format_display_value(woreda)
    k_disp = format_display_value(kebele)
    v_disp = format_display_value(village)
    
    st.markdown(f"""
        <div class="farmer-card">
            <div style="display: flex; justify-content: space-between; align-items: flex-start; flex-wrap: wrap;">
                <div style="flex: 1;">
                    <div style="font-size: 20px; font-weight: 700; margin-bottom: 8px;">👨‍🌾 {farmer_name}</div>
                    <div class="farmer-info-row">
                        <div class="farmer-info-item">📞 <a href="tel:{p_disp}" style="color: #4CAF50; font-weight:bold; text-decoration: none;">{p_disp}</a></div>
                    </div>
                    <div class="farmer-info-row" style="margin-top: 10px;">
                        <span class="location-badge">📍 Woreda: {w_disp}</span>
                        <span class="location-badge">🏘️ Kebele: {k_disp}</span>
                        <span class="location-badge">🏡 Village: {v_disp}</span>
                    </div>
                </div>
                <div style="margin-top: 5px;">{badge}</div>
            </div>
        </div>
    """, unsafe_allow_html=True)

def render_constraint_error(error: pd.Series, error_key: str, id_col: str):
    st.markdown(f"### 🔒 {error['variable']}")
    if 'label' in error.index and pd.notna(error['label']) and str(error['label']).strip():
        st.markdown(f"<div style='background-color: var(--secondary-background-color); padding: 10px; border-radius: 5px; margin-bottom: 10px;'>🗣️ <b>{error['label']}</b></div>", unsafe_allow_html=True)
    
    min_val, max_val = extract_constraint_limits(error['constraint'])
    try: default_value = int(float(error['value']))
    except Exception: default_value = 0
    
    st.info(f"**Reported Value:** {error['value']}  \n**Rule:** {error['constraint']}  \n💡 *Expected range: {min_val} - {max_val}*")
    
    correct_value = st.number_input("Enter Corrected Value", value=default_value, step=1, key=f"value_{error_key}")
    outside_range = False
    
    if min_val != 0 or max_val != 100000:
        if correct_value < min_val or correct_value > max_val:
            st.warning(f"⚠️ Value is outside expected range ({min_val}-{max_val}). Detailed explanation required.")
            outside_range = True
            
    explanation = st.text_area("📝 Explanation (Required)", placeholder="Why is this correction needed?", key=f"explain_{error_key}", height=100)
    
    st.session_state.all_corrections_data[error_key] = {
        'error_type': 'constraint', 'error_data': error, 'correct_value': correct_value, 
        'explanation': explanation, 'outside_range': outside_range, 'id_column': id_col
    }
    
    if explanation and explanation.strip():
        if outside_range and len(explanation.strip()) < 20: st.error("❌ Out-of-range value requires detailed explanation")
        else: st.success("✅ Ready to save")
    else: st.error("❌ Explanation required")

def render_logic_error(error: pd.Series, error_key: str, id_col: str):
    st.markdown(f"### 📊 {error['variable']}")
    if 'label' in error.index and pd.notna(error['label']) and str(error['label']).strip():
        st.markdown(f"<div style='background-color: var(--secondary-background-color); padding: 10px; border-radius: 5px; margin-bottom: 10px;'>🗣️ <b>{error['label']}</b></div>", unsafe_allow_html=True)
    
    try:
        farmer_value = int(float(error['value']))
        troster_value = int(float(error['Troster Value']))
    except Exception:
        farmer_value = 0
        troster_value = 0
        
    c1, c2, c3 = st.columns(3)
    c1.metric("Your Report", farmer_value)
    c2.metric("System Record", troster_value)
    c3.metric("Difference", farmer_value - troster_value)
    
    correct_value = st.number_input("Enter Corrected Value", value=farmer_value, step=1, key=f"value_{error_key}")
    differs_both = correct_value != farmer_value and correct_value != troster_value
    
    if differs_both: st.info(f"💡 Value differs from both reported and system.")
    
    explanation = st.text_area("📝 Explanation (Required)", placeholder="Why is there a difference?", key=f"explain_{error_key}", height=100)
    
    st.session_state.all_corrections_data[error_key] = {
        'error_type': 'logic', 'error_data': error, 'correct_value': correct_value,
        'explanation': explanation, 'differs_from_both': differs_both, 'id_column': id_col
    }
    
    if explanation and explanation.strip():
        if differs_both and len(explanation.strip()) < 15: st.error("❌ Detail explanation needed for differing value")
        else: st.success("✅ Ready to save")
    else: st.error("❌ Explanation required")

# ============================================================================
# AUTHENTICATION
# ============================================================================

def render_login_page(all_enumerators: list):
    """Render unified login page"""
    st.title("🔐 ET Survival Survey HFC Login")
    st.markdown("---")
    
    col1, col2 = st.columns(2)
    
    # Enumerator Login
    with col1:
        st.markdown('<div class="login-box">', unsafe_allow_html=True)
        st.subheader("👤 Enumerator Login")
        with st.form("enumerator_login"):
            username = st.selectbox("Select Username", options=[""] + all_enumerators, index=0)
            password = st.text_input("Password", type="password")
            submit = st.form_submit_button("🚀 Login", use_container_width=True, type="primary")
            
            if submit:
                if username and username in all_enumerators and password == ENUMERATOR_PASSWORD:
                    st.session_state.is_authenticated = True
                    st.session_state.selected_enumerator = username
                    st.session_state.is_admin = False
                    st.rerun()
                else:
                    st.error("❌ Invalid credentials")
        st.info("**Password:** `1234`")
        st.markdown('</div>', unsafe_allow_html=True)
    
    # Admin Login
    with col2:
        st.markdown('<div class="login-box">', unsafe_allow_html=True)
        st.subheader("👑 Admin Login")
        with st.form("admin_login"):
            admin_user = st.text_input("Username")
            admin_pass = st.text_input("Password", type="password")
            admin_submit = st.form_submit_button("🔑 Admin Login", use_container_width=True, type="secondary")
            
            if admin_submit:
                if admin_user == ADMIN_USERNAME and admin_pass == ADMIN_PASSWORD:
                    st.session_state.is_admin = True
                    st.session_state.is_authenticated = True
                    st.session_state.selected_enumerator = "admin"
                    st.rerun()
                else:
                    st.error("❌ Invalid admin credentials")
        st.markdown('</div>', unsafe_allow_html=True)

# ============================================================================
# MAIN INTERFACES
# ============================================================================

def render_admin_dashboard(constraints_df: pd.DataFrame, logic_df: pd.DataFrame, all_enumerators: list):
    st.title("📊 Admin Dashboard")
    if st.button("🚪 Logout", type="secondary"):
        st.session_state.is_authenticated = False
        st.session_state.is_admin = False
        st.rerun()
    
    tab_overview, tab_enum, tab_export = st.tabs(["📈 Overview", "👥 Enumerators", "💾 Export Data"])
    
    stats_df = get_enumerator_statistics(constraints_df, logic_df, all_enumerators)
    
    with tab_overview:
        st.subheader("Overall Progress")
        total_e = stats_df['Total Errors'].sum()
        total_s = stats_df['Solved'].sum()
        render_progress_bar(total_s, total_e)
        
    with tab_enum:
        st.subheader("Enumerator Progress")
        colA, colB = st.columns(2)
        with colA: show_all = st.checkbox("Show completed enumerators", value=False)
        with colB: sort_by = st.selectbox("Sort by", ["Remaining (High to Low)", "Progress (%)", "Username"])
        
        disp_df = stats_df.copy()
        if not show_all: disp_df = disp_df[disp_df['Total Errors'] > 0]
        
        if sort_by == "Remaining (High to Low)": disp_df = disp_df.sort_values('Remaining', ascending=False)
        elif sort_by == "Progress (%)": disp_df = disp_df.sort_values('Progress (%)', ascending=False)
        else: disp_df = disp_df.sort_values('Username')
        
        for _, row in disp_df.iterrows():
            with st.expander(f"👤 {row['Username']} - {row['Remaining']} left ({row['Progress (%)']}%)"):
                c1, c2 = st.columns(2)
                c1.metric("Errors", row['Total Errors'])
                c2.metric("Solved", row['Solved'])
                render_progress_bar(row['Solved'], row['Total Errors'])

    with tab_export:
        st.subheader("Export System Data")
        existing = load_existing_corrections()
        if existing is not None and not existing.empty:
            c1, c2 = st.columns(2)
            with c1:
                st.download_button("📥 CSV Export", data=existing.to_csv(index=False), file_name=f"corrections_{datetime.now().strftime('%Y%m%d')}.csv", mime='text/csv')
            with c2:
                output = io.BytesIO()
                with pd.ExcelWriter(output, engine='openpyxl') as writer:
                    existing.to_excel(writer, index=False, sheet_name='Corrections')
                st.download_button("📥 Excel Export", data=output.getvalue(), file_name=f"corrections_{datetime.now().strftime('%Y%m%d')}.xlsx", mime='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        else: st.info("No data to export.")

def render_enumerator_interface(constraints_df: pd.DataFrame, logic_df: pd.DataFrame):
    selected_enumerator = st.session_state.selected_enumerator
    st.title("🌱 HFC Data Correction")
    st.markdown(f"### Welcome, **{selected_enumerator}**")
    
    if st.button("🚪 Logout", type="secondary"):
        st.session_state.is_authenticated = False
        st.rerun()
        
    id_col = get_unique_id_column(constraints_df) or get_unique_id_column(logic_df)
    
    enum_const = filter_uncorrected_errors(constraints_df[constraints_df['username'] == selected_enumerator] if constraints_df is not None else None, 'constraint', selected_enumerator)
    enum_logic = filter_uncorrected_errors(logic_df[logic_df['username'] == selected_enumerator] if logic_df is not None else None, 'logic', selected_enumerator)
    
    all_farmers = sorted(safe_get_unique_ids(enum_const) | safe_get_unique_ids(enum_logic))
    
    existing = load_existing_corrections()
    saved_count = len(existing[existing['corrected_by'] == selected_enumerator]) if existing is not None else 0
    if saved_count > 0: st.success(f"🔥 **Streak!** You have saved **{saved_count}** corrections so far.")
    
    if len(all_farmers) == 0:
        st.success("🎉 Incredible! You have 0 pending errors.")
        st.balloons()
        return

    st.markdown("---")
    sq = st.text_input("🔍 Search Farmer (Name/Phone):", value=st.session_state.search_query).lower()
    st.session_state.search_query = sq

    filtered_farmers = []
    for fid in all_farmers:
        c_errs = enum_const[enum_const[id_col] == fid] if not enum_const.empty and id_col in enum_const.columns else pd.DataFrame()
        l_errs = enum_logic[enum_logic[id_col] == fid] if not enum_logic.empty and id_col in enum_logic.columns else pd.DataFrame()
        
        fn_col = get_farmer_name_column(c_errs if len(c_errs) else l_errs)
        ph_col = get_phone_column(c_errs if len(c_errs) else l_errs)
        
        fn = (c_errs.iloc[0].get(fn_col,'') if fn_col else '') if len(c_errs) else (l_errs.iloc[0].get(fn_col,'') if fn_col else '')
        ph = (c_errs.iloc[0].get(ph_col,'') if ph_col else '') if len(c_errs) else (l_errs.iloc[0].get(ph_col,'') if ph_col else '')
        
        if sq in str(fn).lower() or sq in str(ph): filtered_farmers.append(fid)

    if not filtered_farmers: st.warning("No farmers found matching search."); return
    if st.session_state.current_farmer_idx >= len(filtered_farmers): st.session_state.current_farmer_idx = 0

    curr_fid = filtered_farmers[st.session_state.current_farmer_idx]
    
    colA, colB, colC = st.columns([1,2,1])
    with colA:
        if st.button("⬅️ Back", disabled=(st.session_state.current_farmer_idx == 0)):
            st.session_state.current_farmer_idx -= 1
            st.rerun()
    with colB: st.markdown(f"<div style='text-align:center; padding-top:10px;'>Farmer <b>{st.session_state.current_farmer_idx + 1}</b> of {len(filtered_farmers)}</div>", unsafe_allow_html=True)
    with colC:
        if st.button("Next ➡️", disabled=(st.session_state.current_farmer_idx == len(filtered_farmers) - 1)):
            st.session_state.current_farmer_idx += 1
            st.rerun()

    st.markdown("---")
    
    fce = enum_const[enum_const[id_col] == curr_fid] if not enum_const.empty and id_col in enum_const.columns else pd.DataFrame()
    fle = enum_logic[enum_logic[id_col] == curr_fid] if not enum_logic.empty and id_col in enum_logic.columns else pd.DataFrame()
    
    sdf = fce if len(fce) else fle
    f_name = sdf.iloc[0].get(get_farmer_name_column(sdf), 'Unknown') if get_farmer_name_column(sdf) else 'Unknown'
    f_phone = sdf.iloc[0].get(get_phone_column(sdf), 'N/A') if get_phone_column(sdf) else 'N/A'
    
    locs = get_location_columns(sdf)
    w_val = sdf.iloc[0].get(locs['woreda'], '') if locs['woreda'] else ''
    k_val = sdf.iloc[0].get(locs['kebele'], '') if locs['kebele'] else ''
    v_val = sdf.iloc[0].get(locs['village'], '') if locs['village'] else ''
    
    is_valid, f_miss, f_comp, f_tot = validate_farmer_corrections(curr_fid, id_col)
    render_farmer_header(f_name, f_phone, w_val, k_val, v_val, len(fce)+len(fle), f_comp)
    
    for _, err in fce.iterrows():
        render_constraint_error(err, f"constraint_{err[id_col]}_{err['variable']}", id_col)
        st.markdown("---")
    for _, err in fle.iterrows():
        render_logic_error(err, f"logic_{err[id_col]}_{err['variable']}", id_col)
        st.markdown("---")

    if is_valid and f_tot > 0:
        if st.button(f"💾 Save {f_name}'s Data", type="primary"):
            corrections = []
            keys_to_remove = []
            
            for ek, d in st.session_state.all_corrections_data.items():
                if str(d['error_data'].get(id_col)) == str(curr_fid):
                    edata = d['error_data']
                    ekey = build_error_key(d['error_type'], edata, id_col)
                    
                    record = {
                        'error_type': d['error_type'], 'username': edata.get('username', ''),
                        'woreda': w_val, 'kebele': k_val, 'village': v_val,
                        'farmer_name': f_name, 'phone_no': f_phone,
                        'unique_id': edata.get(id_col, ''), 'variable': edata.get('variable', ''),
                        'original_value': edata.get('value', ''), 'correct_value': d['correct_value'],
                        'explanation': d['explanation'], 'corrected_by': selected_enumerator,
                        'correction_date': datetime.now().strftime("%d-%b-%y"),
                        'correction_timestamp': datetime.now().isoformat(),
                        'outside_range': d.get('outside_range', False), 'error_key': ekey
                    }
                    corrections.append(record)
                    keys_to_remove.append(ek)
                    
            if save_corrections_to_github(pd.DataFrame(corrections)):
                st.success("✅ Saved successfully!")
                for k in keys_to_remove:
                    st.session_state.corrected_errors.add(k)
                    del st.session_state.all_corrections_data[k]
                load_data_from_github.clear()
                st.rerun()
    elif f_tot > 0:
        st.warning(f"⚠️ Complete all fields to save this farmer ({f_comp}/{f_tot} ready)")

# ============================================================================
# MAIN ENTRY
# ============================================================================

def main():
    with st.spinner("Loading core data..."):
        c_df, l_df = load_data_from_github()
        
    all_enums = set()
    if c_df is not None: all_enums.update(c_df['username'].unique())
    if l_df is not None: all_enums.update(l_df['username'].unique())
    all_enums = sorted(list(all_enums))

    if not st.session_state.is_authenticated:
        render_login_page(all_enums)
        return
        
    if st.session_state.is_admin:
        render_admin_dashboard(c_df, l_df, all_enums)
    else:
        render_enumerator_interface(c_df, l_df)

if __name__ == "__main__":
    main()