# NYC Job Scout

Finds NYC job postings that match your resume and ranks them by an **overall score** that blends:

- **Job fit** (75% by default): skills you share with the posting, overall similarity to your resume,
  title, and seniority (it deducts points when a posting wants more years than you have).
- **Company outlook** (25% by default): 40% company health, 30% industry potential, 30% job security.

## Start it

Double-click `start.command` in Finder, or run:

```bash
cd ~/nyc-job-scout
python3 app.py
```

A page opens at http://127.0.0.1:8765 (only reachable from this computer). On it you can:

- **Edit your resume** in the text box and click *Save changes*. Each save keeps the previous version
  under *Earlier versions*.
- **Upload a new resume** (.pdf, .docx, .txt…). The text is extracted so you can check and edit it.
- Choose categories (Finance & FP&A, Sales, Insurance, Construction & Real Estate, Startups,
  Risk & Compliance, Consulting), add specific titles, exclude titles, and set how much company outlook counts.
- Filter and sort results, open each company's details (Sayari record, news sources), and download a CSV.

A search takes about 15 seconds. Re-scanning all employer job boards adds about a minute (results are reused for 6 hours).

## Where jobs come from

| Source | Setup | Coverage |
|---|---|---|
| Employer job boards (Greenhouse, Lever, Ashby) | none | ~93 employers in `companies.json` (banks, trading firms, fintech, insurers, startups); ~3,000 NYC postings |
| Saved research in `extra_jobs/` | none | Postings found through Tavily research on LinkedIn, Indeed, Built In NYC (Nov 20, 2026). Files older than 45 days are skipped |
| Live Tavily search (LinkedIn, Indeed, Built In NYC, Glassdoor, Workday, NYC & federal jobs) | `export TAVILY_API_KEY=tvly-...` before starting (free tier at tavily.com); runs keyless with a small daily cap otherwise | any employer, two searches per selected category |
| Adzuna (optional) | `export ADZUNA_APP_ID=... ADZUNA_APP_KEY=...` | broad aggregator |

Add employers to `companies.json` using the slug from their job-board URL
(`boards.greenhouse.io/<slug>`, `jobs.lever.co/<slug>`, `jobs.ashbyhq.com/<slug>`).

## Company outlook data

`company_profiles.json` holds ratings for about 90 employers and an outlook for each industry. Each company is marked:

- **research**: backed by Sayari corporate records (active status, registration, sanctions/regulatory/law-enforcement
  flags, footprint) and Tavily news (funding, earnings, acquisitions, layoffs), with source links.
- **research+estimate**: has a Sayari record, but the health and security ratings are judgment calls.
- **estimate**: general knowledge only. Worth checking before you rely on it.

Unlisted companies get a neutral 60. Contract and temporary roles lose 20 points of job security.
Sayari and the Claude-side Tavily connector only work inside a Claude session, so to refresh or extend this file,
ask Claude to "update company_profiles.json for the companies in my latest results".

## Claude scoring (optional)

Tick *Have Claude re-score the top 30* to get a recruiter-style verdict, strengths and gaps for each top job.
It needs Python 3.10+ (`brew install python`), `pip3 install anthropic`, and `ANTHROPIC_API_KEY`.
It uses Claude Opus 5.5 and costs a few cents per job. `start.command` picks the newest Python it can find.

## Command line

```bash
python3 job_scout.py resume.txt --categories finance,insurance,risk --exclude "intern,director" --company-weight 0.3
python3 job_scout.py --help
```

Your resume, settings, uploads and results stay in this folder and are listed in `.gitignore`.
