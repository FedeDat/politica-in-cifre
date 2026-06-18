# =========================
# STREAMLIT UI
# =========================

st.set_page_config(page_title="Piemonte Politics Costs", layout="wide")

st.title("🏛️ Piemonte Politics Cost Tracker")

st.write("Scraping pubblici compensi consiglieri regionali")

# STEP 1: load list of councillors
@st.cache_data(show_spinner=False)
def get_councillors():
    url = "https://www.cr.piemonte.it/cms/consiglieri"
    df = pd.read_html(url)[0]
    return df


df_list = get_councillors()

names = df_list["Nominativo"].tolist()

selected = st.selectbox("Seleziona consigliere", names)

run = st.button("Analizza consigliere", type="primary")

if run:

    progress = st.progress(0)
    status = st.empty()

    with st.spinner("Scraping in corso..."):

        df = scrape_all_single(selected, progress, status)

    progress.empty()
    status.empty()

    if df.empty:
        st.warning("Nessun dato trovato per questo consigliere.")
        st.stop()

    st.success(f"Done! {len(df)} records extracted.")

    st.dataframe(df, use_container_width=True)

def scrape_all_single(name, progress_bar=None, status=None):

    rows = []

    if status:
        status.write(f"Scraping **{name}**")

    profile = find_profile(name)
    if not profile:
        return pd.DataFrame()

    photo = get_photo(profile)
    pdfs = get_pdf_links(profile)

    total = len(pdfs)

    with ThreadPoolExecutor(max_workers=8) as ex:

        futures = {ex.submit(extract_numbers, p): p for p in pdfs}

        for i, f in enumerate(as_completed(futures), start=1):

            pdf = futures[f]

            try:
                values = f.result()
                if not values:
                    continue

                date = datetime.strptime(pdf[-29:-21], "%Y%m%d").date()

                rows.append({
                    "Nominativo": name,
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
