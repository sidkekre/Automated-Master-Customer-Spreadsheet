# Automated Master Customer Spreadsheet

Automates extraction of contract data from DocuSign-signed agreements into a Google Spreadsheet. When a contract is signed via DocuSign, the system receives a webhook, downloads the PDF, uploads it to Google Drive, extracts 32 structured fields using an LLM, and appends the data as a row in the master spreadsheet.

## Pipeline

```
DocuSign envelope-completed webhook
  -> Deduplication check (SQLite with TTL)
  -> Download signed PDF from DocuSign
  -> Upload PDF to Google Drive (public view URL)
  -> LLM extracts 32 fields from PDF -> JSON
  -> Append row to Google Spreadsheet
```

## Project Structure

```
src/
  main.py                       # Flask app, webhook endpoint, pipeline orchestration
  constants.py                  # Extraction columns, defaults, config constants
  logger.py                     # Wrapper methods for log generation

  data_sources/
    docusign.py                 # JWT auth, webhook handling, PDF download
    google.py                   # Sheets API (read/write), Drive API (upload/download)

  db/
    db.py                       # SQLite wrapper with TTL for envelope deduplication

  llm/
    llm_interface.py                 # OpenAI Responses API: prompt generation + contract content extraction
    prompt_template_analysis.md      # Meta-prompt: analyzes blank templates -> generates extraction prompt
    prompt_contract_info_extract.md  # Generated extraction prompt (used at runtime)

tests/
  test.py                       # Unit tests

DOCS/
  contract_templates/           # Blank contract template PDFs (downloaded from Google Drive)
```

## How It Works

### Prompt Generation (one-time setup)

1. `download_templates()` fetches blank contract templates from a Google Drive folder (supports PDFs, Google Docs, Word files)
2. `generate_extraction_prompt()` uploads all templates with `prompt_template_analysis.md` to the LLM
3. The LLM analyzes template structures and produces `prompt_contract_info_extract.md` -- a self-contained extraction prompt with per-contract-type rules and few-shot examples

### Contract Extraction (per webhook)

1. `extract_contract_info()` uploads the signed PDF with the generated extraction prompt
2. The LLM identifies the contract type (MSA, NDA, BAA, DPA, Pilot, Partner, Contractor, etc.) and extracts all 32 fields
3. Response is validated (all keys present, JSON format) with configurable defaults for optional fields
4. Row is appended to the spreadsheet aligned to the header

### Extracted Fields

Company Name, DBA, Entity Type, Country, Price Increase terms, Term dates, Auto-Renewal, Renewal Terms, Termination Notice, Pricing Model, Payment terms, Late Payment Fees, Liability Cap, Indemnification Scope, Governing Law, Arbitration/Litigation venue, DPA/HIPAA applicability, Assignment Restrictions, Non-Solicitation, Agreement Status, Data Training rights, Internal Owner, Renewal Action Required, Notes, and Document Link.

## Setup

### Requirements

- Python 3.12+
- Dependencies: `pip install -r requirements.txt`

### Environment Variables

Copy `.env.example` to `.env` and fill in:

| Variable                        | Description                                     |
|---------------------------------|-------------------------------------------------|
| `FLASK_PORT`                    | Server port                                     |
| `DOCUSIGN_INTEGRATION_KEY`      | DocuSign app integration key                    |
| `DOCUSIGN_USER_ID`              | DocuSign impersonated user ID                   |
| `DOCUSIGN_PRIVATE_KEY`          | RSA private key for JWT auth                    |
| `DOCUSIGN_AUTH_URL`             | DocuSign auth server URL                        |
| `DOCUSIGN_CONSENT_REDIRECT_URI` | OAuth consent redirect                          |
| `DOCUSIGN_HMAC_SECRET`          | Webhook HMAC verification secret                |
| `GOOGLE_CLIENT_ID`              | Google OAuth client ID                          |
| `GOOGLE_CLIENT_SECRET`          | Google OAuth client secret                      |
| `GOOGLE_REFRESH_TOKEN`          | Google OAuth refresh token                      |
| `GOOGLE_SPREADSHEET_ID`         | Target spreadsheet ID                           |
| `GOOGLE_SPREADSHEET_TAB_NAME`   | Target tab name                                 |
| `TEMPLATES_GOOGLE_DRIVE_URL`    | Google Drive folder URL with contract templates |
| `OPENAI_API_KEY`                | OpenAI API key                                  |
| `OPENAI_MODEL`                  | Model name (e.g. `gpt-4o`)                      |

### Run

```bash
python -m src.main
```

### Tests

```bash
python -m unittest tests.test -v
```

### Generate Extraction Prompt

Uncomment `parse_contract_templates()` in `main.py` or call it directly to re-generate the extraction prompt from current templates.
