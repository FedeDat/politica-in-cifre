import streamlit as st
import pandas as pd
from scraper import scrape_all

st.set_page_config(
    page_title="Piemonte Council Salary Scraper",
    page_icon="🏛️",
    layout="wide",
)

st.title("🏛️ Piemonte Regional Council Salary Scraper")

st.markdown(
    """
This application downloads the list of members of the Piemonte Regional Council,
finds their salary slips (cedolini), extracts the salary information from each PDF,
and allows you to download the results as a CSV file.
"""
)

if st.button("Start scraping", type="primary"):

    progress = st.progress(0)
    status = st.empty()

    with st.spinner("Downloading council members..."):

        try:
            salary_df = scrape_all(
                progress_bar=progress,
                status=status,
            )

        except Exception as e:
            st.error(f"Unexpected error: {e}")
            st.stop()

    progress.empty()
    status.empty()

    st.success(
        f"Completed! Extracted {len(salary_df)} salary records."
    )

    if len(salary_df):

        st.metric(
            "Salary records",
            len(salary_df)
        )

        st.metric(
            "Politicians",
            salary_df["Nominativo"].nunique()
        )

        gross = salary_df["Lordo (€)"].mean()

        net = salary_df["Netto (€)"].mean()

        c1, c2 = st.columns(2)

        c1.metric(
            "Average Gross Salary",
            f"€ {gross:,.2f}"
        )

        c2.metric(
            "Average Net Salary",
            f"€ {net:,.2f}"
        )

        st.dataframe(
            salary_df,
            use_container_width=True,
            hide_index=True,
        )

        csv = salary_df.to_csv(
            index=False
        ).encode("utf-8")

        st.download_button(
            label="📥 Download CSV",
            data=csv,
            file_name="piemonte_salary_data.csv",
            mime="text/csv",
        )

    else:

        st.warning("No salary information was extracted.")

st.divider()

with st.expander("About"):

    st.markdown(
        """
### Data source

The application downloads public information from the Piemonte Regional Council website.

Workflow:

1. Download the official councillor list
2. Locate each personal profile
3. Download all available salary PDFs
4. Extract:
   - Gross salary
   - Tax deductions
   - Net salary
5. Export everything as CSV

The application caches downloaded pages and PDFs to improve performance.
"""
    )
