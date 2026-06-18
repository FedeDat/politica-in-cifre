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
# SESSION (robust)
# =========================

def create_session():

    retry = Retry(
        total=5,
        backoff_factor=1,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=["GET", "HEAD"]
    )

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0"
    })

    session.mount("https://", HTTPAdapter(max_retries=retry))

    return session


SESSION = create_session()


# =========================
# HTML + PDF DOWNLOADS
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
# PROFILE FINDER
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

        except Exception:
            continue

    return None


# =========================
# PHOTO EXTRACTION
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

    pdf = fitz.open(
        stream=io.BytesIO(get_pdf_bytes(pdf_url)),
        filetype="pdf"
    )

    text = "\n".join(page.get_text() for page in pdf)
    lines = [l.strip() for l in text.split("\n") if l.strip()]

    if len(lines) < 5:
        return None

    target = lines[-3]

    try:
        total = float(lines[-2].replace(".", "").replace(",", "."))
        if total > 1e6:
            target = lines[-4]
    except:
        pass

    pattern = r"\d{1,3}(?:\.\d{3})*,\d{2}"

    numbers = re.findall(pattern, target)

    if not numbers:
        numbers = re.findall(pattern, lines[-5])

    values = [
        float(x.replace(".", "").replace(",", "."))
        for x in numbers
    ]

    if len(values) < 2:
        return None

    return values


# =========================
# MAIN SCRAPER
# =========================

def scrape_all(progress_bar=None, status=None):

    url = "https://www.cr.piemonte.it/cms/consiglieri"
    df = pd.read_html(url)[0]

    rows = []
    total = len(df)

    for i, row in enumerate(df.itertuples(index=False), start=1):

        name = row.Nominativo
        group = getattr(row, "Gruppo consiliare", None) or row[1]

        if status:
            status.write(f"Scraping **{name}**")

        profile = find_profile(name)

        if not profile:
            continue

        photo = get_photo(profile)
        pdfs = get_pdf_links(profile)

        with ThreadPoolExecutor(max_workers=8) as ex:

            futures = {ex.submit(extract_numbers, p): p for p in pdfs}

            for f in as_completed(futures):

                pdf = futures[f]

                try:
                    values = f.result()
                    if not values:
                        continue

                    date = datetime.strptime(pdf[-29:-21], "%Y%m%d").date()

                    rows.append({
                        "Nominativo": name,
                        "Gruppo consiliare": group,
                        "Photo": photo,
                        "Data": date,
                        "Lordo (€)": values[1],
                        "Ritenuta (€)": values[0],
                        "Netto (€)": values[1] - values[0],
                        "PDF": pdf
                    })

                except:
                    continue

        if progress_bar:
            progress_bar.progress(i / total)

    return pd.DataFrame(rows)


# =========================
# STREAMLIT UI
# =========================

st.set_page_config(page_title="Piemonte Politics Costs", layout="wide")

st.title("🏛️ Piemonte Politics Cost Tracker")

st.write("Scraping pubblici compensi consiglieri regionali")

if st.button("Start scraping", type="primary"):

    progress = st.progress(0)
    status = st.empty()

    with st.spinner("Running scraper..."):

        df = scrape_all(progress, status)

    progress.empty()
    status.empty()

    st.success(f"Done! {len(df)} records extracted.")

    st.dataframe(df, use_container_width=True)

    st.download_button(
        "Download CSV",
        df.to_csv(index=False).encode("utf-8"),
        "piemonte_salary.csv",
        "text/csv"
    )
