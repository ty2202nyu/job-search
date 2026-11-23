#!/usr/bin/env python3
"""NYC Job Scout: scan NYC job postings and score each one against your resume.

Easiest way to use it:   python3 app.py      (opens a local web page)

Command line:
    python3 job_scout.py path/to/resume.pdf
    python3 job_scout.py resume.pdf --categories finance,sales,insurance --years 1
    python3 job_scout.py resume.pdf --ai          # add Claude scoring for the top matches

Run with --help for every option.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import csv
import datetime as dt
import html
import json
import math
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
CACHE_DIR = HERE / ".cache"
OUT_DIR = HERE / "results"
EXTRA_DIR = HERE / "extra_jobs"
CACHE_TTL_HOURS = 6
EXTRA_MAX_AGE_DAYS = 45
UA = {"User-Agent": "nyc-job-scout/1.0"}

NYC_RE = re.compile(
    r"\b(new york|nyc|ny,? ny|brooklyn|manhattan|queens|bronx|staten island|"
    r"jersey city|hoboken)\b",
    re.I,
)
US_REMOTE_RE = re.compile(r"remote.{0,20}(us|u\.s\.|united states|usa|north america)|"
                          r"(us|united states|usa)\W{0,3}remote", re.I)

# --------------------------------------------------------------------------
# Job categories. Each has a label, a title regex used to tag postings, and the
# searches sent to job sites (via Tavily) when that category is selected.
# --------------------------------------------------------------------------
CATEGORIES = {
    "finance": ("Finance & FP&A",
                r"financ|fp&a|fp & a|accounting|accountant|treasury|controller|revenue|pricing|investment|"
                r"banking|banker|capital|credit|equity|valuation|portfolio|wealth|asset management|\bm&a\b|"
                r"corporate development|audit|\btax\b|billing|reconciliation|fund|trading|markets",
                ["financial analyst", "FP&A analyst", "investment banking analyst", "private equity analyst",
                 "credit analyst", "corporate development analyst"]),
    "sales": ("Sales & Business Development",
              r"sales|account executive|business development|\bsdr\b|\bbdr\b|account manager|partnerships|"
              r"revenue operations|\bgtm\b|go-to-market|customer success|relationship manager",
              ["sales development representative", "business development representative", "financial sales associate"]),
    "insurance": ("Insurance",
                  r"insurance|underwrit|actuar|claims|reinsurance|broker|surety|\bp&c\b|casualty",
                  ["insurance underwriting analyst", "underwriting assistant insurance", "reinsurance analyst"]),
    "construction": ("Construction & Real Estate",
                     r"construction|real estate|project finance|infrastructure|estimat|capital projects|"
                     r"facilities|property|development analyst|acquisitions",
                     ["construction finance analyst", "project finance analyst", "real estate analyst"]),
    "startup": ("Startups: Strategy & Operations",
                r"strategy|operations|chief of staff|bizops|business operations|founding|generalist|"
                r"special projects|\bops\b",
                ["startup strategy and operations associate", "business operations analyst", "chief of staff associate"]),
    "risk": ("Risk, Compliance & Security",
             r"compliance|sanction|\baml\b|\bkyc\b|financial crime|fraud|risk|regulatory|policy|security|"
             r"investigat|trust & safety|intelligence|geopolit",
             ["sanctions compliance analyst", "financial crimes analyst", "risk analyst", "geopolitical risk analyst"]),
    "consulting": ("Consulting & Research",
                   r"consult|advisory|research|economic|analyst program|business analyst",
                   ["business analyst consulting", "economic research analyst"]),
}
CATEGORY_RES = {k: re.compile(v[1], re.I) for k, v in CATEGORIES.items()}

# --------------------------------------------------------------------------
# Skills lexicon: phrases matched (case-insensitive, whole-word) in both the
# resume and each job description. Add to it freely.
# --------------------------------------------------------------------------
SKILLS = """
python|r|sql|java|javascript|typescript|go|golang|rust|c\\+\\+|c#|scala|kotlin|swift|ruby|php|matlab|sas|stata|spss|vba|bash
react|angular|vue|node.js|django|flask|fastapi|spring|rails|graphql|rest api|html|css
aws|gcp|google cloud|azure|docker|kubernetes|terraform|ci/cd|linux|git|airflow|dbt|spark|hadoop|kafka|snowflake|bigquery|redshift|databricks
postgresql|mysql|mongodb|redis|elasticsearch|nosql
machine learning|deep learning|nlp|natural language processing|computer vision|llm|large language models|pytorch|tensorflow|scikit-learn|pandas|numpy|statistics|regression|a/b testing|experimentation|causal inference|forecasting|time series|econometrics|optimization|simulation|ai
tableau|power bi|looker|excel|google sheets|powerpoint|microsoft office|pivot tables|data visualization|dashboards|data analysis|data analytics|data modeling|etl|data pipelines|data engineering|data science|business intelligence
product management|roadmap|user research|ux|ui|figma|sketch|adobe creative suite|photoshop|illustrator|indesign|prototyping|wireframing|design systems
project management|program management|agile|scrum|jira|stakeholder management|cross-functional|okrs|change management|process improvement|lean|six sigma
supply chain|logistics|procurement|sourcing|inventory management|demand planning|operations|vendor management|sustainability|esg|compliance|risk management|due diligence|trade compliance|traceability|lifecycle assessment
financial modeling|valuation|accounting|gaap|fp&a|budgeting|financial analysis|financial statement analysis|financial statements|corporate finance|investment banking|private equity|venture capital|equity research|portfolio management|bloomberg terminal|underwriting|credit analysis|audit|tax
lbo|m&a|mergers and acquisitions|dcf|discounted cash flow|revenue forecasting|pricing|reconciliation|invoicing|accounts payable|variance analysis|treasury|cfa|capital markets|fixed income|equity securities|lending|wealth management|asset management|real estate|project finance|arr|unit economics|kpis|board materials|cost reduction|shared services|headcount planning|g&a
sanctions|aml|anti-money laundering|kyc|ofac|financial crimes|fraud|regulatory|geopolitical|national security|cybersecurity|cybercrime|threat intelligence|investigations|stablecoins|cryptocurrency|blockchain|payments|fintech|central bank|cross-border payments|economic policy
insurance|actuarial|claims|reinsurance|brokerage|risk assessment|political risk|surety
construction|estimating|cost estimation|procore|project controls|capital projects|general contractor
prospecting|cold calling|outbound|pipeline generation|quota|lead generation|negotiation|saas|b2b
marketing|digital marketing|seo|sem|content strategy|social media|brand strategy|growth|crm|salesforce|hubspot|email marketing|copywriting|market research|competitive analysis|go-to-market|partnerships|business development|sales|account management|customer success
public policy|policy analysis|research|qualitative research|quantitative research|survey design|grant writing|writing|editing|journalism|communications|public relations|presentation|consulting|strategy|knowledge management|documentation
gis|arcgis|qgis|urban planning|mapping|geospatial
mandarin|spanish|french|portuguese|italian|german|japanese|korean|arabic|hindi
""".strip().splitlines()
SKILLS = sorted({s.strip() for line in SKILLS for s in line.split("|") if s.strip()}, key=len, reverse=True)
SKILL_RES = {s: re.compile(r"(?<![\w+#&])" + s.replace(".", r"\.").replace("/", r"\s*/\s*").replace(" ", r"[\s-]*")
                           + r"(?![\w+#&])", re.I)
             for s in SKILLS}
# Common words that only count in a job ad when the resume also has them.
AMBIGUOUS = {"r", "go", "c#", "swift", "spring", "growth", "research", "writing", "operations", "ai",
             "sales", "strategy", "presentation", "editing", "mapping", "audit", "tax", "lean", "claims",
             "pricing", "documentation", "insurance", "construction", "real estate", "payments", "lending",
             "regulatory", "compliance", "partnerships", "marketing", "consulting", "communications"}

STOP = set("""a an and are as at be been but by can for from has have i in is it its of on or our
that the their this to was we were will with you your they them who what when where which while
about above after all also any both each few how into more most no not only other out over own same
so some such than then there these those through too under until up very just per via within across
team teams work working role roles job company us new york nyc experience years year including
ability strong skills skill etc using use used help helping make making build building""".split())

SENIOR_RE = re.compile(r"\b(senior|sr\.?|staff|principal|lead|head of|director|vp|vice president|"
                       r"chief|manager|distinguished|partner|svp|evp)\b", re.I)
JUNIOR_RE = re.compile(r"\b(junior|jr\.?|entry[- ]level|new grad(?:uate)?|university grad(?:uate)?|recent grad(?:uate)?|"
                       r"early career|campus|class of 20\d\d|apprentice|analyst i\b|analyst 1\b|associate i\b|"
                       r"analyst program|rotational|graduate program|coordinator|assistant)\b", re.I)
# Phrases in a posting's text that mark it as open to people without full-time experience
ENTRY_TEXT_RE = re.compile(r"\b(entry[- ]level|new grad(?:uate)?s?|recent (?:college )?grad(?:uate)?s?|early[- ]career|"
                           r"class of 20(?:2[5-8])|graduating (?:in|by) 20\d\d|university grad(?:uate)?s?|"
                           r"0\s*(?:-|–|to)\s*[12]\s*years?|no (?:prior )?experience (?:is )?required|"
                           r"(?:up to|less than) (?:1|2|one|two) years?)\b", re.I)
# Roles that need a license or degree a resume either shows or doesn't
CREDENTIAL_ROLES = [
    (re.compile(r"\b(counsel|attorney|lawyer|associate general counsel)\b", re.I), re.compile(r"\b(j\.?d\.?|juris doctor|law school|bar admission)\b", re.I)),
    (re.compile(r"\b(dean|professor|lecturer|teacher|instructor)\b", re.I), re.compile(r"\b(ph\.?d|teaching|professor|teacher)\b", re.I)),
    (re.compile(r"\b(nurse|physician|pharmacist|clinician|therapist|dentist|md\b)", re.I), re.compile(r"\b(nursing|rn\b|m\.?d\.?|pharm|clinical)\b", re.I)),
    (re.compile(r"\b(actuary|actuarial)\b", re.I), re.compile(r"\b(actuar|soa exam|fsa|asa)\b", re.I)),
]
INTERN_ROLE_RE = re.compile(r"\b(intern|internship|co-?op|practicum|student|apprentice|summer (?:analyst|associate)|"
                            r"trainee|research assistant|teaching assistant|graduate assistant)\b", re.I)
NUM_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
             "ten": 10, "twelve": 12, "fifteen": 15}
YEARS_REQ_RE = re.compile(r"(\d{1,2})\s*\+?\s*(?:-|–|to)?\s*(?:\d{1,2})?\s*\+?\s*(?:years?|yrs?)['’]?(?:\s+of)?"
                          r"(?:\s+[\w/&,-]+){0,5}\s+experience", re.I)


# --------------------------------------------------------------------------
# Resume loading
# --------------------------------------------------------------------------
def read_resume(path: Path) -> str:
    """Extract text from a resume file. Raises ValueError if it can't."""
    ext = path.suffix.lower()
    if ext in (".txt", ".md", ".text"):
        return path.read_text(errors="ignore")
    if ext == ".pdf":
        try:
            from pypdf import PdfReader  # optional dependency
            return "\n".join(p.extract_text() or "" for p in PdfReader(str(path)).pages)
        except ImportError:
            pass
        if sys.platform == "darwin":  # macOS PDFKit via JavaScript for Automation, no installs needed
            js = ("ObjC.import('PDFKit');"
                  "var d=$.PDFDocument.alloc.initWithURL($.NSURL.fileURLWithPath(%s));"
                  "d.string.js" % json.dumps(str(path.resolve())))
            r = subprocess.run(["osascript", "-l", "JavaScript", "-e", js], capture_output=True, text=True)
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout
        raise ValueError("Could not read the PDF. Run `pip3 install pypdf`, or paste your resume text instead.")
    if ext in (".docx", ".doc", ".rtf", ".odt", ".pages", ".html", ".htm"):
        if sys.platform == "darwin":
            r = subprocess.run(["textutil", "-convert", "txt", "-stdout", str(path)], capture_output=True, text=True)
            if r.returncode == 0:
                return r.stdout
        raise ValueError(f"Could not read {ext} files on this system. Save the resume as .pdf or .txt.")
    return path.read_text(errors="ignore")


MONTHS = {m: i for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)}
_MON = r"(?:(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s*)?"
DATE_RANGE_RE = re.compile(_MON + r"((?:19|20)\d{2})\s*(?:-|–|—|to)+\s*" + _MON + r"((?:19|20)\d{2}|present|current|now)",
                           re.I)
WORK_HEAD_RE = re.compile(r"experience|employment|work history|career", re.I)
OTHER_HEAD_RE = re.compile(r"education|extracurricular|activities|athletics|leadership|skills|projects|honors|awards|"
                           r"certifications|volunteer|interests|publications|coursework|languages", re.I)


def estimate_years(text: str) -> float:
    """Full-time-equivalent years of work experience (internships count a quarter)."""
    return experience_profile(text)["years"]


def career_level(years: float) -> str:
    return "entry" if years < 1.5 else "early" if years < 3 else "mid" if years < 6 else "senior"


def experience_profile(text: str) -> dict:
    """Months of full-time and internship work from date ranges in the Experience section(s)."""
    lines = text.splitlines()
    work, section, saw_heading = [], None, False
    for ln in lines:
        s = ln.strip()
        if 0 < len(s) <= 40 and len(s.split()) <= 4 and not DATE_RANGE_RE.search(s):
            if WORK_HEAD_RE.search(s) and not re.search(r"volunteer", s, re.I):
                section, saw_heading = "work", True
                continue
            if OTHER_HEAD_RE.search(s):
                section, saw_heading = "other", True
                continue
        if section == "work" or not saw_heading:
            work.append(ln)
    today = dt.date.today()
    body = "\n".join(work)
    matches = list(DATE_RANGE_RE.finditer(body))
    full, intern = set(), set()
    for i, m in enumerate(matches):
        sm, sy, em, ey = m.groups()
        start = int(sy) * 12 + (MONTHS[sm[:3].lower()] if sm else 1)
        if ey[0].isdigit():
            end = int(ey) * 12 + (MONTHS[em[:3].lower()] if em else 12)
        else:
            end = today.year * 12 + today.month
        if not 0 <= end - start <= 480:
            continue
        # The role's block: from the line before this date to the next date range.
        line_start = body.rfind("\n", 0, body.rfind("\n", 0, m.start()) if m.start() else 0) + 1
        block = body[line_start:matches[i + 1].start() if i + 1 < len(matches) else len(body)]
        head = "\n".join(block.splitlines()[:4])
        is_intern = INTERN_ROLE_RE.search(head) or re.search(r"\bpracticum\b", block, re.I)
        (intern if is_intern else full).update(range(start, end + 1))
    intern -= full
    years = round((len(full) + 0.25 * len(intern)) / 12, 1)
    return {"years": years, "full_time_months": len(full), "internship_months": len(intern),
            "level": career_level(years)}


# --------------------------------------------------------------------------
# Job fetching
# --------------------------------------------------------------------------
def http_json(url: str, timeout: int = 30):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def strip_html(s: str) -> str:
    s = html.unescape(s or "")
    s = re.sub(r"<(br|/p|/li|/h\d|/div)[^>]*>", "\n", s, flags=re.I)
    s = re.sub(r"<li[^>]*>", "\n• ", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t\xa0]+", " ", s)
    return re.sub(r"\n\s*\n+", "\n", s).strip()


def location_ok(loc: str, include_remote: bool) -> bool:
    if NYC_RE.search(loc):
        return True
    return include_remote and bool(US_REMOTE_RE.search(loc) or loc.strip().lower() == "remote")


def fetch_greenhouse(slug):
    d = http_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true")
    for j in d.get("jobs", []):
        yield dict(source="Greenhouse", company=j.get("company_name") or slug, title=j["title"],
                   location=(j.get("location") or {}).get("name", ""), url=j["absolute_url"],
                   posted=(j.get("first_published") or j.get("updated_at") or "")[:10],
                   salary="", description=strip_html(j.get("content", "")))


def fetch_lever(slug):
    d = http_json(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    for j in d:
        c = j.get("categories") or {}
        locs = c.get("allLocations") or [c.get("location", "")]
        lists = "\n".join(f"{x.get('text','')}\n{strip_html(x.get('content',''))}" for x in j.get("lists") or [])
        sal = j.get("salaryRange") or {}
        salary = f"${sal['min']:,}–${sal['max']:,}" if sal.get("min") and sal.get("max") else ""
        posted = dt.datetime.fromtimestamp(j["createdAt"] / 1000).date().isoformat() if j.get("createdAt") else ""
        yield dict(source="Lever", company=slug.title(), title=j["text"], location=" / ".join(filter(None, locs)),
                   url=j["hostedUrl"], posted=posted, salary=salary,
                   description="\n".join([j.get("descriptionPlain", ""), lists, j.get("additionalPlain", "")]))


def fetch_ashby(slug):
    d = http_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true")
    for j in d.get("jobs", []):
        if j.get("isListed") is False:
            continue
        locs = [j.get("location", "")] + [x.get("location", "") for x in j.get("secondaryLocations") or []]
        if j.get("isRemote"):
            locs.append("Remote")
        comp = (j.get("compensation") or {}).get("compensationTierSummary") or ""
        yield dict(source="Ashby", company=slug.title(), title=j["title"].strip(), location=" / ".join(filter(None, locs)),
                   url=j["jobUrl"], posted=(j.get("publishedAt") or "")[:10], salary=comp,
                   description=j.get("descriptionPlain") or strip_html(j.get("descriptionHtml", "")))


def fetch_adzuna(queries, app_id, app_key, pages=2):
    for q in queries:
        for page in range(1, pages + 1):
            params = urllib.parse.urlencode({
                "app_id": app_id, "app_key": app_key, "results_per_page": 50,
                "what": q, "where": "New York, NY", "distance": 15, "max_days_old": 30,
                "content-type": "application/json"})
            d = http_json(f"https://api.adzuna.com/v1/api/jobs/us/search/{page}?{params}")
            for j in d.get("results", []):
                lo, hi = j.get("salary_min"), j.get("salary_max")
                salary = f"${lo:,.0f}–${hi:,.0f}" if lo and hi and lo != hi else (f"${lo:,.0f}" if lo else "")
                yield dict(source="Adzuna", company=(j.get("company") or {}).get("display_name", ""),
                           title=strip_html(j.get("title", "")),
                           location=(j.get("location") or {}).get("display_name", "") + " (New York area)",
                           url=j["redirect_url"], posted=(j.get("created") or "")[:10], salary=salary,
                           description=strip_html(j.get("description", "")), snippet_only=True)
            if len(d.get("results", [])) < 50:
                break


TAVILY_DOMAINS = ["linkedin.com", "indeed.com", "builtinnyc.com", "wellfound.com", "myworkdayjobs.com",
                  "glassdoor.com", "idealist.org", "nyc.gov", "usajobs.gov"]
POSTING_URL_RE = re.compile(r"linkedin\.com/jobs/view/|indeed\.com/viewjob|builtinnyc\.com/job/|"
                            r"wellfound\.com/jobs/|myworkdayjobs\.com/.+/job/|glassdoor\.com/job-listing/|"
                            r"idealist\.org/.+/(job|internship)/|cityjobs\.nyc\.gov|usajobs\.gov/job/", re.I)
GONE_RE = re.compile(r"job (?:was|has been) removed|no longer accepting applications|this job has expired", re.I)


def parse_web_title(title: str, url: str):
    """Split a job-site page title into (job title, company)."""
    t = re.sub(r"\s*[-|–—]\s*(Indeed\.com|LinkedIn( Jobs)?|Built In NYC|Glassdoor|Wellfound|Jobs)\s*$", "", title, flags=re.I)
    t = re.sub(r"\s*\|\s*LinkedIn Jobs\s*$", "", t, flags=re.I)
    m = re.match(r"(.+?)\s+at\s+(.+?)(?:\s+[—-]\s+.*)?$", t)  # LinkedIn: "Title at Company — Location"
    if m and "linkedin" in url:
        return m.group(1).strip(), m.group(2).strip()
    parts = [p.strip() for p in re.split(r"\s+[-|–—]\s+", t) if p.strip()]
    if "builtinnyc" in url and len(parts) >= 2:  # "Title - Company | Built In NYC"
        return " - ".join(parts[:-1]), parts[-1]
    return parts[0] if parts else t, ""


def fetch_tavily(queries, api_key=None, per_query=20):
    """Search job sites (LinkedIn, Indeed, Built In NYC, ...) through the Tavily search API."""
    headers = {"Content-Type": "application/json", **UA}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    else:
        headers["X-Tavily-Access-Mode"] = "keyless"  # free, but rate-limited per day
    for q in queries:
        body = json.dumps({"query": f"{q} job New York, NY", "include_domains": TAVILY_DOMAINS,
                           "time_range": "month", "max_results": per_query, "search_depth": "advanced",
                           "include_raw_content": True, "country": "United States"}).encode()
        req = urllib.request.Request("https://api.tavily.com/search", data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                d = json.load(r)
        except urllib.error.HTTPError as e:
            msg = e.read().decode(errors="ignore")[:200]
            raise RuntimeError(f"HTTP {e.code}: {msg}") from None
        for r in d.get("results", []):
            url, page_title = r.get("url", ""), r.get("title", "")
            text = r.get("raw_content") or r.get("content") or ""
            if not POSTING_URL_RE.search(url) or GONE_RE.search(text):
                continue  # skip search-result listing pages and expired postings
            if not NYC_RE.search(page_title + " " + text[:3000]):
                continue
            title, company = parse_web_title(page_title, url)
            sal = re.search(r"\$\d[\d,]*(?:\.\d+)?[kK]?\s*(?:-|–|to)\s*\$\d[\d,]*(?:\.\d+)?[kK]?", text)
            yield dict(source="Web (Tavily)", company=company or urllib.parse.urlparse(url).netloc,
                       title=title, location="New York area", url=url, posted="",
                       salary=sal.group(0) if sal else "", description=text[:20000],
                       snippet_only=len(text) < 1200)


def load_extra_jobs(log):
    """Postings saved as JSON in extra_jobs/ (e.g. from a research session). Old files are skipped."""
    out = []
    if not EXTRA_DIR.exists():
        return out
    cutoff = dt.date.today() - dt.timedelta(days=EXTRA_MAX_AGE_DAYS)
    for f in sorted(EXTRA_DIR.glob("*.json")):
        try:
            data = json.loads(f.read_text())
        except Exception as e:
            log(f"  ! couldn't read {f.name}: {e}")
            continue
        fresh = [j for j in data if (j.get("found") or "9999") >= cutoff.isoformat()]
        out.extend(fresh)
        log(f"  {f.name}: {len(fresh)} saved postings")
    return out


def gather_jobs(opts, log=print) -> list[dict]:
    cfg = json.loads((HERE / "companies.json").read_text())
    tasks = [(fetch_greenhouse, s) for s in cfg.get("greenhouse", [])] + \
            [(fetch_lever, s) for s in cfg.get("lever", [])] + \
            [(fetch_ashby, s) for s in cfg.get("ashby", [])]

    CACHE_DIR.mkdir(exist_ok=True)
    cache_file = CACHE_DIR / "board_jobs.json"
    cfg_sig = json.dumps(cfg, sort_keys=True)
    sig_file = CACHE_DIR / "board_jobs.sig"
    jobs: list[dict] = []
    fresh = (cache_file.exists() and sig_file.exists() and sig_file.read_text() == cfg_sig
             and (time.time() - cache_file.stat().st_mtime) < CACHE_TTL_HOURS * 3600)
    if fresh and not opts.get("refresh"):
        jobs = json.loads(cache_file.read_text())
        age = int((time.time() - cache_file.stat().st_mtime) / 60)
        log(f"Using job-board postings scanned {age} min ago ({len(jobs)} jobs).")
    else:
        log(f"Scanning {len(tasks)} employer job boards...")

        def run(t):
            fn, slug = t
            try:
                return slug, list(fn(slug)), None
            except Exception as e:  # one broken board shouldn't stop the scan
                return slug, [], e

        failed = []
        with cf.ThreadPoolExecutor(16) as ex:
            for (fn, slug), (_, got, err) in zip(tasks, ex.map(run, tasks)):
                if err:
                    failed.append((fn, slug))
                jobs.extend(got)
        # Retry failures slowly (common right after the Mac wakes, before the network settles).
        for attempt in (1, 2):
            if not failed:
                break
            time.sleep(10 * attempt)
            retry, failed = failed, []
            with cf.ThreadPoolExecutor(4) as ex:
                for t, (slug, got, err) in zip(retry, ex.map(run, retry)):
                    (failed.append(t) if err else jobs.extend(got))
        for _, slug in failed:
            log(f"  ! skipped {slug} (no response after 3 tries)")
        cache_file.write_text(json.dumps(jobs))
        sig_file.write_text(cfg_sig)
        log(f"  fetched {len(jobs)} postings")

    jobs.extend(load_extra_jobs(log))
    queries = search_queries(opts)

    app_id, app_key = os.environ.get("ADZUNA_APP_ID"), os.environ.get("ADZUNA_APP_KEY")
    if app_id and app_key and queries:
        try:
            ad = list(fetch_adzuna(queries, app_id, app_key))
            log(f"  Adzuna: {len(ad)} postings")
            jobs.extend(ad)
        except Exception as e:
            log(f"  ! Adzuna failed: {e}")

    if opts.get("web", True) and queries:
        key = os.environ.get("TAVILY_API_KEY")
        log(f"Searching LinkedIn, Indeed, Built In NYC and others via Tavily ({len(queries)} searches"
            f"{'' if key else ', keyless mode: set TAVILY_API_KEY for higher limits'})...")
        try:
            web = list(fetch_tavily(queries, key))
            log(f"  Tavily: {len(web)} postings")
            jobs.extend(web)
        except Exception as e:
            log(f"  ! Tavily search failed: {str(e)[:160]}")

    # location filter + de-dup
    seen, out = set(), []
    for j in jobs:
        if j["source"] not in ("Adzuna", "Web (Tavily)") and not location_ok(j["location"], opts.get("include_remote")):
            continue
        key = (j["company"].lower(), re.sub(r"\W+", " ", j["title"].lower()).strip())
        if key in seen or j["url"] in seen:
            continue
        seen.update([key, j["url"]])
        out.append(j)
    return out


def search_queries(opts) -> list[str]:
    """Job-site searches: the user's own titles if given, else two per selected category."""
    titles = [t.strip() for t in (opts.get("titles") or "").split(",") if t.strip()]
    if titles:
        return titles
    out = []
    for c in opts.get("categories") or []:
        if c in CATEGORIES:
            out.extend(CATEGORIES[c][2][:2])
    return out


# --------------------------------------------------------------------------
# Local scoring
# --------------------------------------------------------------------------
def tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z][a-z0-9+#.\-]{1,}", text.lower()) if t not in STOP and len(t) > 2]


# Cheap substring that must appear before a skill's regex is worth running (big speed-up on long postings).
SKILL_KEYS = {s: re.split(r"[\s/]", s.replace("\\", ""))[0].lower() for s in SKILLS}


def find_skills(text: str, strict: bool = False) -> set[str]:
    low = text.lower()
    found = set()
    for s, rx in SKILL_RES.items():
        if strict and s in AMBIGUOUS:
            continue
        if SKILL_KEYS[s] in low and rx.search(text):
            found.add(s)
    return found


def required_years(desc: str) -> int | None:
    """The experience a posting asks for: the largest 'N+ years ... experience' it states (0 for '0-2 years')."""
    desc = re.sub(r"\b(" + "|".join(NUM_WORDS) + r")\b(?=\s*(?:\(\d+\)\s*)?\+?\s*(?:years?|yrs?))",
                  lambda m: str(NUM_WORDS[m.group(1).lower()]), desc, flags=re.I)
    desc = re.sub(r"\b(\w+)\s*\((\d{1,2})\)", r"\2", desc)   # "three (3) years" -> "3 years"
    vals = [int(m.group(1)) for m in YEARS_REQ_RE.finditer(desc) if int(m.group(1)) <= 15]
    return max(vals) if vals else None


def job_level(title: str, desc: str, req: int | None) -> str:
    if SENIOR_RE.search(title) or (req or 0) >= 5:
        return "senior"
    if re.search(r"\b(experienced (?:professionals?|hires?)|lateral)\b", title, re.I):
        return "mid"
    if req is not None and req >= 2:          # a stated requirement beats an entry-sounding title
        return "early" if req == 2 else "mid"
    if JUNIOR_RE.search(title) or ENTRY_TEXT_RE.search(desc) or req is not None:
        return "entry"
    return "unstated"


TECH_TITLE_RE = re.compile(r"\b(engineer|engineering|developer|scientist|swe|sre|devops|architect|programmer)\b", re.I)
CODING_SKILLS = {"python", "java", "javascript", "typescript", "go", "golang", "rust", "c++", "c#", "scala", "kotlin",
                 "swift", "ruby", "php", "react", "node.js"}
CONTRACT_RE = re.compile(r"\b(contract|contractor|temporary|temp|fixed[- ]term|part[- ]time|freelance|seasonal|"
                         r"\$\d+\s*/\s*h(ou)?r)\b", re.I)


def categorize(title: str) -> list[str]:
    return [k for k, rx in CATEGORY_RES.items() if rx.search(title)]


def score_jobs(jobs, resume, opts):
    years = opts["years"] if opts.get("years") not in (None, "") else estimate_years(resume)
    years = float(years)
    resume_skills = find_skills(resume)
    targets = [t.strip().lower() for t in (opts.get("titles") or "").split(",") if t.strip()]
    resume_tok = set(tokens(resume))
    selected = set(opts.get("categories") or [])

    # TF-IDF over the job corpus + resume
    docs = [Counter(tokens(j["title"] + " " + j["title"] + " " + j["description"])) for j in jobs]
    rdoc = Counter(tokens(resume))
    df = Counter()
    for d in docs + [rdoc]:
        df.update(d.keys())
    n = len(docs) + 1
    idf = {t: math.log((n + 1) / (c + 1)) + 1 for t, c in df.items()}

    def vec(c):
        v = {t: (1 + math.log(f)) * idf[t] for t, f in c.items()}
        norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
        return {t: x / norm for t, x in v.items()}

    rvec = vec(rdoc)
    sims = [sum(x * rvec.get(t, 0.0) for t, x in vec(d).items()) for d in docs]
    # Scale similarity against the distribution so it is comparable across resumes.
    top_sim = max(sorted(sims)[int(len(sims) * 0.98)] if sims else 1.0, 1e-6)

    for j, sim in zip(jobs, sims):
        text = j["title"] + "\n" + j["description"]
        all_found = find_skills(text)
        job_skills = (all_found - AMBIGUOUS) | (all_found & resume_skills & AMBIGUOUS)
        matched = sorted(job_skills & resume_skills)
        missing = sorted(job_skills - resume_skills)
        skill_cov = (len(matched) + 0.5) / (len(job_skills) + 1)

        title_tok = set(tokens(j["title"]))
        if targets:
            title_fit = max((len(set(tokens(t)) & title_tok) / max(len(set(tokens(t))), 1) for t in targets), default=0)
        else:
            title_fit = len(title_tok & resume_tok) / max(len(title_tok), 1)

        sem = min(sim / top_sim, 1.0)
        score = 100 * (0.45 * skill_cov + 0.35 * sem + 0.20 * title_fit)

        cats = categorize(j["title"])
        if selected & set(cats):
            score += 4

        notes = []
        req = required_years(j["description"])
        level = job_level(j["title"], j["description"], req)
        if req is not None and req > years + 0.5:
            gap = req - years
            score -= min(12 * gap, 40)
            notes.append(f"asks for {req}+ yrs (you have ~{years:g})")
        if SENIOR_RE.search(j["title"]) and years < 5:
            score -= 30 if years < 3 else 15
            notes.append("senior-level title")
        if level == "entry":
            score += 10 if years < 1.5 else 5 if years < 3 else -5
        elif level == "unstated" and years < 1.5:
            score -= 4   # no stated requirement: often fine, but less certain than an explicit entry-level role
        for role_re, cred_re in CREDENTIAL_ROLES:
            if role_re.search(j["title"]) and not cred_re.search(resume):
                score -= 35
                notes.append("needs a credential your resume doesn't show")
                break
        if TECH_TITLE_RE.search(j["title"]) and not (resume_skills & CODING_SKILLS):
            score -= 25
            notes.append("engineering role; resume shows no programming")
        if j.get("snippet_only"):
            notes.append("short description, so the score is rougher")

        j.update(score=int(max(0, min(100, round(score)))), matched=matched, missing=missing[:12],
                 req_years=req, level=level, notes="; ".join(notes), categories=cats)
    return years, resume_skills


# --------------------------------------------------------------------------
# Company outlook: company health, industry potential, job security
# --------------------------------------------------------------------------
PROFILES_FILE = HERE / "company_profiles.json"
NEUTRAL = 60


def _norm(name: str) -> str:
    n = re.sub(r"[^a-z0-9& ]+", " ", (name or "").lower())
    n = re.sub(r"\b(the|inc|llc|lp|l p|ltd|plc|corp|corporation|co|company|group|pbc)\b", " ", n)
    return re.sub(r"\s+", " ", n).strip()


def load_profiles():
    try:
        data = json.loads(PROFILES_FILE.read_text())
    except (OSError, ValueError):
        return {}, []
    entries = []
    for c in data.get("companies", []):
        for alias in c.get("names", []):
            entries.append((_norm(alias), c))
    entries.sort(key=lambda e: len(e[0]), reverse=True)  # longest alias wins
    return data.get("industries", {}), entries


def find_profile(company: str, entries):
    n = _norm(company)
    for alias, prof in entries:
        if alias and (n == alias or n.startswith(alias + " ")):
            return prof
    return None


def apply_outlook(jobs, weight: float):
    """Blend job fit with company outlook. weight = share of the final score given to the company (0-1)."""
    industries, entries = load_profiles()
    for j in jobs:
        prof = find_profile(j["company"], entries)
        if prof:
            ind = industries.get(prof.get("industry"), {})
            health, security = prof.get("health", NEUTRAL), prof.get("security", NEUTRAL)
            industry = ind.get("outlook", NEUTRAL)
            info = {"type": prof.get("type", ""), "basis": prof.get("basis", "estimate"), "notes": prof.get("notes", ""),
                    "industry": ind.get("label", ""), "industry_note": ind.get("note", ""),
                    "sources": prof.get("sources", []), "sayari": prof.get("sayari")}
        else:
            health = industry = security = NEUTRAL
            info = {"type": "", "basis": "not researched", "notes": "Not researched yet; neutral rating used.",
                    "industry": "", "industry_note": "", "sources": [], "sayari": None}
        text = j["title"] + " " + j["description"][:3000]
        if CONTRACT_RE.search(j["title"]) or re.search(r"\b(contract role|fixed[- ]term|temporary position)\b", text, re.I):
            security = max(0, security - 20)
            info["notes"] = (info["notes"] + " Contract or temporary role, so security is lower.").strip()
        outlook = round(0.4 * health + 0.3 * industry + 0.3 * security)
        fit = j.get("ai_score", j["score"])
        j.update(fit=fit, outlook=outlook, company_health=health, industry_outlook=industry, job_security=security,
                 company_info=info, final_score=int(round((1 - weight) * fit + weight * outlook)))


# --------------------------------------------------------------------------
# Optional Claude scoring
# --------------------------------------------------------------------------
AI_SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "integer", "description": "0-100 fit score"},
        "verdict": {"type": "string", "enum": ["strong match", "good match", "stretch", "poor fit"]},
        "strengths": {"type": "array", "items": {"type": "string"}},
        "gaps": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
    },
    "required": ["score", "verdict", "strengths", "gaps", "summary"],
    "additionalProperties": False,
}

AI_SYSTEM = """You are an experienced recruiter screening jobs for one candidate in New York City.
For the job posting you are given, judge how qualified the candidate is, based only on their resume below.

Scoring guide:
- 85-100: meets essentially every hard requirement; would likely get an interview.
- 70-84: meets most requirements; one or two gaps that are easy to bridge.
- 50-69: plausible stretch; several gaps but a real case can be made.
- 0-49: missing core requirements (wrong field, far too senior, required license or degree absent).
Weigh hard requirements (years, degree, required tools, work authorization) above nice-to-haves.
Keep strengths and gaps to at most 4 short items each, and the summary to one or two sentences
addressed to the candidate.

<resume>
{resume}
</resume>"""


def ai_status() -> tuple[bool, str]:
    if sys.version_info < (3, 10):
        return False, ("Claude scoring needs Python 3.10+ (this is %d.%d). Install one with `brew install python`, "
                       "then start the app with python3.12 or similar." % sys.version_info[:2])
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False, "Claude scoring needs the Anthropic SDK: run `pip install anthropic`."
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
            or (Path.home() / ".config" / "anthropic").exists()):
        return False, "Claude scoring needs an API key: set ANTHROPIC_API_KEY before starting the app."
    return True, "ready"


def ai_score(jobs, resume, n, model, log=print):
    ok, why = ai_status()
    if not ok:
        log(why + " Skipping Claude scoring.")
        return
    import anthropic
    client = anthropic.Anthropic()
    system = [{"type": "text", "text": AI_SYSTEM.format(resume=resume), "cache_control": {"type": "ephemeral"}}]
    picked = jobs[:n]
    log(f"Asking Claude ({model}) to score the top {len(picked)} matches...")

    def one(j):
        msg = (f"Job title: {j['title']}\nCompany: {j['company']}\nLocation: {j['location']}\n"
               f"Salary: {j['salary'] or 'not listed'}\n\nPosting:\n{j['description']}")
        resp = client.beta.messages.create(
            model=model,
            max_tokens=4000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=system,
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": AI_SCHEMA}},
            messages=[{"role": "user", "content": msg}],
        )
        if resp.stop_reason == "refusal":
            raise RuntimeError("model declined")
        text = next(b.text for b in resp.content if b.type == "text")
        return json.loads(text)

    with cf.ThreadPoolExecutor(4) as ex:
        futs = {ex.submit(one, j): j for j in picked}
        for f in cf.as_completed(futs):
            j = futs[f]
            try:
                r = f.result()
                j.update(ai_score=max(0, min(100, int(r["score"]))), ai_verdict=r["verdict"],
                         ai_strengths=r["strengths"], ai_gaps=r["gaps"], ai_summary=r["summary"])
            except anthropic.AuthenticationError:
                log("  ! Claude rejected the credentials. Check ANTHROPIC_API_KEY.")
                ex.shutdown(cancel_futures=True)
                return
            except Exception as e:
                log(f"  ! couldn't score '{j['title']}' at {j['company']}: {e}")


# --------------------------------------------------------------------------
# One full search (used by the CLI and the web app)
# --------------------------------------------------------------------------
def run_search(resume: str, opts: dict, log=print):
    resume = resume.strip()
    if len(resume) < 200:
        raise ValueError("The resume text is too short. Paste or upload the full resume.")
    jobs = gather_jobs(opts, log)

    excl = [w.strip().lower() for w in (opts.get("exclude") or "").split(",") if w.strip()]
    if excl:
        jobs = [j for j in jobs if not any(re.search(rf"\b{re.escape(w)}\b", j["title"].lower()) for w in excl)]

    years, skills = score_jobs(jobs, resume, opts)
    selected = set(opts.get("categories") or [])
    if selected and not opts.get("include_other", True):
        jobs = [j for j in jobs if selected & set(j["categories"])]
    log(f"{len(jobs)} NYC postings scored. Resume: ~{years:g} yrs experience, {len(skills)} recognized skills.")

    jobs.sort(key=lambda j: j["score"], reverse=True)
    if opts.get("ai"):
        ai_score(jobs, resume, int(opts.get("ai_top") or 30), opts.get("model") or "claude-opus-5-5", log)
    weight = opts.get("company_weight")
    weight = 0.25 if weight in (None, "") else max(0.0, min(1.0, float(weight)))
    apply_outlook(jobs, weight)
    jobs.sort(key=lambda j: j["final_score"], reverse=True)
    jobs = [j for j in jobs if j["final_score"] >= int(opts.get("min_score") or 0)]

    meta = {"total": len(jobs), "date": dt.date.today().isoformat(), "years": years,
            "skills": sorted(skills), "ai": any("ai_score" in j for j in jobs), "company_weight": weight,
            "categories": {k: v[0] for k, v in CATEGORIES.items()}}
    return jobs, meta


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------
PUBLIC_FIELDS = ("final_score", "fit", "score", "ai_score", "ai_verdict", "ai_strengths", "ai_gaps", "ai_summary",
                 "outlook", "company_health", "industry_outlook", "job_security", "company_info", "title",
                 "company", "location", "salary", "posted", "url", "matched", "missing", "notes", "source",
                 "categories", "new", "first_seen", "level", "req_years")


def public(jobs):
    return [{k: j.get(k) for k in PUBLIC_FIELDS} for j in jobs]


def write_csv(jobs, path):
    cols = ["final_score", "fit", "outlook", "company_health", "industry_outlook", "job_security", "ai_verdict",
            "new", "first_seen", "title", "company", "location", "salary", "posted", "url", "categories", "matched", "missing", "notes",
            "company_notes", "ai_summary", "source"]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for j in jobs:
            w.writerow({**j, "matched": ", ".join(j.get("matched") or []), "missing": ", ".join(j.get("missing") or []),
                        "categories": ", ".join(CATEGORIES[c][0] for c in j.get("categories") or []),
                        "company_notes": (j.get("company_info") or {}).get("notes", "")})


def write_html(jobs, path, meta):
    data = json.dumps(public(jobs)).replace("</", "<\\/")
    page = HTML_TEMPLATE.replace("__DATA__", data).replace("__META__", json.dumps(meta).replace("</", "<\\/"))
    path.write_text(page)


HTML_TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>NYC Job Matches</title>
<style>
:root{--bg:#f7f7f5;--card:#fff;--ink:#1d1d1f;--muted:#6b6b70;--line:#e4e4e0;--accent:#2457d6;
--hi:#1f8a4c;--mid:#b7791f;--lo:#b33a3a;--chip:#eef2fb}
@media (prefers-color-scheme:dark){:root{--bg:#141416;--card:#1d1d20;--ink:#ececee;--muted:#9a9aa2;
--line:#2e2e33;--accent:#7aa2ff;--hi:#4cc184;--mid:#e0a84a;--lo:#ef7171;--chip:#252a38}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,sans-serif}
.wrap{max-width:1100px;margin:0 auto;padding:28px 16px 60px}
h1{font-size:24px;margin:0 0 4px}.sub{color:var(--muted);margin:0 0 20px}
.bar{display:flex;flex-wrap:wrap;gap:10px;margin-bottom:16px;align-items:center}
input,select{font:inherit;padding:8px 10px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--ink)}
input[type=search]{flex:1;min-width:200px}
.count{color:var(--muted);font-size:13px}
.job{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin-bottom:10px;
display:grid;grid-template-columns:64px 1fr;gap:14px}
.score{font-size:24px;font-weight:700;text-align:center;line-height:1}
.score small{display:block;font-size:11px;font-weight:500;color:var(--muted);margin-top:4px}
.meter{height:5px;border-radius:3px;background:var(--line);margin-top:8px;overflow:hidden}
.meter i{display:block;height:100%}
.t{font-size:16px;font-weight:600;color:var(--accent);text-decoration:none}.t:hover{text-decoration:underline}
.m{color:var(--muted);font-size:13px;margin:2px 0 8px}
.chips{display:flex;flex-wrap:wrap;gap:5px;margin:4px 0}
.chip{font-size:12px;padding:2px 8px;border-radius:999px;background:var(--chip)}
.chip.miss{background:transparent;border:1px dashed var(--line);color:var(--muted)}
.ai{font-size:13px;margin-top:8px;padding-top:8px;border-top:1px solid var(--line)}
.ai b{text-transform:capitalize}.note{font-size:12px;color:var(--mid)}
@media (max-width:560px){.job{grid-template-columns:1fr}.score{text-align:left}}
</style></head><body><div class="wrap">
<h1>NYC Job Matches</h1><p class="sub" id="sub"></p>
<div class="bar"><input type="search" id="q" placeholder="Filter by title, company, skill…">
<select id="min"><option value="0">Any score</option><option value="50">50+</option><option value="60" selected>60+</option>
<option value="70">70+</option><option value="80">80+</option></select>
<select id="sort"><option value="final_score">Sort: best match</option><option value="posted">Sort: newest</option>
<option value="company">Sort: company</option></select><span class="count" id="count"></span></div>
<div id="list"></div></div>
<script>
const JOBS=__DATA__, META=__META__;
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const col=s=>s>=75?"var(--hi)":s>=55?"var(--mid)":"var(--lo)";
document.getElementById("sub").textContent=`${META.total} NYC postings scored on ${META.date} · ~${META.years} yrs experience assumed · ${META.ai?"scores refined by Claude for the top matches":"keyword-based scores"}`;
function render(){
 const q=document.getElementById("q").value.toLowerCase(), min=+document.getElementById("min").value, by=document.getElementById("sort").value;
 let rows=JOBS.filter(j=>j.final_score>=min&&(!q||[j.title,j.company,j.location,...j.matched].join(" ").toLowerCase().includes(q)));
 rows.sort((a,b)=>by==="company"?a.company.localeCompare(b.company):by==="posted"?(b.posted||"").localeCompare(a.posted||""):b.final_score-a.final_score);
 document.getElementById("count").textContent=`${rows.length} shown`;
 document.getElementById("list").innerHTML=rows.slice(0,400).map(j=>`<div class="job">
 <div><div class="score" style="color:${col(j.final_score)}">${j.final_score}<small>${j.ai_score!=null?"Claude":"keyword"}</small></div>
 <div class="meter"><i style="width:${j.final_score}%;background:${col(j.final_score)}"></i></div></div>
 <div><a class="t" href="${esc(j.url)}" target="_blank" rel="noopener">${esc(j.title)}</a>
 <div class="m">${esc(j.company)} · ${esc(j.location)}${j.salary?" · "+esc(j.salary):""}${j.posted?" · posted "+esc(j.posted):""}</div>
 ${j.matched.length?`<div class="chips">${j.matched.map(s=>`<span class="chip">${esc(s)}</span>`).join("")}</div>`:""}
 ${j.missing.length?`<div class="chips">${j.missing.map(s=>`<span class="chip miss">${esc(s)}</span>`).join("")}</div>`:""}
 ${j.notes?`<div class="note">${esc(j.notes)}</div>`:""}
 <div class="m">Job fit ${j.fit} · Company outlook ${j.outlook} (health ${j.company_health}, industry ${j.industry_outlook}, security ${j.job_security})${j.company_info&&j.company_info.notes?": "+esc(j.company_info.notes):""}</div>
 ${j.ai_summary?`<div class="ai"><b>${esc(j.ai_verdict)}</b> (keyword score ${j.score}): ${esc(j.ai_summary)}
 ${j.ai_gaps?.length?`<br><span style="color:var(--muted)">Gaps: ${j.ai_gaps.map(esc).join("; ")}</span>`:""}</div>`:""}
 </div></div>`).join("")||"<p class='sub'>No matches at this score. Lower the filter.</p>";
}
["q","min","sort"].forEach(id=>document.getElementById(id).addEventListener("input",render));render();
</script></body></html>"""


# --------------------------------------------------------------------------
def main():
    p = argparse.ArgumentParser(description="Find NYC jobs that match your resume and score each one.")
    p.add_argument("resume", type=Path, help="resume file (.pdf, .docx, .txt, .md)")
    p.add_argument("--categories", default=",".join(CATEGORIES),
                   help="comma-separated: " + ", ".join(CATEGORIES) + " (default: all)")
    p.add_argument("--only-categories", action="store_true", help="drop jobs outside the chosen categories")
    p.add_argument("--titles", help='comma-separated target roles, e.g. "financial analyst, underwriter". '
                                    "Boosts matching titles and replaces the category web searches.")
    p.add_argument("--years", type=float, help="your years of experience (default: estimated from resume dates)")
    p.add_argument("--exclude", default="", help='comma-separated title words to drop, e.g. "senior,director,intern"')
    p.add_argument("--include-remote", action="store_true", help="also include US-remote postings")
    p.add_argument("--min-score", type=int, default=0, help="drop jobs below this score from the output files")
    p.add_argument("--company-weight", type=float, default=0.25,
                   help="share of the final score from company outlook (health, industry, job security); 0-1, default 0.25")
    p.add_argument("--ai", action="store_true", help="re-score the top matches with Claude (needs ANTHROPIC_API_KEY)")
    p.add_argument("--ai-top", type=int, default=30, help="how many top matches Claude scores (default 30)")
    p.add_argument("--model", default="claude-opus-5-5", help="Claude model for --ai")
    p.add_argument("--refresh", action="store_true", help="ignore the 6-hour cache and re-scan job boards")
    p.add_argument("--no-web", action="store_true", help="skip the Tavily web search of LinkedIn/Indeed/etc.")
    p.add_argument("--no-open", action="store_true", help="don't open the report in a browser")
    a = p.parse_args()

    try:
        resume = read_resume(a.resume)
    except (ValueError, OSError) as e:
        sys.exit(str(e))
    opts = dict(categories=[c.strip() for c in a.categories.split(",") if c.strip()],
                include_other=not a.only_categories, titles=a.titles, years=a.years, exclude=a.exclude,
                include_remote=a.include_remote, min_score=a.min_score, ai=a.ai, ai_top=a.ai_top,
                company_weight=a.company_weight,
                model=a.model, refresh=a.refresh, web=not a.no_web)
    try:
        jobs, meta = run_search(resume, opts)
    except ValueError as e:
        sys.exit(str(e))

    OUT_DIR.mkdir(exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y-%m-%d_%H%M")
    html_path, csv_path = OUT_DIR / f"matches_{stamp}.html", OUT_DIR / f"matches_{stamp}.csv"
    write_html(jobs, html_path, meta)
    write_csv(jobs, csv_path)

    print("\nTop matches:")
    for j in jobs[:20]:
        print(f"  {j['final_score']:>3}  {j['title'][:58]:<58}  {j['company'][:22]:<22}  {j['url']}")
    print(f"\nFull report: {html_path}\nSpreadsheet: {csv_path}")
    if not a.no_open:
        webbrowser.open(html_path.as_uri())


if __name__ == "__main__":
    main()
