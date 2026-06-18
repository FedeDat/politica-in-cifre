import io
import re
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin

import streamlit as st
import pandas as pd
import requests
import fitz
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# =========================
# CONFIG
# =========================

BASE_URL = "https://www.cr.piemonte.it/cms/legislatura-xii/consiglieri"

# =========================
# SESSION
# =========================

def create_session():
    retry = Retry(
        total=5,
        backoff_factor=1,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=["GET", "HEAD"]
    )

    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})
    session.mount("https://", HTTPAdapter(max_retries=retry))

    return session


SESSION = create_session()

# =========================
# DOWNLOAD HELPERS
# =========================

@st.cache_data(show_spinner=False)
def get_html(url):
    r = SESSION.get(url, timeout=30)
    r.raise_for_status()
    return r.text


@st.cache_data(show_spinner=False)
def get_pdf_bytes(url):
    r = SESSION.get(url, timeout=60)
    r.raise_for_status()
    return r.content

# =========================
# PROFILE
# =========================

def find_profile(name):
    words = name.lower().split()

    candidates = [
        "-".join(words),
        "".join(words),
        "-".join(words[1:]) if len(words) > 1 else "",
        "-".join([words[0], words[-1]]) if len(words) > 1 else "",
    ]

    for slug in candidates:
        if not slug:
            continue

        url = f"{BASE_URL}/{slug}"

        try:
            r = SESSION.head(url, allow_redirects=True, timeout=10)
            if r.status_code == 200:
                return url
        except:
            continue

    return None

# =========================
# PHOTO
# =========================

def get_photo(profile_url):
    soup = BeautifulSoup(get_html(profile_url), "html.parser")

    main = (
        soup.find("main")
        or soup.find("div", class_="scheda-consigliere")
        or soup.find("div", id="content")
    )

    img_src = None

    if main:
        img = main.find("img")
        if img and img.get("src"):
            img_src = img["src"]

    if not img_src:
        for img in soup.find_all("img"):
            src = img.get("src", "")
            if src and not any(x in src.lower() for x in ["logo", "icon", "banner"]):
                img_src = src
                break

    return urljoin(profile_url, img_src) if img_src else None

def get_group(name):
    try:
        df = get_councillors()
        row = df[df["Nominativo"] == name]
        if len(row) == 0:
            return None
        return row.iloc[0].get("Gruppo consiliare", None)
    except:
        return None

def get_presences(name):
    try:
        df = get_councillors()
        row = df[df["Nominativo"] == name]
        if len(row) == 0:
            return None

        votes = row.iloc[0].get("Voti", None)
        presences = row.iloc[0].get("Presenze", None)

        votes = re.search(r"\d+", votes).group()
        presences = re.search(r"\d+", presences).group()
        
        return votes, presences 
    except:
        return None

# =========================
# PDF LINKS
# =========================

def get_pdf_links(profile_url):
    url = profile_url + "/cedolini"
    soup = BeautifulSoup(get_html(url), "html.parser")

    links = set()

    for a in soup.find_all("a", href=True):
        if "cedolini-batch" in a["href"] and a["href"].endswith(".pdf"):
            links.add(urljoin(url, a["href"]))

    return sorted(links, reverse=True)

# =========================
# PDF PARSER
# =========================

def extract_numbers(pdf_url):
    pdf = fitz.open(stream=io.BytesIO(get_pdf_bytes(pdf_url)), filetype="pdf")

    text = "\n".join(page.get_text() for page in pdf)
    lines = [l.strip() for l in text.split("\n") if l.strip()]

    if len(lines) < 3:
        return None

    target = lines[-3]

    totale=lines[-2].replace(".", "").replace(",", ".")

    if float(totale) > 1E6:
        target = lines[-4]

    pattern = r"\d{1,3}(?:\.\d{3})*,\d{2}"

    numbers = re.findall(pattern, target)

    if not numbers:
        numbers = re.findall(pattern, lines[-5])

    numbers = [float(item.replace(".", "").replace(",", ".")) for item in numbers]

    if any(x < 10 and x != 0 for x in numbers):
        target = lines[-5]
        numbers = re.findall(r"\d{1,3}(?:\.\d{3})*,\d{2}", target)
        numbers = [float(item.replace(".", "").replace(",", ".")) for item in numbers]

    if len(numbers) < 2:
        return None

    return [n for n in numbers]

# =========================
# SCRAPER SINGLE
# =========================

def scrape_all_single(name, progress_bar=None, status=None):

    rows = []

    profile = find_profile(name)
    if not profile:
        return pd.DataFrame()

    pdfs = get_pdf_links(profile)

    total = len(pdfs)

    if status:
        status.write(f"Analizzando i compensi di {name}")

    with ThreadPoolExecutor(max_workers=8) as ex:

        futures = {ex.submit(extract_numbers, p): p for p in pdfs}

        for i, f in enumerate(as_completed(futures), start=1):

            pdf = futures[f]

            try:
                values = f.result()
                if not values:
                    continue

                date = datetime.strptime(pdf[-29:-21], "%Y%m%d")

                rows.append({
                    "Data": date,
                    "Lordo (€)": values[1],
                    "Ritenuta (€)": values[0],
                    "Netto (€)": values[1] - values[0],
                    "Cedolino": pdf
                })

            except:
                continue

            if progress_bar:
                progress_bar.progress(i / total)

    df = pd.DataFrame(rows)

    df["Data"] = pd.to_datetime(df["Data"])

    df = df.sort_values("Data", ascending=False)

    df = df.reset_index(drop=True)

    df["Mese"] = df["Data"].dt.to_period("M").astype(str)

    df["Data"] = df["Data"].dt.date

    cols = ["Mese"] + [c for c in df.columns if c != "Mese"]
    df = df[cols]

    netto_totale = df["Netto (€)"].sum()
    lordo_totale = df["Lordo (€)"].sum()
    
    return df, netto_totale, lordo_totale

# =========================
# STREAMLIT UI
# =========================

st.set_page_config(page_title="Piemonte Politics Costs", layout="wide")

st.title("🏛️ Tracciatore dei compensi dei Consiglieri Regionali - XII Legislatura (2024-2029)")

st.write("Il codice analizza i dati dei cedolini dei Consiglieri Regionali Piemontesi (XII Legislatura) disponibili sul sito web del Consiglio Regionale del Piemonte: https://www.cr.piemonte.it.")

@st.cache_data(show_spinner=False)
def get_councillors():
    url = "https://www.cr.piemonte.it/cms/consiglieri"
    return pd.read_html(url)[0]

df_list = get_councillors()

df_list.loc[len(df_list)] = ["Raffaele Gallo (XI)", "Partito Democratico", "0%", "0%"]

names = df_list["Nominativo"].tolist()

selected = st.selectbox("Seleziona (ex) Consigliere", names)

run = st.button("Analizza", type="primary")

if run:

    progress = st.progress(0)
    status = st.empty()

    df, netto_totale, lordo_totale = scrape_all_single(selected, progress, status)

    progress.empty()
    status.empty()

    if df.empty:
        st.warning("Nessun dato trovato")
        st.stop()

    st.success(f"Records trovati: {len(df)}")
    
    # =========================
    # 🧑 INFO CONSIGLIERE
    # =========================

    photo = get_photo(find_profile(selected))
    group = get_group(selected)

    votes, preferences = get_presences(selected)
    

    col1, col2, col3, col4, col5, col6 = st.columns([1, 2, 2, 2, 2, 2])

    with col1:
        if photo:
            st.image(photo, width=180)
        else:
            st.info("Foto non disponibile")

    with col2:
        st.subheader(selected)
        st.write(f"🏛️ **Gruppo politico:** {group if group else 'N/D'}")

    with col3:
        st.metric("Lordo totale in attività (€)", f"{lordo_totale:,.2f}")

    with col4:
        st.metric("Netto totale in attività (€)", f"{netto_totale:,.2f}")
    
    with col5:
        st.metric("Partecipazione ai voti (XII Legislatura)", f"{votes}%")
    
    with col6:
        st.metric("Partecipazione alle sedute (XII Legislatura)", f"{preferences}%")

    st.divider()

    st.dataframe(
    df,
    use_container_width=True,
    column_config={
        "Cedolino": st.column_config.LinkColumn(
            "Cedolino PDF"
        )
    }
)

    # =========================
    # CHART
    # =========================

    df["Data"] = pd.to_datetime(df["Data"])
    df["Mese"] = df["Data"].dt.to_period("M").astype(str)

    monthly = df.groupby("Mese")["Netto (€)"].sum().reset_index()

    st.subheader("📊 Rimborso mensile (Netto)")
    st.bar_chart(monthly.set_index("Mese"))
