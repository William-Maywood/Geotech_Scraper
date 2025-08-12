import streamlit as st
from PyPDF2 import PdfReader
from docx import Document
import re
import pandas as pd
from collections import Counter
from typing import Iterator, Tuple

# ---------------------------
# File extractors
# ---------------------------

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

# ---------------------------
# Soil drainage helpers
# ---------------------------

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

# ---------------------------
# Groundwater detector
# ---------------------------

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

# ---------------------------
# USCS parsing + drainage summary
# ---------------------------

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

# ---------------------------
# Borings vs CPT sections + shallow refusal counters (with fallbacks)
# ---------------------------

def iter_boring_sections(text: str) -> Iterator[Tuple[str, str]]:
    """
    Yield (boring_id, section_text) for each *soil boring* only.
    Matches headers like: 'LOG OF BORING GEO-033'
    """
    pat = re.compile(
        r'LOG\s+OF\s+BORING\s+(GEO-\d{3})(.*?)(?=LOG\s+OF\s+BORING\s+GEO-\d{3}|'
        r'LOG\s+OF\s+CPT|CPT\s+(?:SOUNDING|LOG|SOUNDING\s+ID|RESULTS)|Appendix|$)',
        flags=re.IGNORECASE | re.DOTALL
    )
    for m in pat.finditer(text):
        yield m.group(1).upper(), m.group(2)

def iter_cpt_sections(text: str) -> Iterator[Tuple[str, str]]:
    """
    Yield (cpt_id, section_text) for each *CPT sounding* only.
    Handles multiple header styles in geo reports/PDF extracts:
      - 'CPT SOUNDING GEO-009'
      - 'LOG OF CPT SOUNDING GEO-091'
      - 'CPT LOG GEO-046'
      - 'CPT Sounding ID GEO-110'
      - 'CPT RESULTS GEO-105'
    """
    pat = re.compile(
        r'(?:LOG\s+OF\s+CPT\s+SOUNDING|'
        r'CPT\s+(?:SOUNDING|LOG|SOUNDING\s+ID|RESULTS))\s+'
        r'(GEO-\d{3})'
        r'(.*?)(?=(?:LOG\s+OF\s+CPT\s+SOUNDING|'
        r'CPT\s+(?:SOUNDING|LOG|SOUNDING\s+ID|RESULTS))\s+GEO-\d{3}'
        r'|LOG\s+OF\s+BORING\s+GEO-\d{3}|Appendix|$)',
        flags=re.IGNORECASE | re.DOTALL
    )
    for m in pat.finditer(text):
        yield m.group(1).upper(), m.group(2)

REFUSAL_NUM_PAT = re.compile(
    r'\brefusal[^.\n]{0,160}?(\d+(?:\.\d+)?)\s*(?:feet|ft)\b',
    flags=re.IGNORECASE
)

def _count_shallow_refusals(sections: Iterator[Tuple[str, str]], threshold_ft: float = 8.0):
    total = 0
    shallow = 0
    ids_with_depth = {}
    for sid, sec in sections:
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

# ---------- CPT fallback (image-based logs) ----------

def _cpt_fallback_counts(text: str, threshold_ft: float = 8.0):
    t = text
    # (1) total CPTs from narrative like: "CPT soundings were performed at 108 locations"
    m_total = re.search(r'CPT\s+soundings?\s+(?:were\s+)?performed\s+at\s+(\d+)\s+locations', t, re.IGNORECASE)
    total = int(m_total.group(1)) if m_total else 0

    # (2) Table 2 block for CPT shallow refusal depths
    m_tbl = re.search(
        r'(Table\s*2[^.\n]*?CPT\s+Sounding\s+Shallow\s+Refusal\s+Depths.*?)(?:Table\s*3|3\.\d|Appendix|$)',
        t, re.IGNORECASE | re.DOTALL
    )
    shallow = 0
    ids_with_depth = {}

    if m_tbl:
        block = m_tbl.group(1)
        for sid, depth in re.findall(r'(GEO-\d{3}).{0,80}?(\d+(?:\.\d+)?)\s*(?:feet|ft)?', block, re.IGNORECASE | re.DOTALL):
            try:
                d = float(depth)
                ids_with_depth[sid.upper()] = d
                if d < threshold_ft:
                    shallow += 1
            except ValueError:
                pass

    pct = round(100 * shallow / (total if total else 1), 1) if total else 0.0
    return total, shallow, pct, ids_with_depth

def count_cpt_refusals_under_8ft(text: str, threshold_ft: float = 8.0):
    # try primary (section-based). if no sections found, use fallback
    total, shallow, pct, ids = _count_shallow_refusals(iter_cpt_sections(text), threshold_ft)
    if total == 0:
        return _cpt_fallback_counts(text, threshold_ft)
    return total, shallow, pct, ids

# ---------- Boring fallback (image-based logs) ----------

def _boring_fallback_counts(text: str, threshold_ft: float = 8.0):
    t = text

    # (1) Totals from narrative (design + supplemental)
    total = 0
    # e.g., "Soil borings were completed at 20 locations"
    for m in re.finditer(r'Soil\s+borings?\s+were\s+completed\s+at\s+(\d+)\s+locations', t, re.IGNORECASE):
        try:
            total += int(m.group(1))
        except ValueError:
            pass
    # e.g., "A total of six soil borings were completed ..."
    m_sup = re.search(r'A\s+total\s+of\s+(\d+)\s+soil\s+borings?\s+were?\s+completed', t, re.IGNORECASE)
    if m_sup:
        try:
            total += int(m_sup.group(1))
        except ValueError:
            pass

    # (2) Shallow refusal (<8 ft): scan globally for any "GEO-### ... refusal ... X ft"
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
    total, shallow, pct, ids = _count_shallow_refusals(iter_boring_sections(text), threshold_ft)
    if total == 0:
        return _boring_fallback_counts(text, threshold_ft)
    return total, shallow, pct, ids

# ---------------------------
# Streamlit app
# ---------------------------

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

        # USCS / drainage
        uscs_df = extract_uscs_frequencies(text, collapse_compounds=True)
        drainage = drainage_from_uscs_percentages(uscs_df, text)

        # Groundwater
        gw = find_groundwater(text)

        # Refusal (< 8 ft) for borings and CPTs separately (with fallbacks)
        bor_total, bor_shallow, bor_pct, bor_depths = count_boring_refusals_under_8ft(text, threshold_ft=8.0)
        cpt_total, cpt_shallow, cpt_pct, cpt_depths = count_cpt_refusals_under_8ft(text, threshold_ft=8.0)

    st.header("📊 Analysis Results")
    st.write(f"**Porous/Well-Draining Soils?** → `{drainage}`")
    st.write(f"**Groundwater Shallower Than 5 ft?** → `{gw}`")

    st.subheader("📏 Refusal Summary (< 8 ft)")
    st.write(f"**Borings:** `{bor_shallow} of {bor_total}` ({bor_pct}%)")
    st.write(f"**CPTs:** `{cpt_shallow} of {cpt_total}` ({cpt_pct}%)")

    # Optional debug
    if st.checkbox("Show parsed refusal depths (debug)"):
        st.write({"borings": bor_depths})
        st.write({"cpts": cpt_depths})

    st.subheader("🧱 Most Common USCS Soils in Report")
    if uscs_df.empty:
        st.write("No USCS codes detected in extracted text.")
    else:
        st.dataframe(uscs_df, use_container_width=True)

    st.info("Answers are inferred from extracted text. For high-stakes decisions, verify with boring/CPT logs in the appendices.")
