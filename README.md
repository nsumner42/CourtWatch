# CourtWatch

Watches the Ontario daily court dockets on [ontariocourtdates.ca](https://www.ontariocourtdates.ca/)
for a name and emails you when it shows up on today's or tomorrow's list. The email includes the
courtroom's Zoom link (where one is known) and a calendar invite.

It checks Toronto (all three courthouses), Newmarket, Richmond Hill, Brampton, Milton, Burlington,
Oshawa and Lindsay by default. Court holidays are
detected and named in the email instead of being reported as a failure.

## Requirements

- Python 3 with `requests` and `beautifulsoup4`:
  ```
  pip install requests beautifulsoup4
  ```
- A Gmail account with an [app password](https://myaccount.google.com/apppasswords) for sending
  (any SMTP server works; Gmail is the default).
- Optional: a free [healthchecks.io](https://healthchecks.io/) check, so you hear about it when a run fails.

## Setup

1. Put `courtwatch.py` in a folder, for example `C:\Scripts\CourtWatch`.
2. Run it once. It creates `courtwatch_config.json` next to itself and stops:
   ```
   python courtwatch.py --watch
   ```
3. Fill in `courtwatch_config.json`:

   | Key | What it is |
   |---|---|
   | `smtp_user` | The Gmail address that sends the email |
   | `smtp_password` | That account's app password |
   | `smtp_from` | Sender display, e.g. `"CourtWatch <you@gmail.com>"` |
   | `email_to` | List of recipient addresses |
   | `search_term` | The name to look for (case-insensitive, matches anywhere in a docket row) |
   | `healthcheck_url` | Optional: your healthchecks.io ping URL, e.g. `https://hc-ping.com/<uuid>` |
   | `locations` | Optional: override which courthouses are searched (see `SEARCH_LOCATIONS` in the script) |

   This file holds your password and the name you're watching for, so keep it private.
4. Test it (sends a real email; `--force` ignores the "already alerted" memory):
   ```
   python courtwatch.py --watch --force
   python courtwatch.py --watch --force --term SomeOtherName
   ```

## Running it daily (Windows)

`courtwatch-run.ps1` is a wrapper for Task Scheduler. It runs `courtwatch.py --watch` and writes
`last-run-status.txt`. Edit `$root` at the top if your folder isn't `C:\Scripts\CourtWatch`, then create a
daily task (weekdays at 15:15 works well, since tomorrow's list is up by then) that runs:

```
powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\Scripts\CourtWatch\courtwatch-run.ps1
```

On Linux or macOS, a cron entry running `python3 courtwatch.py --watch` does the same.

## What the emails mean

- **"Court docket check: no matches for …"**: every run sends this as a heartbeat. If today or
  tomorrow has no court (a holiday), the subject says so and names the holiday.
- **A hearing alert** (subject like "Name Hearing (October 1st)"): the name was found. The email lists the
  docket row, the courtroom's Zoom link, and a calendar invite. The same row isn't alerted twice.
- **"CourtWatch: low/no rows from …"**: a courthouse returned far fewer rows than normal. Either
  the court is unusually quiet or the website changed. Either way, check the docket yourself.

Exit codes: `0` OK, `1` unexpected error (see `courtwatch_log.txt`), `2` too few rows scraped.

## Files it creates (keep these private)

`courtwatch_config.json`, `courtwatch_state.json` (already-alerted matches), `courtwatch_log.txt`,
`last-run-status.txt`. They're listed in `.gitignore`.

## Notes

- The Zoom links are collected from public defence-lawyer and court pages (sources are in comments in
  the script). Courts change them occasionally, so double-check before a hearing.
- If the website changes, the `--discover*` options print the site's form fields so the script can be updated.
- Personal project, provided as-is. Not affiliated with the Ontario courts.
