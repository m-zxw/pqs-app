import streamlit as st
import pandas as pd
import re
from datetime import datetime
from dateutil import parser
import io
import os
import json

# --- PAGE CONFIGURATION ---
st.set_page_config(page_title="Quenching Parameters Processor", page_icon="🔥", layout="wide")

# --- CORE LOGIC ---
def get_shift(hour):
    if 8 <= hour < 16: return 'A'
    elif 16 <= hour < 23: return 'B'
    else: return 'C'

def parse_whatsapp_data(text_content, start_filter, end_filter, sender_mapping):
    timestamp_pattern = r'(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4}(?:,?\s+|\s+)\d{1,2}[:\.]\d{2}(?:\s*[APap][Mm])?)'
    message_splits = re.split(timestamp_pattern, text_content)

    all_data = []
    current_sample_no = 1
    
    keys_list = [
        "Sample No.", "Heat#", "Size", "Size Details", "Billet Qty", "Shift", "Shared By", "Time", "Date", 
        "PQS Carriage", "P1", "P2", "P3", "P4", "P5", "P6", "Pumps in Operation", "Mill Speed m/s", 
        "Flow Rate m3", "FCV%", "After WHF Temp.", "WHF Exit Temp At Stand 1 Entry", 
        "Bar Temp. Before PQS", "Bar Temp at Cooling Bed", "PQS Water Temperature"
    ]

    # Helper: Extracts first number after a keyword
    def get_num(keyword, text):
        m = re.search(rf'{keyword}[^\d\n]*(\d+(?:\.\d+)?)', text, re.IGNORECASE)
        return m.group(1) if m else ""

    for i in range(1, len(message_splits), 2):
        ts_str = message_splits[i].strip()
        
        # Clean the text: Remove WhatsApp edits and ALL asterisks to make parsing bulletproof
        record = message_splits[i+1].replace('<This message was edited>', '').replace('*', '').strip()
        text_lower = record.lower()
        
        try:
            msg_ts = parser.parse(ts_str, fuzzy=True, dayfirst=True)
            if not (start_filter <= msg_ts <= end_filter): continue

            row = {k: "" for k in keys_list}
            row["Time"] = msg_ts.strftime("%H:%M")
            row["Date"] = msg_ts.strftime("%d/%m/%Y")
            row["Shift"] = get_shift(msg_ts.hour)
            
            # --- Sender Extraction ---
            sender = "Unknown Number"
            s_match = re.search(r'(?:\]|,|-)\s*([^:\n🚨]+):', record)
            if s_match: sender = s_match.group(1).strip()
            sender = sender.replace('[', '').replace(']', '').strip()
            if sender in sender_mapping: sender = sender_mapping[sender]
            row["Shared By"] = sender

            # --- Billets, Heat & Sample Number Tracking ---
            b_match = re.search(r'(\d+)\s*(?:billet|bullet|bilet|billete)', text_lower)
            if not b_match:
                b_match = re.search(r'(?:billet|bullet|bilet|billete)[^\d\n]*(\d+)', text_lower)
            billets = int(b_match.group(1)) if b_match else 0
            
            row["Heat#"] = get_num(r'heat', record)
            row["Billet Qty"] = billets if billets > 0 else ""
            row["Sample No."] = current_sample_no
            
            # Add billet qty for the NEXT message's sample number
            if billets > 0: current_sample_no += billets

            # --- Size & Details ---
            row["Size"] = get_num(r'size', record)
            
            # Extract Size Details safely (bounds check prevents grabbing (bar) or (°C))
            sd_match = re.search(r'\(\s*([a-zA-Z\s]+)\s*\)', record[:150])
            if sd_match:
                val = sd_match.group(1).strip()
                if val.lower() not in ['bar', 'c', 'mm']:
                    row["Size Details"] = val.title()
            
            # --- General Parameters ---
            row["Mill Speed m/s"] = get_num(r'sp[e]{1,2}d', record)
            row["Flow Rate m3"] = get_num(r'(?:fl[o]{1,2}w|f[o]{1,2}ll[o]{1,2}w|fl[i]{1,2}w|follow)', record)
            row["FCV%"] = get_num(r'fcv', record)
            
            # Clean Carriage (removes emojis, arrows, hyphens, digits)
            carriage_match = re.search(r'carriage[\s:,\-\.#=]*([^\n]+)', record, re.IGNORECASE)
            if carriage_match:
                clean_carriage = re.sub(r'[\d\-\u2192\u2794\u27A1🔵🟡]', '', carriage_match.group(1))
                row["PQS Carriage"] = clean_carriage.strip()

            # Clean Pumps
            pump_match = re.search(r'pump[s]?[\s:,\-\.#=]*([^\n]+)', record, re.IGNORECASE)
            if pump_match:
                row["Pumps in Operation"] = pump_match.group(1).strip()

            # --- Typo-Resilient Temperatures ---
            row["After WHF Temp."] = get_num(r'(?:after|afr)\s*whf', record)
            row["WHF Exit Temp At Stand 1 Entry"] = get_num(r'(?:stand|stnd|stad)\s*1', record)
            row["Bar Temp. Before PQS"] = get_num(r'before\s*pqs', record)
            row["Bar Temp at Cooling Bed"] = get_num(r'(?:cooling|coling|c\.?b\.?)', record)
            row["PQS Water Temperature"] = get_num(r'(?:water|watr)\s*temp', record)

            # --- Pressure Logic ---
            found_labeled = False
            for p_idx in range(1, 7):
                # Handles formats like "1# 10", "1# -> 10", "1# → 03", etc.
                p_match = re.search(rf'{p_idx}\s*[#\.\-\:]*\s*(?:->|\u2192|→)*\s*(\d+(?:\.\d+)?)', record, re.IGNORECASE)
                if p_match: 
                    row[f"P{p_idx}"] = p_match.group(1)
                    found_labeled = True

            # Vertical fallback (Type 5 message): If they just typed "4 \n 4 \n 12" on new lines
            if not found_labeled:
                naked_nums = []
                for line in record.split('\n'):
                    cln = line.strip()
                    if re.match(r'^[\s]*(\d+(?:\.\d+)?)[\s]*$', cln):
                        naked_nums.append(cln.strip())
                
                # Maps directly to P1-P6 if 1 to 6 standalone numbers exist
                if 0 < len(naked_nums) <= 6:
                    for p_idx, val in enumerate(naked_nums):
                        row[f"P{p_idx+1}"] = val

            all_data.append(row)

        except Exception:
            continue

    df = pd.DataFrame(all_data)
    # Strictly returns dataframe in exact sequential order of the chat log. 
    return df

# --- UI DESIGN ---
st.title("🔥 Quenching Parameters Extractor")
st.markdown("Upload your WhatsApp chat export to instantly convert raw texts into structured datasets.")

# Sidebar Configuration
with st.sidebar:
    st.header("⚙️ Configuration")
    
    st.subheader("1. Select Date & Time Range")
    col1, col2 = st.columns(2)
    with col1:
        start_date = st.date_input("Start Date")
        start_time = st.time_input("Start Time", value=datetime.strptime("00:00", "%H:%M").time())
    with col2:
        end_date = st.date_input("End Date")
        end_time = st.time_input("End Time", value=datetime.strptime("23:59", "%H:%M").time())
        
    start_dt = datetime.combine(start_date, start_time)
    end_dt = datetime.combine(end_date, end_time)
    
    st.divider()
    
    st.subheader("2. Employee Name Mapping")
    st.write("Map raw WhatsApp numbers to your staff names below.")
    mapping_file = "saved_senders.json"
    
   # Load logic
    default_list = [
        {"Raw Number/Name": "+92 346 2727806", "Employee Name": "Shahzad"},
        {"Raw Number/Name": "+92 315 8139861", "Employee Name": "Umair"},
        {"Raw Number/Name": "+92 307 1696112", "Employee Name": "Haque Nawaz"},
        {"Raw Number/Name": "+92 316 8632889", "Employee Name": "Danish"},
        {"Raw Number/Name": "+92 345 1684108", "Employee Name": "Mehboob"},
        {"Raw Number/Name": "+92 310 0082359", "Employee Name": "Wajeeh"}
    ]
    
    if os.path.exists(mapping_file):
        try:
            with open(mapping_file, "r") as f:
                current_list = json.load(f)
        except:
            current_list = default_list # Uses the full list if file breaks
    else:
        current_list = default_list # Uses the full list on first run
        
    edited_mapping = st.data_editor(pd.DataFrame(current_list), num_rows="dynamic", use_container_width=True)
    
    if st.button("💾 Save Mapping Permanently"):
        new_data = edited_mapping.to_dict(orient="records")
        with open(mapping_file, "w") as f:
            json.dump(new_data, f)
        st.success("Saved!")
        st.rerun()

    sender_dict = dict(zip(edited_mapping["Raw Number/Name"], edited_mapping["Employee Name"]))

# Main Area
st.header("📤 Upload & Process")
uploaded_file = st.file_uploader("Upload WhatsApp Text File (.txt)", type=["txt"])

if uploaded_file is not None:
    # Caches the parsed data so it doesn't reload the file endlessly
    @st.cache_data
    def load_data(file_content, start_d, end_d, mapping):
        return parse_whatsapp_data(file_content, start_d, end_d, mapping)

    content = uploaded_file.read().decode("utf-8", errors="ignore")
    
    with st.spinner('Parsing logs...'):
        result_df = load_data(content, start_dt, end_dt, sender_dict)
        
    if result_df.empty:
        st.warning("No records found in the selected range.")
    else:
        st.success(f"Extracted {len(result_df)} records!")
        
        # Filter by Shift
        available_shifts = sorted(result_df['Shift'].unique().tolist())
        selected_shifts = st.multiselect("Filter by Shift(s)", available_shifts, default=available_shifts)
        
        filtered_df = result_df[result_df['Shift'].isin(selected_shifts)]
        st.dataframe(filtered_df, use_container_width=True)
        
        st.divider()
        st.subheader("💾 Export Data")
        
        # Excel Download Button
        buffer = io.BytesIO()
        filtered_df.to_excel(buffer, index=False)
        st.download_button(
            label="📥 Download as Excel (.xlsx)",
            data=buffer.getvalue(),
            file_name=f"Quenching_Parameters_{start_date.strftime('%Y%m%d')}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True
        )
