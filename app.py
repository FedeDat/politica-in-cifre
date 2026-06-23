import io
import re
from datetime import datetime, date
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

# =========================
# STREAMLIT UI
# =========================

st.set_page_config(page_title="Politica in cifre", layout="wide")

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
        "Analizzatore Cedolini",
        "Anagrafica Consiglieri Regionali",
        "Trattamento economico",
        "Anagrafica Comuni"
    ]
)

def pagina_cedolini():
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
            "https://www.cr.piemonte.it/cms/amministrazione-trasparente/organizzazione/titolari-di-incarichi-politici-di-amministrazione-di."
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

def pagina_anagrafiche_comune():
    st.subheader("📊 Analizzatore Anagrafiche Organi Comunali (aggiornate al 6 giugno 2026)", divider=True)

    st.write("Il codice analizza i dati del Dipartimento per gli Affari Interni e Territoriale e restituisce anali della composizione delle Giunte e dei Consigli Comunali.")
    st.write("Dati estratti da https://dait.interno.gov.it/elezioni/open-data/amministratori-locali-e-regionali-in-carica.")
    
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
    sindaco = df_comune[df_comune["ruolo"] == "Sindaco"]
    giunta = df_comune[df_comune["ruolo"].isin(["Sindaco", "Vicesindaco", "Assessore"])]
    consiglio = df_comune[df_comune["ruolo"].str.contains("Consigliere", na=False)]
    
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
                "nome",
                "cognome",
                "ruolo",
                "sesso",
                "eta",
                "lista_appartenenza/collegamento",
                "ruolo_ordine",
            ]
        ]
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
        .drop(columns=["ruolo_ordine"])
        .assign(**{"Gruppo consiliare": lambda x: x["Gruppo consiliare"].fillna("Non indicato")})
    )
    
    # =========================
    # STREAMLIT OUTPUT
    # =========================
    st.subheader(f"Comune selezionato: {comune}")
    
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

if pagina == "Analizzatore Cedolini":
    pagina_cedolini()

elif pagina == "Anagrafica Consiglieri Regionali":
    pagina_anagrafiche_regione()

elif pagina == "Anagrafica Comuni":
    pagina_anagrafiche_comune()
