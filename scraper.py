import io
import re
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin

import fitz
import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# =========================
# CONFIG
# =========================

BASE_URL = "https://www.cr.piemonte.it/cms/legislatura-xii/consiglieri/"


# =========================
# SESSION WITH RETRY
# =========================

def create_session():
    """
    Create a robust HTTP session with retries.
    """

    retry = Retry(
        total=5,
        backoff_factor=1,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=["GET", "HEAD"]
    )

    adapter = HTTPAdapter(max_retries=retry)

    session = requests.Session()

    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
        )
    })

    session.mount("https://", adapter)

    return session


SESSION = create_session()


# =========================
# DOWNLOAD HELPERS (cached)
# =========================

@st.cache_data(show_spinner=False)
def get_html(url: str) -> str:
    """
    Download HTML with caching.
    """

    r = SESSION.get(url, timeout=30)
    r.raise_for_status()
    return r.text


@st.cache_data(show_spinner=False)
def get_pdf_bytes(url: str) -> bytes:
    """
    Download PDF with caching.
    """

    r = SESSION.get(url, timeout=60)
    r.raise_for_status()
    return r.content


# =========================
# PROFILE DISCOVERY
# =========================

def find_profile_url(name: str):
    """
    Try multiple slug combinations to find valid politician profile.
    """

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

        url = BASE_URL + slug

        try:
            r = SESSION.head(url, allow_redirects=True, timeout=10)

            if r.status_code == 200:
                return slug, url

        except requests.RequestException:
            continue

    return None, None


# =========================
# PHOTO EXTRACTION
# =========================

def get_photo_url(profile_url: str):

    html = get_html(profile_url)
    soup = BeautifulSoup(html, "html.parser")

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

            if src and not any(
                x in src.lower()
                for x in ["logo", "icon", "banner"]
            ):
                img_src = src
                break

    if img_src:
        return urljoin(profile_url, img_src)

    return None

# =========================
# PDF LINKS EXTRACTION
# =========================

def get_pdf_links(profile_url: str):

    url = profile_url + "/cedolini"

    html = get_html(url)
    soup = BeautifulSoup(html, "html.parser")

    links = set()

    for a in soup.find_all("a", href=True):

        href = a["href"]

        if "cedolini-batch" in href and href.lower().endswith(".pdf"):

            links.add(urljoin(url, href))

    return sorted(links, reverse=True)


# =========================
# PDF PARSER
# =========================

def extract_salary_numbers(pdf_url: str):

    pdf_bytes = get_pdf_bytes(pdf_url)

    doc = fitz.open(stream=io.BytesIO(pdf_bytes), filetype="pdf")

    text = "\n".join(page.get_text() for page in doc)

    lines = [l.strip() for l in text.split("\n") if l.strip()]

    if len(lines) < 5:
        return None

    target = lines[-3]

    try:
        total = float(
            lines[-2]
            .replace(".", "")
            .replace(",", ".")
        )

        if total > 1e6:
            target = lines[-4]

    except Exception:
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
# DATE PARSER
# =========================

def extract_date_from_url(pdf_url: str):
    """
    Extract YYYYMMDD from PDF URL.
    """

    try:
        return datetime.strptime(
            pdf_url[-29:-21],
            "%Y%m%d"
        ).date()
    except Exception:
        return None

%%time
import sys
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin
import re
import fitz  # PyMuPDF
import io
from datetime import datetime
import pandas as pd

df_salary = pd.DataFrame(columns=["Nominativo", "Gruppo consiliare", "Legislatura", "Data", "Lordo (€)", "Ritenuta (€)","Netto (€)"])

# NOTE: Make sure 'gruppo' column exists in your original 'df' 
# or change df_gruppo_fallback below to a default string like "Sconosciuto"
for _, row in df.iterrows():
    politico_value = row["Nominativo"]
    # Fallback if 'Gruppo consiliare' column doesn't exist in your source df
    gruppo = row.get("Gruppo consiliare", "Sconosciuto") 
    
    print("\n" + "="*40)
    print("Checking", politico_value, "...")

    # Clean the string and split it into a list of words
    words = [word.lower() for word in politico_value.split()]

    # Base URL for the regional council
    base_url = "https://www.cr.piemonte.it/cms/legislatura-xii/consiglieri/"
    final_url = None
    chosen_id = None

    # List of ways to combine the words
    combinations = [
        "-".join(words),                # gian-luca-vignale
        "".join(words),                 # gianlucavignale
        "-".join(words[1:]),            # luca-vignale
        "-".join([words[0], words[-1]]) # gian-vignale
    ]

    # Loop through each combination to find a live website
    for combo in combinations:
        test_url = base_url + combo
        try:
            response = requests.head(test_url, allow_redirects=True, timeout=5)
            if response.status_code == 200:
                final_url = test_url
                chosen_id = combo  # Remember the working name pattern!
                break
        except requests.RequestException:
            continue

    # Output the result
    if final_url:
        print(f"Link profile: {final_url}")
    else:
        print(f"No active link was found for {politico_value}. Skipping...")
        continue # Safely moves to the next politician instead of crashing

    # Use the proven working ID to build your page paths
    url_pagina = final_url
    url_cedolini = f"{final_url}/cedolini"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }

    # === PHOTO EXTRACTION ===
    try:
        response = requests.get(url_pagina, headers=headers)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        main_content = soup.find("main") or soup.find("div", class_="scheda-consigliere") or soup.find("div", id="content")
        img_src = None

        if main_content:
            img_tag = main_content.find("img")
            if img_tag and img_tag.get("src"):
                img_src = img_tag["src"]

        if not img_src:
            for img in soup.find_all("img"):
                src = img.get("src", "")
                if "logo" not in src.lower() and "icon" not in src.lower() and src:
                    img_src = src
                    break

        if img_src:
            full_url = urljoin(url_pagina, img_src)
            print(f"Link foto: {full_url}")
        else:
            print("Foto non trovata.")
    except requests.exceptions.RequestException as e:
        print(f"Errore di rete foto: {e}")
        
    # === PDF COLLECTION ===
    try:
        response = requests.get(url_cedolini, headers=headers)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        pdf_links = sorted({
            urljoin(url_cedolini, a["href"])
            for a in soup.find_all("a", href=True)
            if "cedolini-batch" in a["href"] and a["href"].lower().endswith(".pdf")
        }, reverse=True)
    except requests.exceptions.RequestException as e:
        print(f"Errore di rete cedolini: {e}")
        continue

    # === EXTRACTION FUNCTION ===
    def extract_numbers(pdf_url):
        r = requests.get(pdf_url, headers=headers)
        r.raise_for_status()

        doc = fitz.open(stream=io.BytesIO(r.content), filetype="pdf")
        text = "\n".join(page.get_text() for page in doc)
        lines = [l.strip() for l in text.split("\n") if l.strip()]

        if len(lines) < 3:
            return None

        target = lines[-3]
        totale = lines[-2].replace(".", "").replace(",", ".")

        try:
            if float(totale) > 1E6:
                target = lines[-4]
        except ValueError:
            pass

        numbers = re.findall(r"\d{1,3}(?:\.\d{3})*,\d{2}", target)
        
        if not numbers:
            numbers = re.findall(r"\d{1,3}(?:\.\d{3})*,\d{2}", lines[-5])
        
        # Convert found strings to clean floats
        numbers = [float(item.replace(".", "").replace(",", ".")) for item in numbers]

        if any(x < 10 and x != 0 for x in numbers):
            target = lines[-5]
            numbers = re.findall(r"\d{1,3}(?:\.\d{3})*,\d{2}", target)
            numbers = [float(item.replace(".", "").replace(",", ".")) for item in numbers]

        return numbers

    # === PROCESS ALL PDF LINKS FOR THIS POLITICIAN ===
    for link in pdf_links:
        try:
            values = extract_numbers(link)
            if not values or len(values) < 2:
                raise ValueError("Could not find both gross and tax values in PDF")
                
            date = datetime.strptime(link[85:93], "%Y%m%d").date()
            
            # Calculate values safely
            lordo = values[1]
            ritenuta = values[0]
            netto = lordo - ritenuta
            
            df_salary.loc[len(df_salary)] = [
                politico_value, 
                gruppo, 
                link[66:72], 
                date, 
                lordo, 
                ritenuta, 
                netto
            ]
        except Exception as e:
            print(f"Failed on link: {link} | Reason: {e}")
    
    print(f"Salaries successfully extracted for {politico_value}.")
