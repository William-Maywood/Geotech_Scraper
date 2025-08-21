import streamlit as st
from PyPDF2 import PdfReader
from docx import Document
import re
import pandas as pd
from collections import Counter
from typing import Iterator, Tuple

def extract_text_pdf(file) -> str:
    reader = PdfReader(file)
    return "\n".join((page.extract_text() or "") for page in reader.pages)

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
    tU = text.upper()
    found = set(re.findall(r'\b(GW|SW|GP|SP|GM|SM|GC|SC|ML|CL|MH|CH|OL|OH|PT)\b', tU))
    if found:
        qualities = [USCS_DRAIN[c] for c in found]
        overall = min(qualities, key=lambda q: RANK[q])
        return f"Mix ({', '.join(sorted(found))}) → overall {overall}"
    t = text.lower()
    if any(x in t for x in ["silty clay", "mh", "ml", "high plasticity", "low permeability"]):
        return "Not well-draining (keyword inference)"
    if any(x in t for x in ["sand", "gravel", "well-draining", "high permeability"]):
        return "Likely well-draining (keyword inference)"
    return "Unclear – needs review"

def find_groundwater(text: str) -> str:
    t = text.lower()
    if re.search(r'groundwater (?:was )?not encountered', t):
        return "No"
    if re.search(r'(groundwater|water table)[^.\n]{0,60}?(<|less than|~|at|approx)[^.\n]{0,10}?5\s*ft', t, re.IGNORECASE):
        return "Yes"
    m = re.search(r'(groundwater|water table)[^.\n]{0,60}?(\d+(?:\.\d+)?)\s*ft', t, re.IGNORECASE)
    if m:
        try:
            return "Yes" if float(m.group(2)) < 5.0 else "No"
        except:
            pass
    if re.search(r'(water table|groundwater)[^.\n]{0,60}?(exceeds|>\s*|greater than)\s*6(\.5)?\s*ft', t):
        return "No"
    return "No"

USCS_NAMES = {
    "GW":"Well-graded gravel","GP":"Poorly graded gravel",
    "GM":"Silty gravel","GC":"Clayey gravel",
    "SW":"Well-graded sand","SP":"Poorly graded sand",
    "SM":"Silty sand","SC":"Clayey sand",
    "ML":"Low-plasticity silt","CL":"Low-plasticity clay",
    "MH":"High-plasticity silt","CH":"High-plasticity clay",
    "OL":"Organic silt/clay","OH":"Organic clay","PT":"Peat (organic)"
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
        return pd.DataFrame(columns=["USCS Code","Soil Name","Count","Percent"])
    rows = [{
        "USCS Code": code,
        "Soil Name": USCS_NAMES.get(code,""),
        "Count": cnt,
        "Percent": round(100*cnt/total,2)
    } for code, cnt in counts.most_common()]
    return pd.DataFrame(rows)

BUCKET = {
    "GW":"excellent","SW":"excellent",
    "GP":"good","SP":"good",
    "GM":"moderate","SM":"moderate",
    "GC":"poor","SC":"poor","ML":"poor","CL":"poor",
    "MH":"very_poor","CH":"very_poor",
    "OL":"unstable","OH":"unstable","PT":"unstable",
}

def drainage_from_uscs_percentages(uscs_df: pd.DataFrame, fallback_text: str) -> str:
    if uscs_df.empty or "Percent" not in uscs_df.columns:
        return assess_drainage(fallback_text)
    totals = {k:0.0 for k in ["excellent","good","moderate","poor","very_poor","unstable"]}
    for _, row in uscs_df.iterrows():
        cat = BUCKET.get(str(row["USCS Code"]))
        if cat:
            totals[cat] += float(row["Percent"])
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

def iter_boring_sections(text: str) -> Iterator[Tuple[str, str]]:
    pat = re.compile(
        r'LOG\s+OF\s+BORING\s+(GEO-\d{3})(.*?)(?=LOG\s+OF\s+BORING\s+GEO-\d{3}|Appendix|$)',
        flags=re.IGNORECASE | re.DOTALL
    )
    for m in pat.finditer(text):
        yield m.group(1).upper(), m.group(2)

REFUSAL_NUM_PAT = re.compile(
    r'\brefusal[^.\n]{0,160}?(\d+(?:\.\d+)?)\s*(?:feet|ft)\b',
    flags=re.IGNORECASE
)

def _count_shallow_refusals_borings(text: str, threshold_ft: float = 8.0):
    total = 0
    shallow = 0
    ids_with_depth = {}
    for sid, sec in iter_boring_sections(text):
        total += 1
        m = REFUSAL_NUM_PAT.search(sec)
        if m:
            try:
                depth = float(m.group(1))
                ids_with_depth[sid] = depth
                if depth < threshold_ft:
                    shallow += 1
            except ValueError:
                pass
    pct = round(100 * shallow / total, 1) if total else 0.0
    return total, shallow, pct, ids_with_depth

def _boring_fallback_counts(text: str, threshold_ft: float = 8.0):
    t = text
    total = 0
    for m in re.finditer(r'Soil\s+borings?\s+were\s+completed\s+at\s+(\d+)\s+locations', t, re.IGNORECASE):
        try:
            total += int(m.group(1))
        except ValueError:
            pass
    m_sup = re.search(r'A\s+total\s+of\s+(\d+)\s+soil\s+borings?\s+were?\s+completed', t, re.IGNORECASE)
    if m_sup:
        try:
            total += int(m_sup.group(1))
        except ValueError:
            pass
    shallow = 0
    ids_with_depth = {}
    for sid, depth in re.findall(r'(GEO-\d{3}).{0,120}?refusal[^.\n]{0,120}?(\d+(?:\.\d+)?)\s*(?:feet|ft)', t, re.IGNORECASE | re.DOTALL):
        try:
            d = float(depth)
            ids_with_depth[sid.upper()] = d
            if d < threshold_ft:
                shallow += 1
        except ValueError:
            pass
    pct = round(100 * shallow / (total if total else 1), 1) if total else 0.0
    return total, shallow, pct, ids_with_depth

def count_boring_refusals_under_8ft(text: str, threshold_ft: float = 8.0):
    total, shallow, pct, ids = _count_shallow_refusals_borings(text, threshold_ft)
    if total == 0:
        return _boring_fallback_counts(text, threshold_ft)
    return total, shallow, pct, ids

st.set_page_config(page_title="Geotechnical Report Analyzer", layout="centered")
st.title("📑 Geotechnical Report Analyzer (Borings Only)")

uploaded_file = st.file_uploader("Upload a PDF or DOCX", type=["pdf", "docx"])

if uploaded_file:
    st.success(f"Uploaded: {uploaded_file.name}")
    with st.spinner("Reading and analyzing..."):
        text = extract_text_pdf(uploaded_file) if uploaded_file.name.lower().endswith(".pdf") else extract_text_docx(uploaded_file)
        uscs_df = extract_uscs_frequencies(text, collapse_compounds=True)
        drainage = drainage_from_uscs_percentages(uscs_df, text)
        gw = find_groundwater(text)
        bor_total, bor_shallow, bor_pct, bor_depths = count_boring_refusals_under_8ft(text, threshold_ft=8.0)

    st.header("📊 Analysis Results")
    st.write(f"**Porous/Well-Draining Soils?** → `{drainage}`")
    st.write(f"**Groundwater Shallower Than 5 ft?** → `{gw}`")

    st.subheader("📏 Refusal Summary (< 8 ft) — Borings")
    st.write(f"`{bor_shallow} of {bor_total}` ({bor_pct}%)")

    if st.checkbox("Show parsed refusal depths (debug)"):
        st.write({"borings": bor_depths})

    st.subheader("🧱 Most Common USCS Soils in Report")
    if uscs_df.empty:
        st.write("No USCS codes detected in extracted text.")
    else:
        st.dataframe(uscs_df[["USCS Code","Soil Name","Percent"]], use_container_width=True)

    st.info("Answers are inferred from extracted text. For high-stakes decisions, verify with boring logs/appendices.")