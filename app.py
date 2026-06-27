import io
import re
import streamlit as st
import pandas as pd
import requests
import fitz
import zipfile
import unicodedata

from datetime import datetime, date
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin
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

# =========================
# DATA DI NASCITÀ
# =========================

mesi = {
    "gennaio": 1,
    "febbraio": 2,
    "marzo": 3,
    "aprile": 4,
    "maggio": 5,
    "giugno": 6,
    "luglio": 7,
    "agosto": 8,
    "settembre": 9,
    "ottobre": 10,
    "novembre": 11,
    "dicembre": 12,
}

def estrai_data_nascita(url):

    soup = BeautifulSoup(get_html(url), "html.parser")

    testo = soup.get_text(" ", strip=True)

    m = re.search(
        r"Nat[oa].*?(\d{1,2})\s*[°º]?\s+"
        r"(gennaio|febbraio|marzo|aprile|maggio|giugno|luglio|agosto|settembre|ottobre|novembre|dicembre)"
        r"\s+(\d{4})",
        testo,
        flags=re.IGNORECASE | re.DOTALL,
    )

    if not m:
        return None

    return date(
        int(m.group(3)),
        mesi[m.group(2).lower()],
        int(m.group(1))
    )

def _birth_worker(nome):

    try:
        profile = find_profile(nome)

        if not profile:
            return None

        nascita = estrai_data_nascita(profile)

        if not nascita:
            return None

        oggi = date.today()

        eta = (
            oggi.year
            - nascita.year
            - ((oggi.month, oggi.day) < (nascita.month, nascita.day))
        )

        return {
            "Consigliere": nome,
            "Gruppo": get_group(nome),
            "Data di nascita": nascita.strftime("%d/%m/%Y"),
            "Età": eta
        }

    except Exception:
        return None


@st.cache_data(show_spinner=True)
def get_birthdays_table():

    nomi = get_councillors()["Nominativo"].tolist()

    risultati = []

    with ThreadPoolExecutor(max_workers=10) as ex:

        futures = [ex.submit(_birth_worker, n) for n in nomi]

        for f in as_completed(futures):

            r = f.result()

            if r:
                risultati.append(r)

    return (
        pd.DataFrame(risultati)
        .sort_values("Età", ascending=False)
        .reset_index(drop=True)
    )

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
# Load data (cached) per Consigliature Comunali
# =========================
@st.cache_data
def load_data():
    base_url = "https://dait.interno.gov.it/documenti/"
    target_url = "ammcom.csv"
    csv_url = base_url + target_url

    df = pd.read_csv(
        csv_url,
        sep=";",
        skiprows=2,
        quotechar='"',
        encoding="utf-8",
        low_memory=False
    )
    return df

# --- ELENCHI UFFICIALI (MAPPATURA DELLE TIPOLOGIE) ---
CITTA_METROPOLITANE = [
    "Bari", "Bologna", "Cagliari", "Catania", "Firenze", "Genova", 
    "Messina", "Milano", "Napoli", "Palermo", "Reggio Calabria", 
    "Roma", "Torino", "Venezia"
]

CAPOLUOGHI_REGIONE = [
    "Ancona", "Aosta", "L'Aquila", "Bari", "Bologna", "Cagliari", "Catanzaro", 
    "Campobasso", "Firenze", "Genova", "Milano", "Napoli", "Palermo", "Perugia", 
    "Potenza", "Roma", "Torino", "Trento", "Trieste", "Venezia"
]

# Elenco esemplificativo dei principali capoluoghi di provincia ordinari (escluse città metropolitane e regionali)
CAPOLUOGHI_PROVINCIA = [
    "Alessandria", "Asti", "Biella", "Cuneo", "Novara", "Verbania", "Vercelli",
    "Bergamo", "Brescia", "Como", "Cremona", "Lecco", "Lodi", "Mantova", "Monza", "Pavia", "Sondrio", "Varese",
    "Bolzano", "Belluno", "Padova", "Rovigo", "Treviso", "Verona", "Vicenza",
    "Gorizia", "Pordenone", "Udine", "Imperia", "La Spezia", "Savona",
    "Ferrara", "Forlì", "Modena", "Parma", "Piacenza", "Ravenna", "Reggio Emilia", "Rimini",
    "Arezzo", "Grosseto", "Livorno", "Lucca", "Massa", "Pisa", "Pistoia", "Prato", "Siena",
    "Terni", "Pesaro", "Macerata", "Fermo", "Ascoli Piceno",
    "Viterbo", "Rieti", "Frosinone", "Latina", "Chieti", "Pescara", "Teramo",
    "Isernia", "Avellino", "Benevento", "Caserta", "Salerno",
    "Foggia", "Taranto", "Brindisi", "Lecce", "Andria", "Barletta", "Trani",
    "Matera", "Cosenza", "Crotone", "Vibo Valentia",
    "Trapani", "Agrigento", "Caltanissetta", "Enna", "Ragusa", "Siracusa",
    "Sassari", "Nuoro", "Oristano"
]

CITTA_METROPOLITANE_SET = {c.upper() for c in CITTA_METROPOLITANE}
CAPOLUOGHI_REGIONE_SET = {c.upper() for c in CAPOLUOGHI_REGIONE}
CAPOLUOGHI_PROVINCIA_SET = {c.upper() for c in CAPOLUOGHI_PROVINCIA}

# --- FUNZIONE DI CALCOLO CORE ---
def calcola_indennita_amministratori(popolazione, comune_selezionato):
    """
    Calcola le indennità in base alle tabelle allegate applicando la gerarchia istituzionale:
    Città Metropolitana -> Capoluogo di Regione -> Capoluogo di Provincia -> Comune Ordinario
    """
    sindaco = (0.0, 0.0)
    vicesindaco = (0.0, 0.0)
    assessore = (0.0, 0.0)
    pres_consiglio = (0.0, 0.0)
    cons_gettone = (0.0, 0.0)
    cons_max = (0.0, 0.0)

    # 1. CITTÀ METROPOLITANE
    if comune_selezionato in CITTA_METROPOLITANE:
        sindaco = (7018.65, 13800.00)
        vicesindaco = (5263.99, 10350.00)
        assessore = (4562.12, 8970.00)
        pres_consiglio = (4562.12, 8970.00)
        cons_gettone = (92.96, 92.96)
        cons_max = (1754.66, 3450.00)

    # 2. CAPOLUOGHI DI REGIONE
    elif comune_selezionato in CAPOLUOGHI_REGIONE:
        sindaco = (7018.65, 11040.00)
        vicesindaco = (5263.99, 8280.00)
        assessore = (4562.12, 7176.00)
        pres_consiglio = (4562.12, 7176.00)
        cons_gettone = (53.45, 53.45)
        cons_max = (1754.66, 2760.00)

    # 3. CAPOLUOGHI DI PROVINCIA
    elif comune_selezionato in CAPOLUOGHI_PROVINCIA:
        if popolazione <= 50000:
            sindaco = (3718.49, 9660.00)
            vicesindaco = (2788.87, 7245.00)
            assessore = (2231.09, 5796.00)
            pres_consiglio = (2231.09, 5796.00)
            cons_gettone = (32.53, 32.53)
            cons_max = (929.62, 1552.50)
        elif 50001 <= popolazione <= 100000:
            sindaco = (4508.67, 9660.00)
            vicesindaco = (3381.50, 7245.00)
            assessore = (2705.20, 5796.00)
            pres_consiglio = (2705.20, 5796.00)
            cons_gettone = (32.53, 32.53)
            cons_max = (1127.17, 1552.50)
        else:
            sindaco = (5205.89, 11040.00)
            vicesindaco = (3904.42, 8280.00)
            assessore = (3383.83, 7176.00)
            pres_consiglio = (3383.83, 7176.00)
            cons_gettone = (32.53, 32.53)
            cons_max = (1301.47, 2760.00)

    # 4. COMUNI ORDINARI
    else:
        if popolazione <= 1000:
            sindaco = (1659.38, 2208.00)
            vicesindaco = (174.30, 331.20)
            assessore = (116.20, 220.80)
            pres_consiglio = (58.10, 110.40)
            cons_gettone = (15.34, 15.34)
            cons_max = (414.84, 552.00)
        elif 1001 <= popolazione <= 3000:
            sindaco = (1659.38, 2208.00)
            vicesindaco = (260.30, 441.60)
            assessore = (195.22, 331.20)
            pres_consiglio = (130.15, 220.80)
            cons_gettone = (15.34, 15.34)
            cons_max = (414.84, 552.00)
        elif 3001 <= popolazione <= 5000:
            sindaco = (1952.21, 3036.00)
            vicesindaco = (390.44, 607.20)
            assessore = (292.83, 455.40)
            pres_consiglio = (195.22, 303.60)
            cons_gettone = (16.27, 16.27)
            cons_max = (488.05, 759.00)
        elif 5001 <= popolazione <= 10000:
            sindaco = (2509.98, 4002.00)
            vicesindaco = (1254.99, 2001.00)
            assessore = (1129.49, 1800.90)
            pres_consiglio = (251.00, 400.20)
            cons_gettone = (16.27, 16.27)
            cons_max = (627.22, 1000.50)
        elif 10001 <= popolazione <= 15000:
            sindaco = (2788.87, 4140.00)
            vicesindaco = (1533.88, 2277.00)
            assessore = (1254.99, 1863.00)
            pres_consiglio = (251.00, 400.20)
            cons_gettone = (19.99, 19.99)
            cons_max = (697.22, 1035.50)
        elif 15001 <= popolazione <= 30000:
            sindaco = (2788.87, 4140.00)
            vicesindaco = (1533.88, 2277.00)
            assessore = (1254.99, 1863.00)
            pres_consiglio = (1254.99, 1863.00)
            cons_gettone = (19.99, 19.99)
            cons_max = (697.22, 1035.50)
        elif 30001 <= popolazione <= 50000:
            sindaco = (3114.23, 4830.00)
            vicesindaco = (1712.83, 2656.50)
            assessore = (1401.40, 2173.50)
            pres_consiglio = (1401.40, 2173.50)
            cons_gettone = (32.53, 32.53)
            cons_max = (778.56, 1207.50)
        elif 50001 <= popolazione <= 100000:
            sindaco = (3718.49, 6210.00)
            vicesindaco = (2788.87, 4657.50)
            assessore = (2231.09, 3726.00)
            pres_consiglio = (2231.09, 3726.00)
            cons_gettone = (32.53, 32.53)
            cons_max = (929.62,1552.50)
        elif 100001 <= popolazione <= 250000:
            sindaco = (4508.67, 6210.00)
            vicesindaco = (3381.50, 4657.50)
            assessore = (2705.20, 3726.00)
            pres_consiglio = (2705.20, 3726.00)
            cons_gettone = (32.53, 32.53)
            cons_max = (1301.47, 2760.00)
        elif 250001 <= popolazione <= 500000:
            sindaco = (4508.67, 6210.00)
            vicesindaco = (3381.50, 4657.50)
            assessore = (2705.20, 3726.00)
            pres_consiglio = (2705.20, 3726.00)
            cons_gettone = (53.45, 53.45)
            cons_max = (1754.66, 2760.00)
        else:
            sindaco = (7018.65, 13800.00)
            vicesindaco = (5263.99, 10350.00)
            assessore = (4562.12, 8970.00)
            pres_consiglio = (4562.12, 8970.00)
            cons_gettone = (92.96, 92.96)
            cons_max = (1754.66, 3450.00)

    # Formattazione valuta IT
    def fmt(v):
        return f"€ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

    data = {
        "Carica": [
            "Sindaco", 
            "Vicesindaco", 
            "Assessore", 
            "Presidente del Consiglio Comunale", 
            "Consigliere (gettone di presenza)", 
            "Consigliere (Compenso mensile massimo)"
        ],
        "Ante 2024": [fmt(sindaco[0]), fmt(vicesindaco[0]), fmt(assessore[0]), fmt(pres_consiglio[0]), fmt(cons_gettone[0]), fmt(cons_max[0])],
        "Attuale": [fmt(sindaco[1]), fmt(vicesindaco[1]), fmt(assessore[1]), fmt(pres_consiglio[1]), fmt(cons_gettone[1]), fmt(cons_max[1])]
    }
    return pd.DataFrame(data)

def get_valid_municipality_url(city_name, province_code):
    """
    Constructs and checks standard formats for Italian municipality websites.
    Returns the URL string if found, otherwise returns None.
    """
    # Clean inputs (lowercase, remove spaces/accents if necessary)
    city = city_name.lower().replace(" ", "").strip()
    prov = province_code.lower().strip()
    
    # Standard Italian institutional URL patterns
    patterns = [
        f"https://www.comune.{city}.{prov}.it",
        f"https://www.comune.{city}.it",
        f"https://www.{city}.it"
    ]
    
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    
    for url in patterns:
        try:
            # Use HEAD request for speed; allow redirects to find the final URL
            response = requests.head(url, headers=headers, timeout=5, allow_redirects=True)
            
            if response.status_code < 400:
                return response.url  # Restituisce solo la stringa dell'URL trovato
                
        except requests.RequestException:
            continue # Try the next pattern if this one fails
            
    return None  # Restituisce None se nessun URL funziona

# =========================
# STREAMLIT UI
# =========================

st.set_page_config(page_title="La politica italiana in cifre", layout="wide")

st.title("🏛️ La politica italiana in cifre")

@st.cache_data(show_spinner=False)
def get_councillors():
    url = "https://www.cr.piemonte.it/cms/consiglieri"
    return pd.read_html(url)[0]

df_list = get_councillors()

df_birth = get_birthdays_table()

eta_media = round(df_birth["Età"].mean())

#df_list.loc[len(df_list)] = ["Raffaele Gallo", "Partito Democratico", "0%", "0%", "None"]

names = df_list["Nominativo"].tolist()

st.sidebar.title("Navigazione")

pagina = st.sidebar.selectbox(
    "Seleziona sezione",
    [
        "Home",
        "Analizzatore Cedolini",
        "Anagrafica Consiglieri Regionali",
        "Anagrafica Organi Comunali",
        "Comuni italiani per popolazione"
    ]
)

def pagina_home():
    
    st.markdown("""
### 📖 La politica italiana raccontata attraverso i dati

**Politica in Cifre** è una piattaforma che raccoglie, elabora e visualizza dati pubblici provenienti da fonti istituzionali italiane, con l'obiettivo di favorire una conoscenza più approfondita delle istituzioni e degli amministratori pubblici.

L'applicazione integra automaticamente informazioni provenienti da siti istituzionali, open data e documentazione pubblica, trasformandole in indicatori statistici e strumenti di consultazione accessibili.
""")

    st.divider()

    col1, col2 = st.columns(2)

    with col1:

        st.subheader("🏛️ Consiglio Regionale del Piemonte")

        st.write("Sono disponibili strumenti per:")

        st.markdown("""
- Analizzare i cedolini dei Consiglieri Regionali.
- Consultare dati anagrafici e demografici.
- Analizzare presenze e partecipazione ai lavori del Consiglio.
- Ricostruire l'evoluzione dei compensi nel tempo.
- Esplorare statistiche aggregate della XII Legislatura.
""")

    with col2:

        st.subheader("🏘️ Comuni Italiani")

        st.write("Sono disponibili strumenti per:")

        st.markdown("""
- Analizzare Sindaci, Giunte e Consigli Comunali.
- Calcolare indicatori demografici degli amministratori.
- Misurare la rappresentanza di genere e degli Under 35.
- Consultare le indennità previste dalla normativa nazionale.
- Visualizzare la composizione politica degli organi comunali.
- Analizzare l'andamento della popolazione nel tempo.
""")

    st.divider()

    st.subheader("📊 Fonti dei dati")

    st.write(
        "Le analisi sono elaborate esclusivamente a partire da dati pubblici "
        "e documentazione ufficiale."
    )

    st.markdown("""
- **Consiglio Regionale del Piemonte:** [https://www.cr.piemonte.it/cms/](https://www.cr.piemonte.it/cms/).
- **Dipartimento per gli Affari Interni e Territoriali (Ministero dell'Interno):** [https://dait.interno.gov.it/contenuti/tipo/open_data](https://dait.interno.gov.it/contenuti/tipo/open_data).
- **De Carlo, E. (2021).** *Vademecum dell'amministratore locale. Status, funzioni, competenze, responsabilità dei sindaci, amministratori e consiglieri comunali*. Halley Editrice.
- **Dati ISTAT sulla popolazione residente:** [https://demo.istat.it/](https://demo.istat.it/).
""")

    st.divider()

    st.subheader("⚙️ Metodologia")

    st.write(
        "L'applicazione automatizza l'acquisizione e l'elaborazione delle "
        "informazioni pubblicate dagli enti istituzionali."
    )

    st.markdown("""
- Acquisizione automatica dei dati da fonti ufficiali.
- Estrazione di informazioni da pagine web e documenti PDF.
- Elaborazione di indicatori statistici.
- Presentazione dei risultati tramite dashboard interattive.

Tutte le elaborazioni vengono effettuate esclusivamente su dati pubblicamente disponibili.
""")

    st.divider()

    st.subheader("🔄 Aggiornamento dei dati")

    st.write("""
Le informazioni vengono aggiornate periodicamente in funzione della disponibilità dei dati pubblicati dagli enti di riferimento.

La tempestività degli aggiornamenti dipende pertanto dalla frequenza di pubblicazione delle fonti ufficiali.
""")

    st.divider()

    st.subheader("⚠️ Limitazioni")

    st.write("""
L'applicazione automatizza l'estrazione di dati pubblici che possono essere modificati dagli enti senza preavviso.

In presenza di variazioni nei siti istituzionali o nei documenti pubblicati, alcune informazioni potrebbero risultare temporaneamente incomplete o non aggiornate.
""")

    st.divider()

    st.subheader("📄 Disclaimer")

    st.write("""
Il progetto ha esclusivamente finalità informative, divulgative e di analisi dei dati.

Non costituisce una pubblicazione ufficiale delle amministrazioni interessate.

Gli utenti sono invitati a verificare sempre le informazioni presso le fonti istituzionali competenti prima di farne qualsiasi utilizzo.
""")

    st.divider()

    st.subheader("📧 Contatti")

    st.write(
        "Per informazioni, suggerimenti o segnalazioni di bug è possibile contattare "
        "**Federico Dattila**[federico.dattila@gmail.com](mailto:federico.dattila@gmail.com)."
    )

def pagina_cedolini_regione():
    st.subheader("📊 Analizzatore Cedolini Consiglio Regionale del Piemonte - XII Legislatura (2024-2029)", divider=True)

    st.write("Il codice analizza i dati dei Consiglieri Regionali Piemontesi (XII Legislatura) presenti sul sito web del Consiglio Regionale del Piemonte: https://www.cr.piemonte.it.")
    
    selected = st.selectbox("Seleziona Consigliere", names)
    
    run = st.button("Analizza i cedolini", type="primary")
    
    if run:
    
        progress = st.progress(0)
        status = st.empty()
    
        df, netto_totale, lordo_totale = scrape_all_single(selected, progress, status)
    
        progress.empty()
        status.empty()
    
        if df.empty:
            st.warning("Nessun dato trovato")
            st.stop()
    
        st.success(f"Cedolini trovati: {len(df)}")
        
        # =========================
        # 🧑 INFO CONSIGLIERE
        # =========================
    
        photo = get_photo(find_profile(selected))
        group = get_group(selected)
    
        votes, preferences = get_presences(selected)
    
        data_nascita = estrai_data_nascita(find_profile(selected))
    
        if data_nascita:
            oggi = date.today()
    
            eta = (
                oggi.year
                - data_nascita.year
                - ((oggi.month, oggi.day) < (data_nascita.month, data_nascita.day))
            )
    
        # Età media del Consiglio
        eta_media = round(df_birth["Età"].mean())
    
        # Scostamento dalla media
        differenza = eta - eta_media

        st.info("ℹ️ Dati anagrafici, di partecipazione ed economici estratti automaticamente da dati pubblici online. Si prega di verificare correttezza prima di ulteriore utilizzo.")
        
        col1, col2, col3, col4 = st.columns([1, 2, 2, 2])
    
        with col1:
            if photo:
                st.image(photo, width=180)
            else:
                st.info("Foto non disponibile")
    
        with col2:
            st.subheader(selected)
            st.write(f"🏛️ **Gruppo politico:** {group if group else 'N/D'}")
            st.write(f"📅 **{data_nascita.strftime('%d/%m/%Y')}** - 🎂 **{eta} anni**")
    
            if differenza > 0:
                st.write(f"{differenza} anni oltre la media del Consiglio ({eta_media} anni)")
            elif differenza < 0:
                st.write(f"{abs(differenza)} anni al di sotto della media del Consiglio ({eta_media} anni)")
            else:
                st.write(f"In linea con l'età media del Consiglio ({eta_media} anni)")
    
            data_inizio = df.iloc[-1]["Data"]
    
            if data_inizio >= date(2024, 8, 1):
                legislatura = "Consigliere dal 2024 (Legislatura XII)"
            else:
                legislatura = "Consigliere dal 2019 (Legislature XI e XII)"
            st.write(legislatura)
    
        with col3:
            st.metric("Lordo totale in attività (€)", f"{lordo_totale:,.2f}")
            st.metric("Netto totale in attività (€)", f"{netto_totale:,.2f}")
            st.write(f"Cedolini accessibili dal {df.iloc[-1]["Data"].strftime("%d-%m-%Y")}")
        
        with col4:
            st.metric("Partecipazione ai voti (XII Legislatura)", f"{votes}%")
            st.metric("Partecipazione alle sedute (XII Legislatura)", f"{preferences}%")
    
        st.divider()
    
        st.dataframe(
        df,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Cedolino": st.column_config.LinkColumn(
                "Cedolino PDF"
            )
        }
    )

        st.info("ℹ️ Una quota del compenso mensile può essere destinata al partito politico di appartenenza o al rimborso delle spese per collaboratori.")

        # =========================
        # CHART
        # =========================
    
        df["Data"] = pd.to_datetime(df["Data"])
        df["Mese"] = df["Data"].dt.to_period("M").astype(str)
    
        monthly = df.groupby("Mese")["Netto (€)"].sum().reset_index()
    
        st.markdown("**Rimborso mensile (Netto)**")
        st.bar_chart(monthly.set_index("Mese"))

        # Titolo principale
        st.subheader("Trattamento Economico Consiglieri Regionali Piemonte")
    
        st.write(
            "La disciplina del trattamento economico dei Consiglieri con gli importi per voci disaggregate "
            "è tratta dal link: "
            "[https://www.cr.piemonte.it/cms/amministrazione-trasparente/organizzazione/titolari-di-incarichi-politici-di-amministrazione-di](https://www.cr.piemonte.it/cms/amministrazione-trasparente/organizzazione/titolari-di-incarichi-politici-di-amministrazione-di)."
        )
    
        # 1. TABELLA GENERALE DEI COMPENSI E RIMBORSI
        st.markdown("**1. Voci Principali del Trattamento Economico**")
    
        dati_generali = {
            "Voce Economica": [
                "Indennità di carica",
                "Rimborso Spese Mandato",
                "Indennità Fine Mandato",
                "Assegno Vitalizio"
            ],
            "Importo / Regola": [
                "€ 5.000,00 lordi mensili",
                "€ 3.500,00 fissi mensili",
                "Ultima mensilità lorda × anni di mandato",
                "Abrogato dalla X legislatura"
            ],
            "Tassazione IRPEF": [
                "Sì (Interamente assoggettata)",
                "No (Detassato ex art. 52 TUIR)",
                "Secondo normativa vigente",
                "Soggetto a ricalcolo contributivo"
            ],
            "Note / Penali": [
                "Valido per membri Consiglio e Giunta",
                "Taglio di € 150 per ogni assenza ingiustificata",
                "Massimo 10 anni anche non consecutivi",
                "Solo per legislature precedenti alla X"
            ]
        }
    
        df_generale = pd.DataFrame(dati_generali)
        st.dataframe(df_generale, use_container_width=True, hide_index=True)
    
        # 2. TABELLA DETTAGLIO INDENNITÀ DI FUNZIONE
        st.markdown("**2. Dettaglio Indennità di Funzione (Aggiuntive)**")
        st.write("Importi lordi mensili cumulabili in base alla carica ricoperta:")
    
        dati_funzioni = {
            "Carica Ricoperta": [
                "Presidente della Giunta regionale",
                "Presidente del Consiglio regionale",
                "Vicepresidente della Giunta regionale",
                "Vicepresidenti del Consiglio regionale",
                "Assessori regionali",
                "Presidenti di Gruppo consiliare",
                "Consiglieri segretari Ufficio di Presidenza",
                "Presidenti Commissioni Permanenti / Speciali / Giunte",
                "Vicepresidenti Commissioni Permanenti / Speciali / Giunte",
                "Consiglieri segretario della Giunta per le elezioni"
            ],
            "Importo Lordo Mensile": [
                "€ 1.700,00",
                "€ 1.700,00",
                "€ 1.250,00",
                "€ 1.250,00",
                "€ 1.250,00",
                "€ 1.000,00",
                "€ 750,00",
                "€ 750,00",
                "€ 600,00",
                "€ 600,00"
            ]
        }

        df_funzioni = pd.DataFrame(dati_funzioni)
        st.dataframe(df_funzioni, use_container_width=True, hide_index=True)

def pagina_anagrafiche_regione():
    st.subheader("Data di nascita ed età dei Consiglieri Regionali",divider=True)
    st.write("La lista di Consiglieri Regionali per Gruppo di appartenenza e età anagrafica è riportato di seguito.")
    
    st.dataframe(
        df_birth,
        use_container_width=True,
        hide_index=True
    )

def pagina_anagrafiche_comuni():
    st.subheader("📊 Analizzatore Anagrafiche Organi Comunali (aggiornate al 6 giugno 2026)", divider=True)

    st.write("Il codice analizza i dati del Dipartimento per gli Affari Interni e Territoriale e restituisce anali della composizione delle Giunte e dei Consigli Comunali.")
    st.write("Dati estratti da [https://dait.interno.gov.it/elezioni/open-data/amministratori-locali-e-regionali-in-carica](https://dait.interno.gov.it/elezioni/open-data/amministratori-locali-e-regionali-in-carica).")
    
    df = load_data()
    
    # =========================
    # Municipality selection (no default city)
    # =========================
    comuni = sorted(df["denominazione_comune"].dropna().unique())
    
    comune = st.selectbox(
        "Seleziona o digita il Comune",
        options=comuni
    )
    
    # =========================
    # Filter
    # =========================
    df_comune = df.loc[df["denominazione_comune"].eq(comune)].copy()

    url_comune = get_valid_municipality_url(comune, df_comune["sigla_provincia"].iloc[0])

    popolazione = int(df_comune["popolazione_censita_alla_data_elezione"].iloc[0])
    
    if df_comune.empty:
        st.warning("Nessun dato disponibile per il comune selezionato.")
        st.stop()
    
    # =========================
    # Dates and age
    # =========================
    df_comune["data_nascita"] = pd.to_datetime(
        df_comune["data_nascita"],
        dayfirst=True,
        errors="coerce"
    )

    df_comune.loc[(df_comune["nome"] == "SIMONE") & (df_comune["cognome"] == "FISSOLO") & (df_comune["denominazione_comune"] == "TORINO"),
    "data_nascita"] = pd.Timestamp("1989-05-31")
    
    oggi = pd.Timestamp.today().normalize()
    
    df_comune["eta"] = (
        oggi.year
        - df_comune["data_nascita"].dt.year
        - (
            (oggi.month < df_comune["data_nascita"].dt.month)
            | (
                (oggi.month == df_comune["data_nascita"].dt.month)
                & (oggi.day < df_comune["data_nascita"].dt.day)
            )
        )
    ).astype("Int64")
    
    # =========================
    # Role handling
    # =========================
    df_comune["ruolo"] = df_comune["descrizione_carica"]
    
    mask = (
        df_comune["descrizione_carica"].eq("Assessore")
        & df_comune["incarico"].fillna("").str.contains("vicesindaco", case=False)
    )
    
    df_comune.loc[mask, "ruolo"] = "Vicesindaco"
    
    # =========================
    # Subsets
    # =========================

    subset = ["denominazione_comune", "cognome", "nome"]
    
    subset = ["denominazione_comune", "cognome", "nome"]
    
    sindaco = (
        df_comune[df_comune["ruolo"] == "Sindaco"]
        .drop_duplicates(subset=subset)
    )
    
    giunta = (
        df_comune[df_comune["ruolo"].isin(["Sindaco", "Vicesindaco", "Assessore"])]
        .drop_duplicates(subset=subset)
    )
    
    consiglio = (
        df_comune[df_comune["ruolo"].str.contains("Consigliere", na=False)]
        .drop_duplicates(subset=subset)
    )
    
    # =========================
    # Safe indicators
    # =========================
    eta_sindaco = sindaco["eta"].iloc[0] if not sindaco.empty else None
    sesso_sindaco = sindaco["sesso"].iloc[0] if not sindaco.empty else None
    
    eta_media_giunta = giunta["eta"].mean()
    eta_media_consiglio = consiglio["eta"].mean()
    
    
    def percentuali_sesso(df_tmp):
        if df_tmp.empty:
            return {"uomini": 0, "donne": 0}
    
        p = df_tmp["sesso"].value_counts(normalize=True).mul(100)
        return {
            "uomini": round(p.get("M", 0), 1),
            "donne": round(p.get("F", 0), 1),
        }
    
    
    def percentuale_under_35(df_tmp):
        if df_tmp.empty:
            return 0.0
        return round((df_tmp["eta"] < 35).mean() * 100, 1)
    
    perc_giunta = percentuali_sesso(giunta)
    perc_consiglio = percentuali_sesso(consiglio)
    
    under35_giunta = percentuale_under_35(giunta)
    under35_consiglio = percentuale_under_35(consiglio)
    
    # =========================
    # ROLE ORDER
    # =========================
    ordine_ruoli = {
        "Sindaco": 0,
        "Vicesindaco": 1,
        "Assessore": 2,
    }
    
    df_comune["ruolo_ordine"] = df_comune["ruolo"].map(ordine_ruoli).fillna(3)
    
    # =========================
    # Clean table
    # =========================
    df_comune["nome"] = df_comune["nome"].str.title()
    df_comune["cognome"] = df_comune["cognome"].str.title()
    
    df_ridotto = (
        df_comune[
            [
                "denominazione_comune",
                "nome",
                "cognome",
                "ruolo",
                "sesso",
                "eta",
                "lista_appartenenza/collegamento",
                "ruolo_ordine",
            ]
        ]
        .groupby(["denominazione_comune", "cognome", "nome"], as_index=False)
        .agg(
            {
                "ruolo": "first",
                "sesso": "first",
                "eta": "first",
                "ruolo_ordine": "first",
                "lista_appartenenza/collegamento": lambda s: s.dropna().iloc[0] if not s.dropna().empty else None,
            }
        )
        .rename(
            columns={
                "lista_appartenenza/collegamento": "Gruppo consiliare",
                "nome": "Nome",
                "cognome": "Cognome",
                "ruolo": "Carica",
                "sesso": "Genere",
                "eta": "Età",
            }
        )
        .sort_values(by=["ruolo_ordine", "Cognome", "Nome"], ignore_index=True)
        .drop(columns=["denominazione_comune", "ruolo_ordine"])
        .assign(
            **{
                "Gruppo consiliare": lambda x: x["Gruppo consiliare"].fillna("Non indicato")
            }
        )
    )
    
    # =========================
    # STREAMLIT OUTPUT
    # =========================
    st.subheader(f"Comune selezionato: {comune}")

    if len(df_ridotto) <= 1:
        st.warning("⚠️ Dati mancanti nel registro pubblico dei dati. Verificare esito dell'analisi.")
        
        if url_comune is not None:
            # URL exists: Display the info message with a clickable markdown link
            st.warning(f"Si suggerisce di verificare i dati sul sito web comunale: [{url_comune}]({url_comune})")
        else:
            # URL is None: Display alternative fallback text
            st.warning("Si suggerisce di verificare i dati sul sito web comunale.")

    st.metric(f"Popolazione residente (censimento ISTAT 2011)", popolazione if popolazione is not None else "N/D")
    
    col1, col2, col3, col4 = st.columns(4)
    
    col1.metric("Età Sindaco", eta_sindaco if eta_sindaco is not None else "N/D")
    col2.metric("Genere Sindaco", sesso_sindaco if sesso_sindaco is not None else "N/D")
    col3.metric("Età media Giunta", f"{eta_media_giunta:.1f}" if pd.notna(eta_media_giunta) else "N/D")
    col4.metric("Età media Consiglio", f"{eta_media_consiglio:.1f}" if pd.notna(eta_media_consiglio) else "N/D")
    
    st.write("### Parità di Genere")
    
    col1, col2 = st.columns(2)

    col1, col2 = st.columns(2)

    with col1:
        st.write(f"Giunta Comunale: {perc_giunta['uomini']}% uomini - {perc_giunta['donne']}% donne")
    
    with col2:
        st.write(f"Consiglio Comunale: {perc_consiglio['uomini']}% uomini - {perc_consiglio['donne']}% donne")

    st.write("### Rappresentatività Under 35") 
    
    col1, col2 = st.columns(2)

    with col1:
        st.metric(
            label="Giunta Comunale",
            value=f"{under35_giunta}%"
        )
    
    with col2:
        st.metric(
            label="Consiglio Comunale",
            value=f"{under35_consiglio}%"
        )
    
    st.write("### Tabella componenti")
    st.dataframe(df_ridotto, use_container_width=True, hide_index=True)

    st.subheader("💰 Indennità mensili lorde (massime) degli Amministratori Comunali")

    st.write("I commi da 583 a 587 della legge n. 234 del 30 dicembre 2021 (legge di Bilancio 2022) hanno previsto e finanziato (a carico statale) un incremento delle indennità di funzione.")
    st.write("L'incremento percentuale decorrente dal 2022 trova applicazione integrale (nella misura del 100%) a decorrere dall'anno 2024.")
    st.write("Informazioni tratte da De Carlo, E. (2021). Vademecum dell'amministratore locale. Status, funzioni, competenze, responsabilità dei sindaci, amministratori e consiglieri comunali. Matelica: Halley Editrice. ISBN: 9788875895419.")    
    # Rilevamento automatico della categoria per mostrare un badge informativo
    if comune in CITTA_METROPOLITANE_SET:
        st.info("ℹ️ Tipologia rilevata: **Città Metropolitana**")
    elif comune in CAPOLUOGHI_REGIONE_SET:
        st.info("ℹ️ Tipologia rilevata: **Capoluogo di Regione**")
    elif comune in CAPOLUOGHI_PROVINCIA_SET:
        st.info("ℹ️ Tipologia rilevata: **Capoluogo di Provincia**")
    else:
        st.info("ℹ️ Tipologia rilevata: **Comune Ordinario** (indennità valutata sul numero di abitanti)")
    
    # Generazione e render della tabella
    df_risultato = calcola_indennita_amministratori(popolazione, comune)
    
    st.dataframe(df_risultato, use_container_width=True, hide_index=True)
    st.info("ℹ️ L'indennità mensile di funzione lorda è parzialmente a carico della finanza statale e viene dimezzata del 50% in caso di Amministratori con contratto da dipendenti. L'indennità effettivamente erogata agli Amministratori è consultabile presso la sezione Amministrazione Trasparente del proprio comune.")

def pagina_popolazione_comuni():

    st.subheader("Comuni italiani per popolazione")

    # =====================================================
    # NORMALIZATION (UNICODE FIX DEFINITIVO)
    # =====================================================
    def normalize_comune(name):
        if pd.isna(name):
            return None
    
        name = str(name)
    
        # 1. decode safety (fix latin1 artifacts already read by pandas)
        name = name.encode("latin1", errors="ignore").decode("utf-8", errors="ignore")
    
        # 2. normalize unicode (fix accents)
        name = unicodedata.normalize("NFKC", name)
    
        # 3. remove combining marks (CRITICAL for ISTAT datasets)
        name = "".join(c for c in name if not unicodedata.combining(c))
    
        # 4. cleanup spacing
        name = " ".join(name.split())
    
        # 5. casefold for matching
        return name.casefold()

    def key_comune(name):
        if pd.isna(name):
            return None
    
        name = str(name)
        name = unicodedata.normalize("NFKC", name)
        name = "".join(c for c in name if not unicodedata.combining(c))
        return name.casefold().strip()

    # =====================================================
    # DOWNLOAD ISTAT
    # =====================================================
    def load_istat_population(year):

        zip_url = f"https://demo.istat.it/data/p2/P2_{year}_it_Comuni.zip"

        r = requests.get(zip_url, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()

        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            csv_file = next(name for name in z.namelist() if name.endswith(".csv"))
            with z.open(csv_file) as f:
                df = pd.read_csv(f, sep=";", skiprows=1, encoding="latin1")

        return df

    # =====================================================
    # COLONNE
    # =====================================================
    def find_population_column(df):
        for c in df.columns:
            if "Popolazione" in c and "31 dicembre - Totale" in c:
                return c
        raise KeyError("Colonna popolazione non trovata")

    def find_municipality_column(df):
        for c in df.columns:
            if c.lower() in ["comune", "territorio", "denominazione"]:
                return c
        for c in df.columns:
            if "comun" in c.lower():
                return c
        raise KeyError("Colonna comune non trovata")

    def find_flow_column(df, keywords):
        for c in df.columns:
            cl = c.lower()
            if all(k in cl for k in keywords):
                return c
        return None

    # =====================================================
    # DATASET
    # =====================================================
    @st.cache_data
    def build_dataset(years):

        all_data = []

        for y in years:
            df = load_istat_population(y)
            df.columns = df.columns.str.strip()

            pop_col = find_population_column(df)
            muni_col = find_municipality_column(df)

            male_births_col = find_flow_column(df, ["nati", "vivi", "maschi"])
            female_births_col = find_flow_column(df, ["nati", "vivi", "femmine"])
            emigrated_col = find_flow_column(df, ["emigrati", "estero"])

            tmp = pd.DataFrame()

            # ORIGINAL NAME (UI)
            tmp["Comune"] = df[muni_col].astype(str).str.strip()
            tmp["Comune_key"] = tmp["Comune"].apply(key_comune)

            tmp["Popolazione"] = pd.to_numeric(df[pop_col], errors="coerce")
            tmp["Nati maschi"] = pd.to_numeric(df[male_births_col], errors="coerce") if male_births_col else np.nan
            tmp["Nati femmine"] = pd.to_numeric(df[female_births_col], errors="coerce") if female_births_col else np.nan
            tmp["Emigrati estero"] = pd.to_numeric(df[emigrated_col], errors="coerce") if emigrated_col else np.nan

            tmp["Anno"] = y

            all_data.append(tmp)

        return pd.concat(all_data, ignore_index=True)

    # =====================================================
    # ANALISI COMUNE
    # =====================================================
    def analyze_municipality(df, comune):

        # FIX: always match on key_comune
        d = df[df["Comune_key"] == key_comune(comune)].sort_values("Anno")

        if d.empty:
            st.error("Comune non trovato")
            return

        d = d.copy()

        # =====================================================
        # FEATURES
        # =====================================================
        d["variazione (%)"] = d["Popolazione"].pct_change() * 100
        d["nuovi nati"] = d["Nati maschi"] + d["Nati femmine"]
        d["emigrati all'estero"] = d["Emigrati estero"]

        # =====================================================
        # PERCENTILE
        # =====================================================
        def compute_percentile(row):
            year_df = df[df["Anno"] == row["Anno"]]
            return (year_df["Popolazione"] < row["Popolazione"]).mean() * 100

        d["percentile"] = d.apply(compute_percentile, axis=1)

        # =====================================================
        # LATEST YEAR
        # =====================================================
        latest_year = d["Anno"].max()
        last_row = d[d["Anno"] == latest_year].iloc[0]

        # =====================================================
        # METRICS
        # =====================================================
        if len(d) > 1:
            prev_row = d.iloc[-2]

            pop_delta = int(last_row["Popolazione"] - prev_row["Popolazione"])
            pop_delta_pct = (
                (last_row["Popolazione"] - prev_row["Popolazione"])
                / prev_row["Popolazione"]
            ) * 100

            delta_label = f"{pop_delta:+,} ({pop_delta_pct:.2f}%)"
        else:
            delta_label = None

        col1, col2 = st.columns(2)

        with col1:
            st.metric(
                label="Percentile popolazione",
                value=f"{last_row['percentile']:.1f}%",
                help=(
                    f"Questo comune è più popolato del {last_row['percentile']:.1f}% dei comuni italiani. "
                    f"Solo il {100 - last_row['percentile']:.1f}% ha più abitanti."
                )
            )

        with col2:
            st.metric(
                label=f"Popolazione totale {latest_year}",
                value=int(last_row["Popolazione"]),
                delta=delta_label
            )

        # =====================================================
        # CHARTS
        # =====================================================

        d["Anno"] = d["Anno"].astype(str)
        
        st.subheader("📊 Andamento della popolazione")
        st.bar_chart(d.set_index("Anno")[["Popolazione"]])

        st.subheader("📉 Variazione percentuale annuale")
        st.line_chart(d.set_index("Anno")[["variazione (%)"]])

        st.subheader("👶 Flussi demografici")
        st.line_chart(
            d.set_index("Anno")[["nuovi nati", "emigrati all'estero"]]
        )

    # =====================================================
    # STREAMLIT APP
    # =====================================================

    years = range(2019, 2026)
    df_all = build_dataset(years)

    comuni = (
    df_all["Comune"]
    .dropna()
    .astype(str)
    .str.strip()
    .unique()
    )
    
    comuni_map = {c.upper(): c for c in comuni}
    
    comune_sel_ui = st.selectbox(
        "Seleziona un comune",
        list(comuni_map.keys())
    )
    
    comune_sel = comuni_map[comune_sel_ui]
    
    analyze_municipality(df_all, comune_sel)

if pagina == "Home":
    pagina_home()

elif pagina == "Analizzatore Cedolini":
    pagina_cedolini_regione()

elif pagina == "Anagrafica Consiglieri Regionali":
    pagina_anagrafiche_regione()

elif pagina == "Anagrafica Organi Comunali":
    pagina_anagrafiche_comuni()

elif pagina == "Comuni italiani per popolazione":
    pagina_popolazione_comuni()
