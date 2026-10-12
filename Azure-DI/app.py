"""Azure Document Intelligence Studio - Streamlit app.

Flow:  Select model -> Upload documents -> Analyze Document -> View structured results

Run:     streamlit run app.py
Config:  a .env file next to this script (see .env.example), or paste the values in the sidebar
"""
import hashlib
import json
import os
from pathlib import Path

import pandas as pd
import streamlit as st
from azure.ai.documentintelligence import DocumentIntelligenceClient
from azure.ai.documentintelligence.models import (
    AnalyzeDocumentRequest,
    DocumentAnalysisFeature,
)
from azure.core.credentials import AzureKeyCredential
from azure.core.exceptions import HttpResponseError
from dotenv import load_dotenv

try:  # PyMuPDF renders the PDF / image preview
    import pymupdf as fitz
except ImportError:
    try:
        import fitz
    except ImportError:
        fitz = None

# Load .env from the same folder as this script (works regardless of the launch directory)
load_dotenv(Path(__file__).resolve().parent / ".env", override=True)


def env(*names: str) -> str:
    """First non-empty environment variable among `names`."""
    for n in names:
        if v := os.getenv(n, "").strip().strip('"').strip("'"):
            return v
    return ""


ENV_ENDPOINT = env("DOCUMENTINTELLIGENCE_ENDPOINT", "AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT")
ENV_KEY = env("DOCUMENTINTELLIGENCE_API_KEY", "AZURE_DOCUMENT_INTELLIGENCE_KEY")

st.set_page_config(page_title="Document Intelligence", page_icon="📄", layout="wide")

# ----------------------------------------------------------------------------
# The 5 prebuilt models. `tabs` = which result tables are shown for that model.
# ----------------------------------------------------------------------------
MODELS = {
    "Prebuilt Invoice": {
        "model_id": "prebuilt-invoice",
        "features": [],
        "tabs": ["Fields", "Line items"],
        "help": "Vendor, customer, dates, totals, tax and line items.",
    },
    "Prebuilt Receipt": {
        "model_id": "prebuilt-receipt",
        "features": [],
        "tabs": ["Fields", "Line items"],
        "help": "Merchant, date, totals, tax, tip and purchased items.",
    },
    "Prebuilt Read": {
        "model_id": "prebuilt-read",
        "features": [],
        "tabs": ["Lines", "Paragraphs", "Words", "Languages"],
        "help": "OCR: printed and handwritten text, words with confidence, detected languages.",
    },
    "Prebuilt Layout": {
        "model_id": "prebuilt-layout",
        "features": [],
        "tabs": ["Paragraphs", "Tables", "Selection marks"],
        "help": "Document structure: paragraphs with roles, tables and selection marks.",
    },
    "Prebuilt General Document": {
        "model_id": "prebuilt-layout",
        "features": [DocumentAnalysisFeature.KEY_VALUE_PAIRS],
        "tabs": ["Key-value pairs", "Tables", "Paragraphs"],
        "help": (
            "Key-value pairs, tables and text. In API v4 the old prebuilt-document model "
            "is replaced by Layout + the key-value-pairs add-on, which is what runs here."
        ),
    },
}
ALLOWED_TYPES = ["pdf", "jpg", "jpeg", "png", "bmp", "tiff", "tif", "heif"]


# ----------------------------------------------------------------------------
# Azure helpers
# ----------------------------------------------------------------------------
@st.cache_resource(show_spinner=False)
def get_client(endpoint: str, key: str) -> DocumentIntelligenceClient:
    return DocumentIntelligenceClient(endpoint=endpoint, credential=AzureKeyCredential(key))


def analyze(client: DocumentIntelligenceClient, cfg: dict, data: bytes):
    kwargs = {"features": cfg["features"]} if cfg["features"] else {}
    poller = client.begin_analyze_document(
        cfg["model_id"],
        body=AnalyzeDocumentRequest(bytes_source=data),
        **kwargs,
    )
    return poller.result()


def _conf(x):
    return round(x, 3) if x is not None else None


def _val(x):
    """Enum -> plain value."""
    return getattr(x, "value", x)


def table_to_df(t) -> pd.DataFrame:
    grid = [[""] * t.column_count for _ in range(t.row_count)]
    for c in t.cells:
        grid[c.row_index][c.column_index] = c.content or ""
    has_header = any(c.row_index == 0 and c.kind == "columnHeader" for c in t.cells)
    if has_header and len(grid) > 1:
        cols, seen = [], {}
        for i, h in enumerate(grid[0]):
            h = h.strip() or f"Column {i + 1}"
            seen[h] = seen.get(h, 0) + 1
            cols.append(h if seen[h] == 1 else f"{h} ({seen[h]})")
        return pd.DataFrame(grid[1:], columns=cols)
    return pd.DataFrame(grid)


def parse(result) -> dict:
    """Turn the SDK result into plain Python / pandas objects (safe to keep in session state)."""
    docs = result.documents or []
    multi = len(docs) > 1

    # --- prebuilt models with fields (invoice, receipt) ---
    fields, arrays, doc_types = [], {}, []
    for i, doc in enumerate(docs, 1):
        if doc.doc_type:
            doc_types.append(f"{doc.doc_type} ({_conf(doc.confidence)})")
        base = {"Doc": i} if multi else {}
        for name, f in (doc.fields or {}).items():
            if f.value_array:  # e.g. Items on invoices / receipts
                for item in f.value_array:
                    row = dict(base)
                    row.update({k: (v.content if v else None) for k, v in (item.value_object or {}).items()})
                    arrays.setdefault(name, []).append(row)
            else:
                fields.append({**base, "Field": name, "Value": f.content, "Confidence": _conf(f.confidence)})

    # --- pages: lines, words, selection marks ---
    lines, words, marks = [], [], []
    for page in result.pages or []:
        pn = page.page_number
        for i, ln in enumerate(page.lines or [], 1):
            lines.append({"Page": pn, "Line": i, "Text": ln.content})
        for w in page.words or []:
            words.append({"Page": pn, "Word": w.content, "Confidence": _conf(w.confidence)})
        for m in page.selection_marks or []:
            marks.append({"Page": pn, "State": _val(m.state), "Confidence": _conf(m.confidence)})

    # --- paragraphs, languages, key-value pairs, tables ---
    paragraphs = []
    for p in result.paragraphs or []:
        page_no = p.bounding_regions[0].page_number if p.bounding_regions else None
        paragraphs.append({"Page": page_no, "Role": _val(p.role) or "text", "Text": p.content})

    lang_rows = [
        {
            "Language": lg.locale,
            "Confidence": _conf(lg.confidence),
            "Characters": sum(s.length for s in (lg.spans or [])),
        }
        for lg in result.languages or []
    ]
    languages = pd.DataFrame(lang_rows)
    if not languages.empty:
        languages = languages.groupby("Language", as_index=False).agg(
            Confidence=("Confidence", "max"), Characters=("Characters", "sum")
        )

    kv = [
        {
            "Key": p.key.content if p.key else "",
            "Value": p.value.content if p.value else "",
            "Confidence": _conf(p.confidence),
        }
        for p in result.key_value_pairs or []
    ]

    tables = []
    for t in result.tables or []:
        page_no = t.bounding_regions[0].page_number if t.bounding_regions else "?"
        tables.append((f"Table {len(tables) + 1} · page {page_no}", table_to_df(t)))

    def one(rows):
        df = rows if isinstance(rows, pd.DataFrame) else pd.DataFrame(rows)
        return [("", df)] if not df.empty else []

    sections = {
        "Fields": one(fields),
        "Line items": [(k, pd.DataFrame(v)) for k, v in arrays.items()],
        "Key-value pairs": one(kv),
        "Tables": tables,
        "Paragraphs": one(paragraphs),
        "Lines": one(lines),
        "Words": one(words),
        "Languages": one(languages),
        "Selection marks": one(marks),
    }

    stats = [f"{len(result.pages or [])} page(s)"]
    if doc_types:
        stats.append("detected: " + ", ".join(doc_types))
    if any(s.is_handwritten for s in (result.styles or [])):
        stats.append("handwriting detected")

    try:
        raw = result.as_dict()
    except AttributeError:
        raw = dict(result)

    return {
        "sections": sections,
        "stats": stats,
        "text": result.content or "",
        "json": json.dumps(raw, indent=2, default=str),
    }


# ----------------------------------------------------------------------------
# UI helpers
# ----------------------------------------------------------------------------
def fmt_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


@st.cache_data(show_spinner=False, max_entries=32)
def page_images(doc_id: str, filename: str, _data: bytes, max_pages: int = 10):
    """Render the first pages of a PDF / image to PNG bytes. `doc_id` is the cache key."""
    ext = filename.rsplit(".", 1)[-1].lower()
    doc = fitz.open(stream=_data, filetype=ext)
    try:
        total = doc.page_count
        imgs = [doc[i].get_pixmap(dpi=110).tobytes("png") for i in range(min(total, max_pages))]
    finally:
        doc.close()
    return imgs, total


def show_preview(doc_id: str, d: dict):
    try:
        if fitz is None:
            raise RuntimeError("PyMuPDF not installed")
        imgs, total = page_images(doc_id, d["name"], d["data"])
        for i, img in enumerate(imgs, 1):
            st.image(img, caption=f"Page {i} of {total}")
        if total > len(imgs):
            st.caption(f"Preview limited to the first {len(imgs)} pages (the whole file is analyzed).")
    except Exception:  # noqa: BLE001
        try:
            st.image(d["data"])
        except Exception:  # noqa: BLE001
            st.info("Preview isn't available for this file type, but it can still be analyzed.")


def render_results(res: dict, tab_names: list, doc_id: str, name: str, model_label: str):
    st.caption(" · ".join(res["stats"]))
    shown = [t for t in tab_names if res["sections"].get(t)]
    labels = [f"{t} ({sum(len(df) for _, df in res['sections'][t])})" for t in shown]
    tabs = st.tabs(labels + ["Text", "JSON"])

    for tab, t in zip(tabs, shown):
        with tab:
            for title, df in res["sections"][t]:
                if title:
                    st.markdown(f"**{title}**")
                st.dataframe(df, hide_index=True)
    if not shown:
        st.warning("The model returned no structured data for this document. Check the Text tab.")

    with tabs[-2]:
        st.text_area(
            "Extracted text",
            res["text"],
            height=300,
            label_visibility="collapsed",
            key=f"text_{doc_id}_{model_label}",
        )
    with tabs[-1]:
        st.download_button(
            "Download JSON",
            res["json"],
            file_name=f"{os.path.splitext(name)[0]}_{model_label.lower().replace(' ', '_')}.json",
            mime="application/json",
            key=f"dl_{doc_id}_{model_label}",
        )
        st.json(res["json"], expanded=False)


# ----------------------------------------------------------------------------
# State
# ----------------------------------------------------------------------------
st.session_state.setdefault("docs", {})  # doc_id -> {name, data, size, results, errors}
st.session_state.setdefault("uploader_key", 0)
docs: dict = st.session_state.docs

if msg := st.session_state.pop("toast", None):
    st.toast(msg)

# ----------------------------------------------------------------------------
# Sidebar: Azure connection
# ----------------------------------------------------------------------------
with st.sidebar:
    st.header("Azure connection")
    from_env = bool(ENV_ENDPOINT and ENV_KEY)
    if from_env:
        st.success("Endpoint and key loaded from .env", icon="✅")
    with st.expander("Connection details", expanded=not from_env):
        endpoint = st.text_input(
            "Endpoint",
            value=ENV_ENDPOINT,
            placeholder="https://<resource>.cognitiveservices.azure.com/",
        ).strip()
        api_key = st.text_input("API key", value=ENV_KEY, type="password").strip()

# ----------------------------------------------------------------------------
# Step 1: select model
# ----------------------------------------------------------------------------
st.title("📄 Azure Document Intelligence")

def reset_page():
    """Called when the model changes: wipe documents, results, selection and the uploader."""
    st.session_state.docs = {}
    st.session_state.selected = []
    st.session_state.uploader_key += 1


st.subheader("1 · Select a model")
model_label = st.selectbox(
    "Prebuilt model", list(MODELS), label_visibility="collapsed", on_change=reset_page
)
cfg = MODELS[model_label]
st.caption(f"`{cfg['model_id']}` · {cfg['help']}")

# ----------------------------------------------------------------------------
# Step 2: upload (any number of files) + delete
# ----------------------------------------------------------------------------
st.subheader("2 · Upload documents")
uploaded = st.file_uploader(
    "Upload documents",
    type=ALLOWED_TYPES,
    accept_multiple_files=True,
    key=f"uploader_{st.session_state.uploader_key}",
    label_visibility="collapsed",
)
if uploaded:
    new_ids = []
    for f in uploaded:
        data = f.getvalue()
        doc_id = hashlib.sha1(data).hexdigest()[:12]
        if doc_id not in docs:
            docs[doc_id] = {"name": f.name, "data": data, "size": len(data), "results": {}, "errors": {}}
            new_ids.append(doc_id)
    st.session_state.selected = st.session_state.get("selected", []) + new_ids
    st.session_state.toast = f"Added {len(new_ids)} document(s)" + (
        f" ({len(uploaded) - len(new_ids)} duplicate skipped)" if len(uploaded) > len(new_ids) else ""
    )
    st.session_state.uploader_key += 1  # resets the uploader so files can be re-added later
    st.rerun()

if docs:
    with st.expander(f"Uploaded documents ({len(docs)})", expanded=True):
        for doc_id, d in list(docs.items()):
            c1, c2 = st.columns([10, 1], vertical_alignment="center")
            done = ", ".join(d["results"]) or "not analyzed"
            c1.markdown(f"**{d['name']}** · {fmt_size(d['size'])} · _{done}_")
            if c2.button("✕", key=f"del_{doc_id}", help="Delete this document"):
                del docs[doc_id]
                st.rerun()
        if st.button("Delete all"):
            docs.clear()
            st.rerun()
else:
    st.info("Upload one or more documents to continue.")
    st.stop()

# ----------------------------------------------------------------------------
# Step 3: analyze
# ----------------------------------------------------------------------------
st.subheader("3 · Analyze")
st.session_state.selected = [i for i in st.session_state.get("selected", []) if i in docs]  # drop deleted
selected = st.multiselect(
    "Documents to analyze",
    options=list(docs),
    format_func=lambda i: docs[i]["name"],
    key="selected",
)

col_a, col_b = st.columns([1, 3], vertical_alignment="center")
ready = bool(selected and endpoint and api_key)
run = col_a.button("Analyze Document", type="primary", disabled=not ready)
force = col_b.checkbox("Re-analyze documents that already have results for this model")
if not (endpoint and api_key):
    st.warning("Add your endpoint and API key in the sidebar (or in a .env file).")

if run:
    client = get_client(endpoint, api_key)
    todo = [i for i in selected if force or model_label not in docs[i]["results"]]
    if not todo:
        st.info("Nothing to do: all selected documents already have results for this model.")
    else:
        bar = st.progress(0.0)
        for n, doc_id in enumerate(todo, 1):
            d = docs[doc_id]
            bar.progress((n - 1) / len(todo), text=f"Analyzing {d['name']} ({n}/{len(todo)})")
            try:
                d["results"][model_label] = parse(analyze(client, cfg, d["data"]))
                d["errors"].pop(model_label, None)
            except HttpResponseError as e:
                d["errors"][model_label] = e.message or str(e)
            except Exception as e:  # noqa: BLE001
                d["errors"][model_label] = str(e)
        bar.empty()
        st.rerun()

# ----------------------------------------------------------------------------
# Step 4: preview + structured results
# ----------------------------------------------------------------------------
st.subheader("4 · Preview & results")
if not selected:
    st.caption("Select at least one document above.")

for doc_id in selected:
    d = docs[doc_id]
    with st.expander(d["name"], expanded=len(selected) <= 3):
        left, right = st.columns([2, 3])
        with left:
            with st.container(height=620):
                show_preview(doc_id, d)
        with right:
            if err := d["errors"].get(model_label):
                st.error(err)
            elif res := d["results"].get(model_label):
                render_results(res, cfg["tabs"], doc_id, d["name"], model_label)
            else:
                st.info(f"Click **Analyze Document** to extract data with **{model_label}**.")
