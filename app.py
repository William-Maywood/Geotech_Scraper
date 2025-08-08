
import streamlit as st
from PyPDF2 import PdfReader
from docx import Document
import re
import pandas as pd
from collections import Counter


def extract_text_pdf(file) -> str:
    """Fast, resilient text extraction from PDF (no OCR)."""
    reader = PdfReader(file)
    chunks = []
    for i, page in enumerate(reader.pages):
        txt = page.extract_text() or ""
        chunks.append(txt)
        # Optional speed cap for very large reports:
        # if i > 60:
        #     break
    return "\n".join(chunks)

def extract_text_docx(file) -> str:
    doc = Document(file)
    return "\n".join(p.text for p in doc.paragraphs if p.text)

USCS_DRAIN = {
  "GW":"Excellent","SW":"Excellent","GP":"Good","SP":"Good",
  "GM":"Moderate","SM":"Moderate","GC":"Poor","SC":"Poor",
  "ML":"Poor","CL":"Poor","MH":"Very Poor","CH":"Very Poor",
  "OL":"Unstable","OH":"Unstable","PT":"Unstable"
}
RANK = {"Excellent":4,"Good":3,"Moderate":2,"Poor":1,"Very Poor":0,"Unstable":-1}

def assess_drainage(text: str) -> str:
    """Fallback inference if we can't build a % mix table."""
    tU = text.upper()
    found = set(re.findall(r'\b(GW|SW|GP|SP|GM|SM|GC|SC|ML|CL|MH|CH|OL|OH|PT)\b', tU))
    if found:
        qualities = [USCS_DRAIN[c] for c in found]
        overall = min(qualities, key=lambda q: RANK[q])  # worst-case overall
        return f"Mix ({', '.join(sorted(found))}) → overall {overall}"
    # Fallback keywords if USCS codes aren't in extracted text
    t = text.lower()
    if any(x in t for x in ["silty clay", "mh", "ml", "high plasticity", "low permeability"]):
        return "Not well-draining (keyword inference)"
    if any(x in t for x in ["sand", "gravel", "well-draining", "high permeability"]):
        return "Likely well-draining (keyword inference)"
    return "Unclear – needs review"


def find_refusals(text: str):
    """
    Count depths explicitly tied to refusal or PWR.
    Also estimate total borings to compute %.
    """
    t = text.lower()

    # depths tied to refusal/PWR context nearby
    # e.g., "auger refusal at 6 ft", "PWR encountered at 4 ft", "pile driving refusal 5.5 feet"
    ctx_pat = r'(?:refusal|auger\s*refusal|pile[^.\n]{0,20}?refusal|pwr|partially\s*weathered\s*rock)[^.\n]{0,120}?(\d+(?:\.\d+)?)\s*(?:feet|ft)\b'
    depths = [float(d) for d in re.findall(ctx_pat, t)]
    shallow = [d for d in depths if d < 8.0]

    # estimate number of borings in the text
    # matches: "B-1", "B-26A", "boring B-14", etc.
    boring_ids = set(re.findall(r'\b(?:boring\s+)?b-?\s*(\d+[a-z]?)\b', t))
    total_borings = len(boring_ids)

    pct = round(100 * len(shallow) / total_borings, 1) if total_borings else 0.0
    return total_borings, len(shallow), pct

def find_groundwater(text: str) -> str:
    t = text.lower()

    # Strong negative signal
    if re.search(r'groundwater (?:was )?not encountered', t):
        return "No"

    # Explicit shallow wording
    if re.search(r'(groundwater|water table)[^.\n]{0,60}?(<|less than|~|at|approx)[^.\n]{0,10}?5\s*ft', t, re.IGNORECASE):
        return "Yes"

    # Any explicit depth mention
    m = re.search(r'(groundwater|water table)[^.\n]{0,60}?(\d+(?:\.\d+)?)\s*ft', t, re.IGNORECASE)
    if m:
        try:
            depth = float(m.group(2))
            return "Yes" if depth < 5.0 else "No"
        except:
            pass

    # NRCS/map phrasing like "water table exceeds 6.5 ft"
    if re.search(r'(water table|groundwater)[^.\n]{0,60}?(exceeds|>\s*|greater than)\s*6(\.5)?\s*ft', t):
        return "No"

    return "No"  # default conservative

USCS_NAMES = {
    "GW":"Well-graded gravel", "GP":"Poorly graded gravel",
    "GM":"Silty gravel", "GC":"Clayey gravel",
    "SW":"Well-graded sand", "SP":"Poorly graded sand",
    "SM":"Silty sand", "SC":"Clayey sand",
    "ML":"Low-plasticity silt", "CL":"Low-plasticity clay",
    "MH":"High-plasticity silt", "CH":"High-plasticity clay",
    "OL":"Organic silt/clay", "OH":"Organic clay",
    "PT":"Peat (organic)"
}
USCS_PATTERN = r'\b(?:SC-SM|GW|GP|GM|GC|SW|SP|SM|SC|ML|CL|MH|CH|OL|OH|PT)\b'

def extract_uscs_frequencies(text: str, collapse_compounds: bool = True):
    T = text.upper()
    all_codes = re.findall(USCS_PATTERN, T)

    expanded = []
    for code in all_codes:
        if "-" in code and collapse_compounds:
            expanded.extend([p for p in code.split("-") if p in USCS_NAMES])
        else:
            expanded.append(code)

    counts = Counter([c for c in expanded if c in USCS_NAMES])
    total = sum(counts.values())
    if total == 0:
        return pd.DataFrame(columns=["USCS Code", "Soil Name", "Count", "Percent"])

    rows = []
    for code, cnt in counts.most_common():
        rows.append({
            "USCS Code": code,
            "Soil Name": USCS_NAMES.get(code, ""),
            "Count": cnt,
            "Percent": round(100 * cnt / total, 2)
        })
    return pd.DataFrame(rows)


BUCKET = {
    "GW":"excellent", "SW":"excellent",
    "GP":"good",      "SP":"good",
    "GM":"moderate",  "SM":"moderate",
    "GC":"poor", "SC":"poor", "ML":"poor", "CL":"poor",
    "MH":"very_poor", "CH":"very_poor",
    "OL":"unstable",  "OH":"unstable", "PT":"unstable",
}

def drainage_from_uscs_percentages(uscs_df: pd.DataFrame, fallback_text: str) -> str:
    """
    Summarize drainage based on the % mix of USCS codes found in the document text.
    Falls back to assess_drainage(text) if no USCS table was found.
    """
    if uscs_df.empty or "Percent" not in uscs_df.columns:
        return assess_drainage(fallback_text)

    totals = {"excellent":0.0,"good":0.0,"moderate":0.0,"poor":0.0,"very_poor":0.0,"unstable":0.0}
    for _, row in uscs_df.iterrows():
        code = str(row["USCS Code"])
        pct  = float(row["Percent"])
        cat  = BUCKET.get(code)
        if cat:
            totals[cat] += pct

    bad = totals["poor"] + totals["very_poor"] + totals["unstable"]
    ok  = totals["excellent"] + totals["good"]
    mid = totals["moderate"]

    if bad >= 60:
        overall = "Overall NOT well-draining"
    elif bad >= 40:
        overall = "Mostly not well-draining"
    elif ok >= 50 and bad < 30:
        overall = "Overall well-draining tendency"
    else:
        overall = "Mixed drainage"

    return (
        f"{overall} — mix: "
        f"excellent {totals['excellent']:.1f}%, good {totals['good']:.1f}%, "
        f"moderate {mid:.1f}%, poor {totals['poor']:.1f}%, "
        f"very poor {totals['very_poor']:.1f}%, unstable {totals['unstable']:.1f}%"
    )

st.set_page_config(page_title="Geotechnical Report Analyzer", layout="centered")
st.title("📑 Geotechnical Report Analyzer")

uploaded_file = st.file_uploader("Upload a PDF or DOCX", type=["pdf", "docx"])

if uploaded_file:
    st.success(f"Uploaded: {uploaded_file.name}")
    with st.spinner("Reading and analyzing..."):
        if uploaded_file.name.lower().endswith(".pdf"):
            text = extract_text_pdf(uploaded_file)
        else:
            text = extract_text_docx(uploaded_file)

        uscs_df = extract_uscs_frequencies(text, collapse_compounds=True)

        drainage = drainage_from_uscs_percentages(uscs_df, text)

        total_borings, shallow_refusals, pct_refusals = find_refusals(text)
        gw = find_groundwater(text)

    st.header("📊 Analysis Results")
    st.write(f"**Porous/Well-Draining Soils?** → `{drainage}`")
    st.write(f"**Shallow Refusals (< 8 ft)?** → `{shallow_refusals} of {total_borings} borings` ({pct_refusals}%)")
    st.write(f"**Groundwater Shallower Than 5 ft?** → `{gw}`")

    st.subheader("🧱 Most Common USCS Soils in Report")
    if uscs_df.empty:
        st.write("No USCS codes detected in extracted text.")
    else:
        st.dataframe(uscs_df, use_container_width=True)

    st.info("Answers are inferred from extracted text. For high-stakes decisions, verify with boring logs/appendices.")
