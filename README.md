# Piemonte Regional Council Salary Scraper

## Installation

```bash
pip install -r requirements.txt
```

## Run

```bash
streamlit run app.py
```

## Input CSV

The input CSV must contain at least:

| Column |
|--------|
| Nominativo |
| Gruppo consiliare |

## Output

The application extracts:

- Politician
- Group
- Photo URL
- Legislature
- Date
- Gross salary
- Tax deduction
- Net salary
- PDF URL

Results can be downloaded as CSV.
