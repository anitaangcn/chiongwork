# ChiongWork

**Every Singapore job site in one feed, with the noise filtered out.**

ChiongWork pulls Singapore jobs from MyCareersFuture, company career pages and the LinkedIn / JobStreet / Indeed alert emails you already get. It puts them into one de-duplicated feed, then adds the things the job sites don't tell you:

- **Plain-English verdicts:** *Apply early*, *Good bet*, *Crowded*, *Possible ghost job*, *Mass-hiring ad*, *Hype-style sales ad*.
- **Real pay data:** what a role pays, and where each job sits against similar jobs.
- **CV match:** a match score on every job, plus the skills you're missing.
- **Commute:** a rough travel time from your home, with a filter and a *Closest to home* sort.
- **A proper application tracker:** saved ad copies, follow-up reminders, interviews and stats.

It runs on your own computer as a small local web app. There's no account and no server, and your data stays with you.

> Unofficial personal project. Not affiliated with MyCareersFuture, Workforce Singapore, LinkedIn, JobStreet, Indeed or Glassdoor.

## Quick start

You need **Python 3.11 or newer** ([python.org/downloads](https://www.python.org/downloads/); on Windows, tick *Add python.exe to PATH*).

**Windows**
1. Download this repo: *Code → Download ZIP*, then unzip it. Or `git clone` it.
2. Double-click **`Setup.cmd`** once. It creates a private Python environment and installs two small libraries.
3. Double-click **`ChiongWork.cmd`**. The app opens at <http://127.0.0.1:8700>. Close the black window to stop it.

**macOS / Linux**
```sh
git clone https://github.com/anitaangcn/chiongwork.git
cd chiongwork
chmod +x chiongwork.sh
./chiongwork.sh            # first run sets itself up, then opens the app
```

**First run:** the app starts empty. Click **Get jobs now**; the first download takes about 2 minutes. Then tell it what you're looking for: roles, minimum pay, level and, optionally, your postal code. Later:
- Upload your CV on **My CV**.
- Connect Gmail on **Email alerts** to pull in LinkedIn, JobStreet and Indeed jobs.
- Use **Full sweep** in the sidebar (about 20 minutes) to load all of MyCareersFuture.

## Where jobs come from

| Source | How | Notes |
|---|---|---|
| **MyCareersFuture** (gov.sg) | Its public JSON API | About 66–90k active jobs. Almost all have a salary range, applicant count, repost count, expiry date and workplace location. |
| **Company career pages** | Public job-board APIs from Greenhouse, Lever, Ashby and SmartRecruiters | 48 companies with Singapore roles are in `companies.json` (Grab, Stripe, Wise, Airwallex, Databricks…). Add more there. |
| **LinkedIn / JobStreet / Indeed / Glassdoor** | **Your own alert emails** | These sites forbid scraping, so ChiongWork never contacts them. It reads the job alerts they email you: a connected Gmail (read-only) or `.eml`/`.mbox` files. |

When the same job appears in several places (same company, near-identical title, same seniority), it is merged into one row. If a LinkedIn or career-page job matches a MyCareersFuture posting, it gets the MyCareersFuture salary and applicant count.

## What's in the app

- **For you:** your home page.
  - Summary tiles, and how many ghost jobs and hype ads were kept out.
  - Shelves: *Best matches for your CV*, *Best bets*, *Apply early* (posted in the last 3 days, 10 or fewer applicants), *You won't find these on MyCareersFuture*, *Highest paying* and *Closing this week*.
- **Search jobs:** one-click filters (Best bets, Apply early, Not on MyCareersFuture, Strong CV match, Direct employers, Hide ghost jobs & hype ads…), pay, commute, level and site filters, and a pay summary for whatever you searched.
- **Every job** gets a verdict. Click a job to see:
  - how well you match
  - the commute
  - where its pay sits against similar jobs
  - a pros-and-cons list
  - every site it's listed on, with an Apply button that prefers the company's own site
- **My CV:** upload your CV (PDF, .docx or pasted text) once. Every job then gets a match score, with the skills you have and the ones you're missing.
  - Matching runs on your computer only: no AI service, nothing is uploaded.
  - Skills come from the ~14,000 skills employers tag on MyCareersFuture. Specific skills (SIEM, IFRS 9) count far more than general ones (Excel, teamwork), and the job title is compared with your CV too.
  - Edit the skills it found, set your years of experience, and see which skills employers keep asking for that aren't on your CV.
- **Commute:** add your home postal code. Every job with an address shows a rough public-transport time ("~35 min away"). You can filter (≤ 30/45/60 min), sort by *Closest to home*, or set a longest commute for your feed.
  - Worked out offline: your postal code is placed using the job addresses MyCareersFuture publishes. Time is estimated from straight-line distance (about 8 min plus 2.7 min per km). Each job links to Google Maps for the exact route, and only the workplace is sent.
  - LinkedIn, Indeed and career-page jobs have no address, so they show no time and are never filtered out for it.
- **Market pay:** what a role really pays in Singapore right now.
  - Median, the middle-half range and the top 10%.
  - A pay chart, pay by seniority, and who's hiring the most.
- **Tracker:** every job you save keeps its own copy of the ad, so you still have the description after the site deletes it.
  - Click **Apply** on a job and, when you come back, ChiongWork asks whether you applied, then logs the date.
  - 7 days after applying with no reply, it nudges you to follow up. After 30 days with no news, the job moves to *No reply* by itself.
  - Each application has interviews (with an *Add to calendar* file), contact details, the CV version you sent, pay asked and offered, notes and a timeline.
  - Board and list views. Stats: applications per week, reply rate, interview rate, typical reply time, and which sites actually reply.
  - **Add a job** covers jobs found anywhere else (Telegram groups, referrals, recruiter messages).
- **Email alerts:** connect Gmail once with an [app password](https://myaccount.google.com/apppasswords).
  - Every refresh reads new LinkedIn, JobStreet, Indeed and Glassdoor alert emails. The first check covers the last 30 days.
  - The mailbox is opened read-only, and only emails from those sites are downloaded.
  - The app password is kept in your system's password store (Windows Credential Manager, macOS Keychain or Linux Secret Service), never in a file.
- **Light or dark mode**, and a phone-friendly layout.
- Keyboard: `/` search · `j`/`k` next/previous job · `s` save · `o` open · `Esc` close.

The app only listens on `127.0.0.1`, so other devices can't reach it.

## Your data and privacy

| What | Where | Notes |
|---|---|---|
| Your profile, applications, CV text, home postal code, connected Gmail address | `data/my-chiongwork.json` in the app folder | Small. Ignored by git, so it never ends up on GitHub. If the job cache is lost, your tracker is rebuilt from this file. |
| Gmail app password | Your system's password store | Never written to a file. |
| The job cache (~300 MB) | `~/.sgjobs/sgjobs.db` (your home folder) | Kept outside the app folder so cloud-sync tools don't fight over a live database. Re-downloadable, so it's safe to delete. Set `SGJOBS_CACHE` to move it. |

Nothing is sent anywhere except the requests that fetch jobs (MyCareersFuture's API and the companies' public job boards) and, if you connect it, Gmail.

## Command line

Windows: `.\cw.cmd …` · macOS/Linux: `./chiongwork.sh …`

```text
cw fetch                      # newest ~1,000 MCF jobs + your roles + all career pages (~2 min)
cw fetch --full               # sweep ALL of MyCareersFuture (~20 min; do it occasionally)
cw search "data analyst" --min-salary 5000 --direct --max-applicants 30
cw search --new               # only what appeared in the latest fetch
cw show 123                   # details + every site it's listed on
cw open 123                   # open in browser (prefers the company's own page)
cw save 123 ; cw status 123 applied --note "referred by Wei"
cw tracker                    # your pipeline
cw browse                     # interactive mode
cw keywords add "ux designer" # extra MyCareersFuture searches on every refresh
cw gmail-check                # read new alert emails from your connected Gmail
cw import-emails path\to\alerts  # .eml files, a folder of them, or a Google Takeout .mbox
cw web                        # the web app
```

## How the verdicts decide

- **Possible ghost job:** open 60 or more days and re-posted, or re-posted 3 or more times.
- **Mass-hiring ad:** the same ad appears as 3 or more MyCareersFuture listings.
- **Hype-style sales ad:** either an emoji plus sales/events wording, or tells like "travel perks", "youngsters", "earn & learn" or "no experience" next to sales. A company with 3 or more such ads gets all its sales and marketing ads flagged too. This list is recomputed after every refresh.
- **Best bets** ranking: freshness, few applicants, a direct employer, responsive, pay shown, and listed on the company's own site. Ghost, mass and hype ads are pushed down.

## Files

- `sgjobs/sources/mcf.py`, `ats.py`, `email_alerts.py`: one module per source.
- `sgjobs/db.py`: SQLite job cache and the merge/de-duplication logic.
- `sgjobs/feed.py`: search, verdicts, the best-bet score and pay statistics.
- `sgjobs/cv.py`: CV reading and job match scores. `sgjobs/geo.py`: home location and commute estimates.
- `sgjobs/tracker.py`: applications: saved ad copies, timeline, interviews (.ics), follow-ups, stats.
- `sgjobs/gmail.py`: reads job-alert emails from a connected Gmail (read-only).
- `sgjobs/userdata.py`: your data file.
- `sgjobs/web.py` + `sgjobs/web/index.html`: the local web app (standard library HTTP server, no framework). `serve.py` starts it without opening a browser.
- `sgjobs/__main__.py`: the command line.
- `config.json`: default MyCareersFuture searches and page counts. `companies.json`: career pages to poll.
- `tests/`: synthetic alert emails for the email parser (`make_fixtures.py`).

## Known limits

- **Unofficial API:** the MyCareersFuture API is public but undocumented, so it could change without notice. Please keep the default request delay and don't hammer it.
- **Hourly pay:** pay under $100 is treated as an hourly rate, because employers often type hourly rates into MyCareersFuture's "monthly" field.
- **Alert-email jobs** drop off 30 days after the email, since emails don't say when a job closes.
- **Cross-site matching** needs the same company name stem. For example, LinkedIn's "PwC" won't match "PricewaterhouseCoopers".
- **Agency filter:** the *direct employers only* filter relies on MyCareersFuture's industry code for recruitment agencies, so a few agencies slip through.
- **Commute times** are straight-line estimates, not routes. About 60% of MyCareersFuture jobs give a workplace address.
- **CV matching** is keyword-based. Scanned (image-only) PDFs can't be read; paste the text instead.
- **Email parser:** checked against real LinkedIn and Indeed alerts. JobStreet and Glassdoor layouts are only tested with synthetic emails.
- **Career pages:** Workday and SuccessFactors sites (used by many banks and government-linked companies) aren't supported yet.

## Licence

MIT. See [LICENSE](LICENSE).
