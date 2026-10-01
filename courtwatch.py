#!/usr/bin/env python3
"""
courtwatch.py - Watch ontariocourtdates.ca (Toronto) for a name and email you
when it appears on the daily docket.

CONFIRMED SITE FLOW (found via --discover* modes against the live site):

  1. GET the landing page (e.g. https://www.ontariocourtdates.ca/tomorrow/)
  2. POST the agree checkbox (ctl00$MainContent$chkAgree=on) + ENTER button
     -> redirects to daily-docket.aspx, the real search form.
  3. POST a city selection (ctl00$MainContent$ddlCity=Toronto) with
     __EVENTTARGET set to that field -> this is a cascading-dropdown
     postback that populates ddlLob and listBoxCourtOffice with
     Toronto-specific options (the 3 Toronto courthouses).
  4. POST the final search: court type, city, line-of-business, courthouse
     office selection, and the SUBMIT button -> this returns the actual
     results table.

Toronto courthouse listBoxCourtOffice values (confirmed):
    'Toronto'   = "--- All Below ---"  (all 3 at once - used by default)
    'M5G1T30'   = 361 University Ave
    'M7A0B90'   = 10 Armoury St.
    'M8Z5X60'   = 2201 Finch Ave W

USAGE
-----
  Discovery (already done for you, kept here for reference / re-verifying
  if the site changes):
      python courtwatch.py --discover URL
      python courtwatch.py --discover-search URL
      python courtwatch.py --discover-city URL

  Test run against a specific name without editing config:
      python courtwatch.py --watch --term SomeName

  Repeated testing without the state file blocking re-alerts: --force makes
  this run ignore AND not modify courtwatch_state.json, so the same match
  can be re-sent every time you run it, as many times as you want, without
  having to delete the state file in between. Must be combined with --watch
  (on its own, --force does nothing):
      python courtwatch.py --watch --force
      python courtwatch.py --watch --force --term SomeName

  Real run (uses SEARCH_TERM from courtwatch_config.json):
      python courtwatch.py --watch
"""

import argparse
import json
import logging
import re
import smtplib
import sys
import traceback
from urllib.parse import quote
from datetime import datetime, date, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# Bump this whenever you change behaviour. It is written to courtwatch_log.txt
# on every run and printed by --version, so a log line or an email always tells
# you which copy of the script actually produced it.
#   2.0.0 - external config, Zoom links, calendar invites (recovered version).
#   2.1.0 - healthchecks.io pings, and 0 scraped rows now fails loudly instead
#           of reporting a false all-clear.
#   2.2.0 - searches multiple courthouses (Toronto + Lindsay), Lindsay Zoom
#           links, and the 0-row check is now per location rather than on the
#           total, so a small court failing cannot hide behind a large one.
#   2.3.0 - per-location row thresholds (min_rows) instead of a single global
#           floor, scaled by how many court days a run covers so Friday's
#           Saturday scrape does not look like a failure every week.
#   2.4.0 - thresholds are now per DAY (min_rows_per_day), checked against each
#           scrape individually, and the weekday/weekend decision reads the date
#           the results page itself prints instead of assuming what "tomorrow"
#           means. The page's date is logged every run.
#   2.5.0 - adds Newmarket (50 Eagle St W) with its OCJ Zoom links. Room
#           lookup now tries the room as printed first (spaces removed, leading
#           zeros stripped, so Newmarket's "0101" finds "101" and "100V" stays
#           distinct from "100"); Newmarket is exact-match only, so a Superior
#           Court room like "CTRM 202" can never borrow an OCJ link.
#   2.5.1 - Newmarket min_rows_per_day set to 400.
#   2.6.0 - court holidays. An HTTP error on one search no longer aborts the
#           run (the site returns 500 for a court with no docket). If EVERY
#           location is low/failed for one date while another date is healthy,
#           that date is treated as "no court (likely a holiday)": the normal
#           email goes out saying so and healthchecks gets OK. Matches are now
#           emailed before the failure check, so a hit is never dropped just
#           because another scrape came back low. The holiday's name comes from
#           canada-holidays.ca, and a named holiday also counts as closed when
#           there is no healthy date to compare with (Good Friday + Saturday).
#           The search term and sender name are now placeholders in the script;
#           the real values live only in courtwatch_config.json.
#   2.7.0 - more GTA courthouses: Richmond Hill, Brampton (both Hurontario
#           St courthouses), Milton, Burlington and Oshawa. Zoom links added
#           for Brampton, Milton and Oshawa (OCJ rooms that two sources agree
#           on, exact room match only); Richmond Hill and Burlington have none.
#           Their thresholds are first guesses from one day's counts and need
#           retuning from the log.
#   2.7.1 - floors retuned to roughly 40% of the lowest day seen so far (the
#           first Thursday/Friday pair, 2026-10-01/02). Friday dockets are
#           much lighter: Milton's Friday was 146 rows (98 OCJ + 48 SCJ,
#           verified complete), under its old floor of 150.
__version__ = "2.7.1"

# ============================== CONFIG ======================================

SEARCH_TERM = "Your Name"   # placeholder - the real term comes from "search_term" in courtwatch_config.json; case-insensitive substring match against each result row

# If True, send an email every run even when nothing new is found - useful as
# a "heartbeat" so you know the script is still running/reaching the site.
# If False (default), you only get emailed when there's an actual new match.
ALWAYS_SEND_EMAIL = True

# Landing pages (the ones with the agree checkbox). Not daily-docket.aspx directly.
DOCKET_URLS = {
    "today": "https://www.ontariocourtdates.ca/",
    "tomorrow": "https://www.ontariocourtdates.ca/tomorrow/",
}

# --- Confirmed field names/values from --discover* runs ---
AGREE_CHECKBOX_NAME = "ctl00$MainContent$chkAgree"
ENTER_BUTTON_NAME = "ctl00$MainContent$btnEnter"
ENTER_BUTTON_VALUE = "ENTER"

CITY_FIELD_NAME = "ctl00$MainContent$ddlCity"
CITY_VALUE = "Toronto"

COURT_FIELD_NAME = "ctl00$MainContent$ddlCourt"
COURT_VALUE = "both"          # 'both' | 'scj' | 'ocj'

LOB_FIELD_NAME = "ctl00$MainContent$ddlLob"
LOB_VALUE = "0"               # '0' = All, '2' = Criminal (seen after selecting Toronto)

COURT_OFFICE_FIELD_NAME = "ctl00$MainContent$listBoxCourtOffice"
COURT_OFFICE_VALUE = "Toronto"   # "--- All Below ---" = all 3 Toronto courthouses at once

# Every (city, court office) pair searched on each run. Confirmed by selecting
# each city on daily-docket.aspx and reading the resulting listBoxCourtOffice
# options. Override in courtwatch_config.json with a "locations" list of
# {"label", "city", "office"} objects.
#
# Toronto's 'Toronto' office value is the "--- All Below ---" entry, covering
# all three Toronto courthouses in one search. Lindsay offers exactly one
# office, 440 Kent St W, which houses both the Superior Court and the Ontario
# Court of Justice - selecting ddlCourt='both' returns both courts' lists.
# min_rows_per_day is the floor for ONE court day at that location, checked
# separately for each date scraped - not against a multi-day total. Per-day is
# simpler to reason about and does not need rescaling when the number of dates
# changes. One global number cannot serve both locations: a normal Toronto day
# is ~2,000-2,400 rows and a normal Lindsay day ~20-45, two orders of magnitude
# apart.
#
# Since 2.7.1 the rule is: floor ~= 40% of the LOWEST day seen at that location.
# A broken scrape shows up as 0 rows, or as one court (OCJ or SCJ) or one
# courthouse dropping out, which is a much bigger fall than a quiet Friday.
# Lowest days seen (Thu 2026-10-01 / Fri 2026-10-02 unless noted):
#   Toronto 2,807 / 1,976 -> 800      Newmarket 731 / 452 -> 200
#   Brampton 1,165 / 1,007 -> 400     Milton 339 / 146 -> 60
#   Oshawa 477 / 383 -> 150           Richmond Hill 36 / 45 -> 10 (unchanged)
#   Burlington 16 / 25 -> 5 (unchanged)
#   Lindsay 15 is unchanged by the user's decision (Thu 256, Fri 79, but some
#   September days were in the teens).
#
# Toronto (pre-2.7.1: 1250): comfortably under a normal day, so it trips only when a day is
#   genuinely dead (holiday/long weekend) or the scrape broke - both wanted.
# Lindsay 15: measured days were 44 and 22. Under ~20 would be unusual, so 15
#   leaves a little headroom. It will still not reliably prove a court was
#   closed, but it will catch a broken scrape. Retune from courtwatch_log.txt,
#   which records per-location counts and the page's own date every run.
# Newmarket 400: one office (50 Eagle St W, both courts). Measured days were
#   961 (Tue 2026-09-29) and 633 (Thu 2026-10-01) rows, about 80% of them OCJ.
#   400 was chosen by the user; retune once more days are logged.
#
# The GTA courthouses added in 2.7.0 were measured on ONE day only (Thu
# 2026-10-01, the day after a court holiday, so possibly busier than usual).
# Each floor was roughly 40% of that count (2.7.0 first guesses, superseded by
# the 2.7.1 floors above):
#   Richmond Hill 36 -> 10  (855 Major Mackenzie Dr E; courtrooms A/B and video
#                            settlement conferences, i.e. mostly Superior Court)
#   Brampton   1,149 -> 500 ("--- All Below ---" = 7755 Hurontario, the main
#                            courthouse with both courts, plus 7765 Hurontario)
#   Milton       339 -> 150 (491 Steeles Ave E, both courts)
#   Burlington    16 -> 5   (2021 Plains Rd E, Halton provincial offences court;
#                            small enough that a quiet day may trip it)
#   Oshawa       456 -> 200 (150 Bond St E, both courts)
SEARCH_LOCATIONS = [
    {"label": "Toronto", "city": "Toronto", "office": "Toronto", "min_rows_per_day": 800},
    {"label": "Lindsay", "city": "Lindsay", "office": "K9V6G80", "min_rows_per_day": 15},
    {"label": "Newmarket", "city": "Newmarket", "office": "L3Y6B10", "min_rows_per_day": 200},
    {"label": "Richmond Hill", "city": "Richmond Hill", "office": "L4B4C60", "min_rows_per_day": 10},
    {"label": "Brampton", "city": "Brampton", "office": "Brampton", "min_rows_per_day": 400},
    {"label": "Milton", "city": "Milton", "office": "L9T1Y70", "min_rows_per_day": 60},
    {"label": "Burlington", "city": "Burlington", "office": "L7R4M30", "min_rows_per_day": 5},
    {"label": "Oshawa", "city": "Oshawa", "office": "L1G0A20", "min_rows_per_day": 150},
]

# Fallback only. The real date for a scrape is read from the results page's own
# address paragraph (see table_metadata), so the thresholds do not depend on
# assuming what "tomorrow" resolves to. These offsets are used solely when a
# page returns no rows at all and therefore prints no date to read.
#
# This matters because it is genuinely unclear what the site serves on a Friday:
# it may give Saturday (no court) or skip ahead to Monday. Reading the printed
# date means either answer is handled correctly without having to know.
DOCKET_DATE_OFFSETS = {"today": 0, "tomorrow": 1}

SUBMIT_BUTTON_NAME = "ctl00$MainContent$btnSubmit"
SUBMIT_BUTTON_VALUE = "SUBMIT"

# Maps a distinctive substring of each courthouse's address (as it appears in
# the address <p> preceding each results table) to a friendly display name.
# Confirmed via --discover-tables against the live results page.
COURTHOUSES = {
    "361 University": "361 University Ave - Superior Court of Justice",
    "10 Armoury": "10 Armoury St. - Ontario Court of Justice",
    "2201 Finch": "2201 Finch Ave W - Ontario Court of Justice",
    # Lindsay's address paragraph reads "Lindsay 440 Kent St W, Lindsay <date>".
    # Both the Superior Court and the Ontario Court of Justice sit at this one
    # address and the docket prints the identical paragraph above every table,
    # so unlike Toronto there is no way to tell the two courts apart from the
    # address alone - and no need to, since the Zoom lookup keys on room number.
    "440 Kent": "440 Kent St W - Lindsay",
    # Newmarket reads "Newmarket 50 Eagle St W, Newmarket <date>" above every
    # table, OCJ and Superior Court alike. The courts are told apart by room
    # name instead: OCJ rooms are numeric ("0101", "100V", "202T", "A1") and
    # Superior Court rooms are named ("CTRM 401", "CEVCcourtroom 26",
    # "TELECONFERENCE", "Virtual Small Claims 2").
    "50 Eagle St W": "50 Eagle St W - Newmarket",
    # GTA courthouses added in 2.7.0 (address text as printed on 2026-10-01).
    # Brampton, Milton and Oshawa each house both courts at one address, like
    # Newmarket; OCJ rooms print as bare numbers ("307", "9", "V102") and
    # Superior Court rooms as names ("CRT 208", "COURTROOM 7", "COURTROOM # 202").
    "855 Major Mackenzie": "855 Major Mackenzie Dr E - Richmond Hill",
    "7755 Hurontario": "7755 Hurontario St - Brampton",
    "7765 Hurontario": "7765 Hurontario St - Brampton",
    "491 Steeles": "491 Steeles Ave E - Milton",
    "2021 Plains": "2021 Plains Rd E - Burlington",
    "150 Bond St E": "150 Bond St E - Oshawa",
}

# Kept as an alias so anything referring to the old name still works.
TORONTO_COURTHOUSES = COURTHOUSES

# Courtroom -> Zoom link, per courthouse. Sourced once from sondhidefence.ca
# (these links are static / do not change day to day, per the source pages,
# so this is hardcoded rather than re-scraped on every run). #success
# suffixes and a couple of missing "?" before "pwd=" on the source site were
# already cleaned up here. Courtroom 908 (Armoury) had no real link on the
# source page (just a "#" placeholder) so it's omitted. 361 University Ave
# has no known source for Zoom links at all, hence the empty dict.
ARMOURY_ZOOM_LINKS = {
    "101": "https://ca01web.zoom.us/j/68643341906?pwd=RW9LL083d2pObUMxUXA3OHpuNUhSdz09",
    "201": "https://ca01web.zoom.us/j/64117725477?pwd=WTlya2k2UEFEL2czMTJiQ0RFQzBIdz09",
    "202": "https://ca01web.zoom.us/j/62877429247?pwd=NFFoK2xKUmhGa0NZU0l6L0I4ME5iUT09",
    "203": "https://ca01web.zoom.us/j/64055589354?pwd=TU5oM0VMZmYvQXVuNlcxZnRZYVpmUT09",
    "204": "https://ca01web.zoom.us/j/65205511991?pwd=NXlKbE9jOTFJZFFOamFGdStLeXdDQT09",
    "205": "https://ca01web.zoom.us/j/68735618593?pwd=ajZXZEg5L0VDdVA5WGRXL2FVdHhwZz09",
    "501": "https://ca01web.zoom.us/j/65844417761?pwd=L0EwZmZmd1RocVFYZnVuL2hYOCtzQT09",
    "601": "https://ca01web.zoom.us/j/62911867325?pwd=TXliL2hXVzA5bERBbE1KSFVQanFOUT09",
    "602": "https://ca01web.zoom.us/j/68508592180?pwd=S0NWVDNkY0dQWmZtdmFyeEt5b2hNUT09",
    "603": "https://ca01web.zoom.us/j/64481654217?pwd=QWVRNHg4UmV3TXl0cjV4M0lwblhMUT09",
    "604": "https://ca01web.zoom.us/j/67972618187?pwd=RVl5NFovbElrTkNzTkRJQUU1cVVHUT09",
    "605": "https://ca01web.zoom.us/j/63704068720",
    "606": "https://ca01web.zoom.us/j/64235113971?pwd=V3Mxc29wUzhHcm5oeHUwU29CRGluQT09",
    "607": "https://ca01web.zoom.us/j/67873289666?pwd=bDRoRm9hN1REbkpySWZWVmEvNEd2QT09",
    "701": "https://ca01web.zoom.us/j/62462214203?pwd=ZkJvWS84ejZyMUFTK2pyVEVjMHN5QT09",
    "702": "https://ca01web.zoom.us/j/63290088971?pwd=WWx6bHFUdXlEY3hBSFltOEJseVRHUT09",
    "703": "https://ca01web.zoom.us/j/61061628390?pwd=Z3VJUi9JU212OE5xdnVPMGhsWGZjdz09",
    "704": "https://ca01web.zoom.us/j/61302052402?pwd=NldTemhGOUE2T3NHZjJzMVlqV1JKdz09",
    "705": "https://ca01web.zoom.us/j/62197122746?pwd=OVJwcjk1TmMrUllja2tsR21zVGdFQT09",
    "706": "https://ca01web.zoom.us/j/64449579791?pwd=Yk1QR01Yd0laNzhvYmJVakhQaDN2Zz09",
    "707": "https://ca01web.zoom.us/j/64342053293?pwd=enJXTGkrVms0dmlZV0RHRGF0QXd2UT09",
    "801": "https://ca01web.zoom.us/j/63547596988?pwd=YjF6aTRqZVUzN0tHOU9kaWtZNHVSdz09",
    "802": "https://ca01web.zoom.us/j/62941179316",
    "803": "https://ca01web.zoom.us/j/62996423837?pwd=NStQRytaWlR1Wk84OGtVZk81amRJdz09",
    "804": "https://ca01web.zoom.us/j/67028913432?pwd=a2RVQTFtN3dJK01MenNzQWo3N1F5UT09",
    "805": "https://ca01web.zoom.us/j/66010649586?pwd=ckhGbFR1WUVSazVtUXdmdE8wSys5dz09",
    "806": "https://ca01web.zoom.us/j/62030655192?pwd=UXhXdEhRaWQyQVRwei93OTBhdnBEQT09",
    "807": "https://ca01web.zoom.us/j/61297927790?pwd=VEVqRHRXUGxmdEcyMnZoVHF0clRkZz09",
    "901": "https://ca01web.zoom.us/j/65430772491?pwd=UHArUmw0NDR1cGVxR0svV2dISlZVUT09",
    "902": "https://ca01web.zoom.us/j/62441806055?pwd=b2pXNmU2YVJKN015SEQ0Nmkybm5QUT09",
    "903": "https://ca01web.zoom.us/j/63974035577?pwd=d2dLdWNiT3hCWEc0amhzbXhpdXkxUT09",
    "904": "https://ca01web.zoom.us/j/64880745881?pwd=bGlLbnlyUTc2emxkeEVNZkNkd2RqQT09",
    "905": "https://ca01web.zoom.us/j/69016611585?pwd=d0dTZzBhK1c2K2F3NjY4d2VPVkJiQT09",
    "906": "https://ca01web.zoom.us/j/61887336786?pwd=UDJPcEIxTkppaFh4Mk1GUlZHTGk3QT09",
    "907": "https://ca01web.zoom.us/j/66869108875?pwd=U2FZeDBObExCbktWV1hOVy92MHNyZz09",
    "1001": "https://ca01web.zoom.us/j/66046385889?pwd=YjRiUnFTTzd4VmR4V3hSR1ZpeXNHdz09",
    "1002": "https://ca01web.zoom.us/j/62173472887",
    "1003": "https://ca01web.zoom.us/j/61796553031?pwd=a2pkKzdkSE1tQm1JZ290R3VTRVE4dz09",
    "1004": "https://ca01web.zoom.us/j/64133898872?pwd=ZWZDMm1ZWnVBdkVaclNJYzRmNHZZUT09",
    "1005": "https://ca01web.zoom.us/j/67991314033?pwd=NVZ6c054dXNUamllbVE0TWpIMXhrUT09",
    "1006": "https://ca01web.zoom.us/j/64261440848?pwd=c1lKaXloUWpZRWNZRStTcUJjS2h0UT09",
    "1007": "https://ca01web.zoom.us/j/66656721620?pwd=eWUxRWhrSEpMa3JvRi9xdWhKRnI5dz09",
    "1101": "https://ca01web.zoom.us/j/61532448546?pwd=SkRBNi9GZklVS29YVmp3MkFoeTRoUT09",
    "1102": "https://ca01web.zoom.us/j/61126243073?pwd=dDBTRXd6bEtXTlBqZUcvemF5NWxFZz09",
    "1103": "https://ca01web.zoom.us/j/67966360084?pwd=OFJIelNsUzlQK3dRaHZMcS9HUmZrZz09",
    "1104": "https://ca01web.zoom.us/j/65023401752?pwd=WS9ScGxxcm9mN1JVN281cFdqMEp6UT09",
    "1105": "https://ca01web.zoom.us/j/62376184679?pwd=cUlJTW04dStmMWx3Wm1jMVhzVGFPUT09",
    "1106": "https://ca01web.zoom.us/j/68739839004?pwd=MXJ2OHpoYWd1YmxMV0Iza2JBNHNZdz09",
    "1107": "https://ca01web.zoom.us/j/68776032401?pwd=WVJuRzVoMjE2YVZsZkZQb0dJRWRJdz09",
    "1108": "https://ca01web.zoom.us/j/69192235984?pwd=VGZOemk4YXNoQ2ZoMEJ6Kzc0akMrQT09",
    "1201": "https://ca01web.zoom.us/j/62833970447?pwd=WjNzTzF1eVdWZWdrY0s0NzkreEQyZz09",
    "1202": "https://ca01web.zoom.us/j/68461583959?pwd=Y21zZFJWNTIvSVd0Q05BaEN0WThEdz09",
    "1203": "https://ca01web.zoom.us/j/66229084971?pwd=SEF1Z1ZTbXQrbkRabE1wa3lNZTlVdz09",
    "1204": "https://ca01web.zoom.us/j/65333108465?pwd=cVhOVHpYM1lCeUEzNyttRWcvYS95Zz09",
    "1205": "https://ca01web.zoom.us/j/63396962650?pwd=d2VuRWNwZnRHWmtFZ0FjenAxQWJBQT09",
    "1206": "https://ca01web.zoom.us/j/66367589285?pwd=SEIvZmhtSkxCWkl1MlZTcitkYUFpQT09",
    "1207": "https://ca01web.zoom.us/j/68744831556?pwd=bGZJVDJtNm90VEpCanNiTjM3aklydz09",
    "1301": "https://ca01web.zoom.us/j/69298956828?pwd=YVI0MWh1OUxINVZXdklZQ0gvSFNkZz09",
    "1302": "https://ca01web.zoom.us/j/69752118043?pwd=cnRUZWtRTTN5eUZkZUNTUFRuRnJqUT09",
    "1303": "https://ca01web.zoom.us/j/68748265459?pwd=Wm1keVVabUtCOEdJYmhDenBKUHpKUT09",
    "1304": "https://ca01web.zoom.us/j/66860715966?pwd=OVdTY0ZyYnBJQnUzWUR3dzg3V04yZz09",
    "1305": "https://ca01web.zoom.us/j/65756640120?pwd=QTlxMWo2Y0VmZzI4NFFhSWdLMi9mQT09",
    "1306": "https://ca01web.zoom.us/j/67404840641?pwd=Z1RndnRybmhmVXpDaUhvQzBGU3JhQT09",
    "1307": "https://ca01web.zoom.us/j/67287307445?pwd=NzZKYTdxZnU4aDFzU1Z0MVYxUllIQT09",
    "1401": "https://ca01web.zoom.us/j/66949911402?pwd=aWVRTGhZTVJnSE5XQ3oremdrOTY4QT09",
    "1402": "https://ca01web.zoom.us/j/62114187523?pwd=Z1RONG5xWloyOEM1MGRhV1RabnJ2dz09",
    "1403": "https://ca01web.zoom.us/j/65787827629?pwd=UnlienVsdmc1TVFISVl1TjNMRHU5QT09",
    "1404": "https://ca01web.zoom.us/j/65499319358?pwd=Z28vK3RJeHZiM1BzU0k5T00yd0pvZz09",
    "1405": "https://ca01web.zoom.us/j/64797288796?pwd=amtkQkdSWStVM045MGl1T25TV1orZz09",
    "1406": "https://ca01web.zoom.us/j/63136531733?pwd=SElJREFsQ00yeTQ3ZnVRRnJjV0pFUT09",
}

FINCH_ZOOM_LINKS = {
    "102": "https://ca01web.zoom.us/j/64783683248?pwd=TnFQNVJnSGZiRWtpem9vV2FQcHdOQT09",
    "103": "https://ca01web.zoom.us/j/66310019164?pwd=VHFENG84eklnY2p6Y1R2SXN0dXRqQT09",
    "104": "https://ca01web.zoom.us/j/61474891730?pwd=V3RueGtTcGpjYzFhbTJzWkFkVXVRdz09",
    "105": "https://ca01web.zoom.us/j/68915801752?pwd=cTh0Tk5wbUlIeDZFSXlJeDF0MnE4UT09",
    "106": "https://ca01web.zoom.us/j/69561969969?pwd=VjVkaU9XMWUyaFlZYW5HaXJ2YXNwUT09",
    "107": "https://ca01web.zoom.us/j/65983841811?pwd=ODhLcDhyV01nMUllQXZxcE0wL05Xdz09",
    "301": "https://ca01web.zoom.us/j/65329317873?pwd=enpUMGdqM2JhR2J6Z0dwN0doaEd3dz09",
    "302": "https://ca01web.zoom.us/j/65981875767?pwd=L2s2WFR4ZUN6dktnRzBwemwvaTIwQT09",
    "303": "https://ca01web.zoom.us/j/68126648111?pwd=V05pYjh5ZTA0dFF3Zkp3b3kweEd6Zz09",
    "401": "https://ca01web.zoom.us/j/62313701840?pwd=czEwYjJYVHU3UDF3Nm05ZVdGTkM5QT09",
    "402": "https://ca01web.zoom.us/j/69044572345?pwd=WGc5RHFRSEkwcFd6cE1FemhROGxzQT09",
    "403": "https://ca01web.zoom.us/j/64045828121?pwd=eG9uR29zUVBMbHBnUCtMQXVmVkdaUT09",
}

# Lindsay (440 Kent St W). Sourced from jsmlaw.ca's "Peterborough, Lindsay and
# Cobourg Zoom Coordinates" page, #success suffixes stripped.
#
# IMPORTANT - only the entries that page labels with an explicit courthouse name
# are used here. It carries a second, unlabelled table ("Adult Criminal Case
# Management Court (Courtroom 3)", "Plea Court", a JICMC court) whose courthouse
# is never stated; by elimination it is most likely Peterborough, and its
# "Courtroom 3" link differs from the one the page explicitly labels "Lindsay
# Courtroom #3". Guessing there could send someone to the wrong hearing, so
# those rows are deliberately left out. Room 1/3/4/6 below covers every room
# number observed on a real Lindsay docket.
LINDSAY_ZOOM_LINKS = {
    # "Courtroom #1- Lindsay Adult Court"
    "1": "https://ca01web.zoom.us/j/67631840845?pwd=dGVWaHYycGJBUFpZcG4xek1ZMmt3dz09",
    # "Lindsay Courtroom #3"
    "3": "https://ca01web.zoom.us/j/61110219869?pwd=cjFaUlNvZzJVWExSeDV0bjhTT1puUT09",
    # "Cobourg & Lindsay Amalgamated Bail Court #4" - shared bail court, and
    # room 4 on the Lindsay docket is indeed where BAIL HEARING/SHOW CAUSE sits.
    "4": "https://ca01web.zoom.us/j/69525293502?pwd=YTBVUEZtZ3JEWTNVcUQ5WmZMMU1IUT09",
    # "Lindsay Courtroom #6"
    "6": "https://ca01web.zoom.us/j/68879625169?pwd=L25iaG1SVGc4RkJMeG9IS3g0WkJTUT09",
}

# Newmarket (50 Eagle St W), Ontario Court of Justice only. Cross-checked
# 2026-09-29 across sondhidefence.ca (OCJ and case-management pages),
# adhillonlaw.com (2026-01-22 post), teleshlawfirm.ca, morfisher.ca/zoom-links
# (province-wide table; its "Courtroom / Docket" column is the room used here,
# and the number starting its "Purpose" column is a different code, not the
# docket room) and the official ontariocourts.ca/ocj/locations/newmarket page
# (which lists only 101, 106, 202, 302 and JCMC). Every entry below is on at
# least two of those, and all agree wherever they overlap. Keys are the docket's room with leading zeros
# stripped (docket "0101" -> "101"); "100V" is the Video Remand court and is
# NOT the same room as docket "0100".
#
# Deliberately left out:
#   201  - sondhidefence's "Courtroom 201" row repeats the 205 link, while its
#          own case-management page and teleshlawfirm say the 201 docket sits
#          in the 101 (VCMC) meeting; morfisher has no courtroom 201 row.
#          Conflicting, so no link.
#   0100, 202T, A1 - on the docket but on no source page.
#   JCMC - a real meeting (Judicial Case Management Court) but it never
#          appears as a room on the docket, so there is nothing to key it on.
NEWMARKET_ZOOM_LINKS = {
    # Video Remand Court
    "100V": "https://ca01web.zoom.us/j/69334108840?pwd=dW04YkdZN204bSt0aVNPeW4yeWMwUT09",
    # Case Management Court / VCMC docket (official page)
    "101": "https://ca01web.zoom.us/j/62041091566?pwd=N1VoVjRDUkVaeTBQeGQrVHkyL3VaUT09",
    "102": "https://ca01web.zoom.us/j/68719051450?pwd=Q200bWZ6a0FDUWJxeEFXa051L0dKUT09",
    # Contested Bail Court
    "103": "https://ca01web.zoom.us/j/68578772415?pwd=cXZreDhaSXZWY2hqQUhCVkhrMEc3dz09",
    # New Arrest and Non-Contested Bail
    "104": "https://ca01web.zoom.us/j/68811814043?pwd=eDNMUHdEcDRIMFRXWWNtaFpDK0VHZz09",
    # Domestic (Tue/Wed), Youth (Thu) and POA (Fri) Case Management (official page)
    "106": "https://ca01web.zoom.us/j/68731823651?pwd=THBPcUJjQzZiMDBqNTB4ejhjbGNJZz09",
    "200": "https://ca01web.zoom.us/j/68633207836?pwd=Z08wenFRREl1MndvNTRKNzFmdWwrdz09",
    # Out of Custody Plea Court (official page)
    "202": "https://ca01web.zoom.us/j/66299098778?pwd=Z1hkcE0zZGQzWHdSL1R3SjlNMDlaUT09",
    "203": "https://ca01web.zoom.us/j/67213083380?pwd=S0N3ZjhEMHZVYU9BZVJvUmM0SHFGUT09",
    "204": "https://ca01web.zoom.us/j/69747329351?pwd=RTJ5OVo5c0hPWXNOYjMrejJsVmR6Zz09",
    "205": "https://ca01web.zoom.us/j/65577455635?pwd=L05jUTJSYmVKOWRLY3o2bXFMYnBldz09",
    # Trial Readiness (official page)
    "302": "https://ca01web.zoom.us/j/61502150320?pwd=WXpoWGcvTlliVnZyejRpNzBOQWhZQT09",
    "303": "https://ca01web.zoom.us/j/64787847080?pwd=TUhEaGk0Z2x0RklMS1NYak9HZHdlQT09",
    # Self-represented JPTs (Mon/Fri)
    "1000": "https://ca01web.zoom.us/j/66056357113?pwd=U25EaGV5YmgvSURwZ3VLckNwK3o1UT09",
}

# Brampton (7755 Hurontario St), Milton (491 Steeles Ave E) and Oshawa (150 Bond
# St E), Ontario Court of Justice only. Gathered 2026-09-30 from
# morfisher.ca/zoom-links, teleshlawfirm.ca/resources/court-links, the official
# ontariocourts.ca/ocj/locations pages (brampton, milton, oshawa-durham) and
# sondhidefence.ca/oshawa-zoom-coordinates. As with Newmarket, every entry is on
# at least two sources with the same meeting ID AND passcode.
#
# Brampton needs care: morfisher lists both a "docket" code and a physical
# "courtroom", and Telesh lists only the physical courtroom, so where the two
# differ they disagree (e.g. Telesh's "204" is the official page's "108"). The
# official page matches the docket code, which is also what ontariocourtdates
# prints as the room, so keys here are docket codes. Deliberately left out:
#   101, 304 - the sources give different meetings for them.
#   206      - only listed as docket 206 sitting in courtroom 102; unclear.
#   312, 411, 412, 413 - on morfisher only.
#   7765 Hurontario (rooms H-11/H-12/H-13) - no source.
# Milton: 9 and 15 are on the official page and morfisher; nothing else agrees.
# Oshawa: V102 and V108 share one meeting (official page). 103 is left out
# (the two sources give different passcodes), as are 102, 106 and 404 (one
# source each or unclear) and B1/B2 (never a room on the docket).
# Richmond Hill and Burlington: no Zoom links found on two sources.
BRAMPTON_ZOOM_LINKS = {
    "103": "https://ca01web.zoom.us/j/64681518446?pwd=VjVsa3Fza2hMTzBkZTVqSTNCaE8xdz09",
    "104": "https://ca01web.zoom.us/j/67805418119?pwd=Yk1nK0djb01yblRMNGJaN09SRTlVZz09",
    "105": "https://ca01web.zoom.us/j/68482502470?pwd=THREd0VyQ0VIcVJ0MTNGeXFpNHBMQT09",
    "106": "https://ca01web.zoom.us/j/61704910693?pwd=TWtKVmxVRGFiaEtZYU04OHg3TGJYdz09",
    "107": "https://ca01web.zoom.us/j/67136121600?pwd=Q3FtUFFmSTgwa3d2ZjYzdnNZWXhLUT09",
    "108": "https://ca01web.zoom.us/j/67326141025?pwd=MGFtRk5OYmV5VzFhL3FWQUNBcjRXQT09",
    "109": "https://ca01web.zoom.us/j/61665674969?pwd=eXlibWVtTCtqdm9OOGIyVmhER3ZzQT09",
    "110": "https://ca01web.zoom.us/j/61536320302?pwd=L3luNnQ3aDF2OVY5ajhhRGlLc21iUT09",
    "111": "https://ca01web.zoom.us/j/62823723884?pwd=R3c4OVNNWVBONVFyQ0lIQzR5dHRlQT09",
    "112": "https://ca01web.zoom.us/j/63546167019?pwd=NzUzeE5Fd1FVRXMwUmZkYkhweHFNQT09",
    "201": "https://ca01web.zoom.us/j/64337372268?pwd=K0o1amF2VkdVaURoVFcraU1pRVVOQT09",
    "202": "https://ca01web.zoom.us/j/63589671435?pwd=eFA4NUFoM1JJeUttM0doQTFzSmljQT09",
    "203": "https://ca01web.zoom.us/j/65893143525?pwd=eHRXSmdaZGU3dnV6Y2VhNjNOODdJUT09",
    "204": "https://ca01web.zoom.us/j/65938982880?pwd=RlhpKzVwN2pPN3o5bmhZSXlkSnc4QT09",
    "207": "https://ca01web.zoom.us/j/62730869709?pwd=aEYyanE4UTNSVzQvVlAzUlJGYnM2UT09",
    "208": "https://ca01web.zoom.us/j/68525605891?pwd=aWpHNmhFS0hZVVZuUnk4VVZJdWl5QT09",
    "209": "https://ca01web.zoom.us/j/69500625514?pwd=SU5FYytzbEV5UFpjdFRyblVER1NKZz09",
    "210": "https://ca01web.zoom.us/j/68100568535?pwd=Q2hsbmxHQm1tVUVzMFpJTDU4enNRZz09",
    "302": "https://ca01web.zoom.us/j/61155228565?pwd=amNNaTB5aTV0R3RySGE0a2xiNHBTUT09",
    "303": "https://ca01web.zoom.us/j/64971796716?pwd=N1k5MVNYYjZ5WEYrY3NUS0p0V2lvQT09",
    "306": "https://ca01web.zoom.us/j/68840172766?pwd=M0w2WmpPbzlxTmRFdDJBd3VDZ1FiQT09",
    "307": "https://ca01web.zoom.us/j/64410129387?pwd=bFZiazcvcnk0cVZTemQwQTlCYmxmZz09",
    "309": "https://ca01web.zoom.us/j/67808023152?pwd=MzFqZTVsYmh0TmpiQUt6bytRMVBXUT09",
    "403": "https://ca01web.zoom.us/j/67615536400?pwd=TUtTUU5vWDNVNTBGMk1HQ3RWcklvdz09",
    "405": "https://ca01web.zoom.us/j/63986734187?pwd=eU1DbHhFOE1GeHdtZXRPdk1nbWNCdz09",
    "409": "https://ca01web.zoom.us/j/67565833516?pwd=TC9TdCtyN082aEJSUXBzK0E5UzhsQT09",
    "H9": "https://ca01web.zoom.us/j/68611773374?pwd=c0pYdWhLNFVwUkR1TWhVNzhBM1Z3dz09",
    "H15": "https://ca01web.zoom.us/j/66167769592?pwd=MUdXdm1Ga3FKL0YyUTRwOGRYUEdEQT09",
}

MILTON_ZOOM_LINKS = {
    # Courtroom 9: adult criminal case management, DV, federal and youth (official page)
    "9": "https://ca01web.zoom.us/j/66755908772?pwd=U2ZCblFuV2RKY0lSWFVqTkdKMzIyUT09",
    # Courtroom 15 / M15: plea court and JICMC (official page)
    "15": "https://ca01web.zoom.us/j/62714951146?pwd=Q3hJQUg5K09hblRsMm1GSTIrMExLUT09",
}

OSHAWA_ZOOM_LINKS = {
    # Represented matters / Youth (official page)
    "101": "https://ca01web.zoom.us/j/63345031777?pwd=dGVEbXUwMjZaWS9EWVJjczBCcE9EUT09",
    "104": "https://ca01web.zoom.us/j/63591857307?pwd=T3hFSllieXBFZmIvUW9OUGRRYkdIZz09",
    "105": "https://ca01web.zoom.us/j/61501891107?pwd=VW9keTEzcWpzRlFxWU1KRWlyZVMzdz09",
    # Plea Court
    "107": "https://ca01web.zoom.us/j/67557792182?pwd=SWEvUWU5YmgwN3ppOXRYc2xRQnpidz09",
    "108": "https://ca01web.zoom.us/j/69698314908?pwd=VzJYQ3YyL0tWYktKRG8zTVRwdlBZZz09",
    # Adult criminal case management, Mon-Fri (official page: "V108 / V102")
    "V102": "https://ca01web.zoom.us/j/69626566709?pwd=akxSd2hFYW0rKzZWQVpCQ1BMV0pCUT09",
    "V108": "https://ca01web.zoom.us/j/69626566709?pwd=akxSd2hFYW0rKzZWQVpCQ1BMV0pCUT09",
    "402": "https://ca01web.zoom.us/j/69163561663?pwd=YkFCWDdUdmEzVzczMS8zWThYQWZaQT09",
    "403": "https://ca01web.zoom.us/j/64593713166?pwd=R2kzc0hMWm42S2IwWHM1OUoxWEtpUT09",
    "405": "https://ca01web.zoom.us/j/62184723197?pwd=M2hjUjQvaW9mUnlzTFQwenlIYU9wZz09",
    "406": "https://ca01web.zoom.us/j/67098725169?pwd=S2JOSnhjVXRDdVBBK2g0MzF2Q1kxZz09",
    "407": "https://ca01web.zoom.us/j/65888204869?pwd=QVNWa1Z6YTVCSjNJVE5DZkpidTRKUT09",
    "408": "https://ca01web.zoom.us/j/63632594710?pwd=NUF4UHU2bVU2aWc4R3dDZ1U2YzVKQT09",
    "409": "https://ca01web.zoom.us/j/63299495683?pwd=bzdCRGJZMjFpSWoxSEh3Tk0reDN5UT09",
}

# Which courtroom dict belongs to which courthouse label (must match the
# values in COURTHOUSES above exactly).
ZOOM_LINKS_BY_COURTHOUSE = {
    "10 Armoury St. - Ontario Court of Justice": ARMOURY_ZOOM_LINKS,
    "2201 Finch Ave W - Ontario Court of Justice": FINCH_ZOOM_LINKS,
    "361 University Ave - Superior Court of Justice": {},  # no known source for these yet
    "440 Kent St W - Lindsay": LINDSAY_ZOOM_LINKS,
    "50 Eagle St W - Newmarket": NEWMARKET_ZOOM_LINKS,
    "7755 Hurontario St - Brampton": BRAMPTON_ZOOM_LINKS,
    "491 Steeles Ave E - Milton": MILTON_ZOOM_LINKS,
    "150 Bond St E - Oshawa": OSHAWA_ZOOM_LINKS,
}

# Courthouses whose rooms must match exactly (after the normalising below),
# with no digits-only fallback. Newmarket shares one address between the OCJ
# and the Superior Court, so "CTRM 202" (a Superior Court room) must not turn
# into "202" and pick up the OCJ plea court's link.
EXACT_ROOM_MATCH_ONLY = {"50 Eagle St W - Newmarket", "7755 Hurontario St - Brampton",
                         "491 Steeles Ave E - Milton", "150 Bond St E - Oshawa"}


def get_zoom_link(courthouse_label: str, room: str):
    """Look up the Zoom link for a given courthouse + room number, or None."""
    if not room:
        return None
    links = ZOOM_LINKS_BY_COURTHOUSE.get(courthouse_label, {})
    # The room as printed, minus spaces and leading zeros ("0101" -> "101",
    # "100V" stays "100V").
    exact = re.sub(r"\s", "", room).upper().lstrip("0")
    if exact in links or courthouse_label in EXACT_ROOM_MATCH_ONLY:
        return links.get(exact)
    room_key = re.sub(r"\D", "", room).lstrip("0")  # strip anything non-numeric, just in case
    return links.get(room_key)

# --- Email settings (Gmail example; any SMTP provider works) ---
# These are just fallback defaults - the real values live in
# courtwatch_config.json (see load_external_config below), so this script
# can be replaced/updated without ever touching your actual credentials.
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587
SMTP_USER = "your_email@gmail.com"
SMTP_PASSWORD = "your_16_char_app_password"
# Display name + address shown in the "From" field. Using a Gmail "+alias"
# here (registered under Gmail's Settings -> Accounts and Import -> Send
# mail as) lets you search/filter/label on the alias cleanly, unlike relying
# on the buried X-Google-Original-From header that a plain "+alias" without
# that Gmail setup would otherwise produce.
SMTP_FROM = "CourtWatch <your_email+alias@gmail.com>"
EMAIL_TO = ["first_person@gmail.com", "second_person@example.com"]

# healthchecks.io ping URL, e.g. "https://hc-ping.com/<uuid>" - set the real one
# in courtwatch_config.json. Leave the bare URL there: this script appends
# "/start" when a run begins and "/fail" when it fails, on its own.
# Empty = pinging disabled (the run still happens and is still logged).
HEALTHCHECK_URL = ""

# A weekday docket is never genuinely empty. Parsing 0 rows means the site flow
# or page structure changed and we are no longer reading results at all - which
# would otherwise masquerade as a reassuring "no matches found" result.
MIN_EXPECTED_ROWS = 1

# Local file remembering what's already been alerted on, so scheduled reruns
# don't email you again for the same listing.
STATE_FILE = Path(__file__).parent / "courtwatch_state.json"

# Log file - every run (including ones triggered silently by Task Scheduler)
# appends a timestamped record here: what arguments it was invoked with,
# what happened, and the full traceback of any error. This is what to check
# first whenever an expected email doesn't arrive.
LOG_FILE = Path(__file__).parent / "courtwatch_log.txt"

# External config file holding the REAL credentials/search term - kept
# separate from this script so replacing courtwatch.py never touches it.
# Created automatically (as a template) on first run if it doesn't exist.
CONFIG_FILE = Path(__file__).parent / "courtwatch_config.json"

# =========================================================================

logger = logging.getLogger("courtwatch")
logger.setLevel(logging.INFO)
_file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
_file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logger.addHandler(_file_handler)
_console_handler = logging.StreamHandler(sys.stdout)
_console_handler.setFormatter(logging.Formatter("%(message)s"))
logger.addHandler(_console_handler)


def get_hidden_fields(soup: BeautifulSoup) -> dict:
    """Pull every hidden input's name/value out of a page (VIEWSTATE etc.)."""
    fields = {}
    for inp in soup.find_all("input", {"type": "hidden"}):
        name = inp.get("name")
        if name:
            fields[name] = inp.get("value", "")
    return fields


def print_fields(soup: BeautifulSoup, label: str):
    """Print every form field found in a page: names, values, dropdown options."""
    print(f"\n=== Fields on {label} ===\n")
    for inp in soup.find_all("input"):
        print(f"<input> name={inp.get('name')!r} type={inp.get('type')!r} "
              f"value={inp.get('value')!r}")
    for sel in soup.find_all("select"):
        print(f"\n<select> name={sel.get('name')!r} id={sel.get('id')!r}")
        for opt in sel.find_all("option"):
            print(f"    option value={opt.get('value')!r}  text={opt.text.strip()!r}")
    for btn in soup.find_all("button"):
        print(f"<button> name={btn.get('name')!r} value={btn.get('value')!r} "
              f"text={btn.text.strip()!r}")
    print(f"\nDone with {label}.\n")


def discover(url: str):
    """GET a page as-is and print its form fields (use for the landing page)."""
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    print_fields(soup, url)


def discover_search(url: str):
    """Agree, then print the fields of the resulting search page (diagnostic)."""
    with requests.Session() as session:
        session.headers.update({"User-Agent": "Mozilla/5.0", "Referer": url})
        resp = session.get(url, timeout=30)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        data = get_hidden_fields(soup)
        data[AGREE_CHECKBOX_NAME] = "on"
        data[ENTER_BUTTON_NAME] = ENTER_BUTTON_VALUE
        resp2 = session.post(url, data=data, timeout=30)
        soup2 = BeautifulSoup(resp2.text, "html.parser")
        print(f"Status: {resp2.status_code}, Final URL: {resp2.url}")
        print_fields(soup2, f"{url} (after agreeing)")


def discover_city(url: str, city: str = "Toronto"):
    """Agree, then select a city and print the resulting fields (diagnostic)."""
    with requests.Session() as session:
        session.headers.update({"User-Agent": "Mozilla/5.0", "Referer": url})
        resp = session.get(url, timeout=30)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        data = get_hidden_fields(soup)
        data[AGREE_CHECKBOX_NAME] = "on"
        data[ENTER_BUTTON_NAME] = ENTER_BUTTON_VALUE
        resp2 = session.post(url, data=data, timeout=30)
        search_url = resp2.url
        soup2 = BeautifulSoup(resp2.text, "html.parser")

        data2 = get_hidden_fields(soup2)
        data2[CITY_FIELD_NAME] = city
        data2["__EVENTTARGET"] = CITY_FIELD_NAME
        data2["__EVENTARGUMENT"] = ""
        resp3 = session.post(search_url, data=data2, timeout=30)
        soup3 = BeautifulSoup(resp3.text, "html.parser")
        print(f"Status: {resp3.status_code}, Final URL: {resp3.url}")
        print_fields(soup3, f"{search_url} (after selecting city={city!r})")


# --------------------------- confirmed watch flow ---------------------------

def step_agree(session: requests.Session, landing_url: str):
    """GET landing page, POST agree checkbox+ENTER. Returns (soup, search_url)."""
    resp = session.get(landing_url, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    data = get_hidden_fields(soup)
    data[AGREE_CHECKBOX_NAME] = "on"
    data[ENTER_BUTTON_NAME] = ENTER_BUTTON_VALUE
    resp2 = session.post(landing_url, data=data, timeout=30)
    resp2.raise_for_status()
    return BeautifulSoup(resp2.text, "html.parser"), resp2.url


def step_select_city(session: requests.Session, search_url: str, soup: BeautifulSoup,
                      city: str = None):
    """POST the city-change postback so ddlLob/listBoxCourtOffice populate."""
    data = get_hidden_fields(soup)
    data[CITY_FIELD_NAME] = city or CITY_VALUE
    data["__EVENTTARGET"] = CITY_FIELD_NAME
    data["__EVENTARGUMENT"] = ""
    resp = session.post(search_url, data=data, timeout=30)
    resp.raise_for_status()
    return BeautifulSoup(resp.text, "html.parser")


def step_final_search(session: requests.Session, search_url: str, soup: BeautifulSoup,
                       city: str = None, office: str = None):
    """POST the final search (court type, city, lob, courthouse, submit)."""
    data = get_hidden_fields(soup)
    data[COURT_FIELD_NAME] = COURT_VALUE
    data[CITY_FIELD_NAME] = city or CITY_VALUE
    data[LOB_FIELD_NAME] = LOB_VALUE
    data[COURT_OFFICE_FIELD_NAME] = office or COURT_OFFICE_VALUE
    data[SUBMIT_BUTTON_NAME] = SUBMIT_BUTTON_VALUE
    # The results page (dockets_view.aspx) appears to check Referer; requests
    # does not update this automatically across redirects like a browser
    # does, so we set it explicitly to the search form URL right before this
    # POST (confirmed via --discover-final that this is what fixes the bounce
    # back to an empty search form).
    session.headers["Referer"] = search_url
    resp = session.post(search_url, data=data, timeout=30)
    resp.raise_for_status()
    return BeautifulSoup(resp.text, "html.parser")


def discover_final(url: str, dump_path: str = "final_page_dump.html"):
    """
    Run the FULL confirmed flow (agree -> select city -> final search) and
    dump diagnostics: status code, final URL, how many <table> elements were
    found, and the raw HTML saved to disk so we can inspect what actually
    came back when the row-extraction found nothing.
    """
    with requests.Session() as session:
        session.headers.update({"User-Agent": "Mozilla/5.0", "Referer": url})
        soup, search_url = step_agree(session, url)
        soup = step_select_city(session, search_url, soup)

        data = get_hidden_fields(soup)
        data[COURT_FIELD_NAME] = COURT_VALUE
        data[CITY_FIELD_NAME] = CITY_VALUE
        data[LOB_FIELD_NAME] = LOB_VALUE
        data[COURT_OFFICE_FIELD_NAME] = COURT_OFFICE_VALUE
        data[SUBMIT_BUTTON_NAME] = SUBMIT_BUTTON_VALUE

        print(f"\nFinal POST fields being sent: "
              f"{ {k: v for k, v in data.items() if not k.startswith('__')} }")

        # Follow redirects manually (not automatically) so we can update the
        # Referer header at each hop the way a real browser would, and see
        # exactly which hop (if any) bounces us back to the search form.
        current_url = search_url
        current_method = "POST"
        current_data = data
        hop = 0
        resp = None

        while hop < 6:
            hop += 1
            if current_method == "POST":
                resp = session.post(current_url, data=current_data, timeout=30,
                                     allow_redirects=False)
            else:
                resp = session.get(current_url, timeout=30, allow_redirects=False)

            print(f"Hop {hop}: {current_method} {current_url} -> {resp.status_code}")

            if resp.status_code in (301, 302, 303, 307, 308):
                next_url = resp.headers.get("Location")
                print(f"    Location: {next_url}")
                session.headers["Referer"] = current_url
                if not next_url.startswith("http"):
                    from urllib.parse import urljoin
                    next_url = urljoin(current_url, next_url)
                current_url = next_url
                current_method = "GET"
                current_data = None
            else:
                break

        soup_final = BeautifulSoup(resp.text, "html.parser")
        tables = soup_final.find_all("table")

        print(f"\nLanded on: {current_url}")
        print(f"Last hop status: {resp.status_code}")
        print(f"Number of <table> elements found: {len(tables)}")
        for i, t in enumerate(tables):
            rows_in_table = len(t.find_all("tr"))
            print(f"  table[{i}] id={t.get('id')!r} class={t.get('class')!r} rows={rows_in_table}")

        Path(dump_path).write_text(resp.text, encoding="utf-8")
        print(f"\nFull HTML saved to {dump_path} - open it in a browser or text editor to inspect.")

        title = soup_final.find("title")
        print(f"Page title: {title.text.strip() if title else '(none)'}")

        lbl = soup_final.find(id="ctl00_MainContent_lblResult")
        print(f"lblResult text: {lbl.get_text(strip=True) if lbl else '(element not found)'}")

        # Check what actually ended up "selected" in each dropdown on the
        # RETURNED page - this tells us whether our selections stuck server-side,
        # independent of what plain text extraction shows (which lists ALL
        # option labels regardless of selection).
        print("\n--- Selected options on returned page ---")
        for name in (CITY_FIELD_NAME, COURT_FIELD_NAME, LOB_FIELD_NAME, COURT_OFFICE_FIELD_NAME):
            sel = soup_final.find("select", {"name": name})
            if sel is None:
                print(f"{name}: <select> not found on returned page")
                continue
            selected = sel.find_all("option", selected=True)
            print(f"{name}: selected={[ (o.get('value'), o.text.strip()) for o in selected ]}")

        # Does the search term appear ANYWHERE in the raw HTML, even outside
        # a <table>? Results might render as divs/spans instead of a table.
        print(f"\n--- Raw search for 'SWANEK' anywhere in response ---")
        print("FOUND" if "SWANEK" in resp.text.upper() or "swanek" in resp.text.lower()
              else "NOT FOUND")

        # List any element with an id suggestive of a results grid/list, in
        # case results aren't in a <table> at all.
        print("\n--- Elements with id containing 'grid', 'result', or 'docket' ---")
        for el in soup_final.find_all(id=True):
            el_id = el.get("id", "")
            if any(kw in el_id.lower() for kw in ("grid", "result", "docket", "list")):
                print(f"  <{el.name}> id={el_id!r}")

        body_text = soup_final.get_text(separator=" ", strip=True)
        print(f"\nVisible text (first 1000 chars):\n{body_text[:1000]}")
    """Pull each results-table row as a flat text string for searching/logging."""
    rows = []
    for table in soup.find_all("table"):
        for tr in table.find_all("tr"):
            text = " | ".join(cell.get_text(strip=True) for cell in tr.find_all(["td", "th"]))
            if text.strip():
                rows.append(text)
    return rows


def discover_tables(url: str):
    """
    Run the full flow and, for each results <table>, walk backward through
    its preceding siblings/ancestors to find whatever heading or text block
    identifies which courthouse that table belongs to. Prints enough context
    to figure out the right selector to extract courthouse names in extract_rows.
    """
    with requests.Session() as session:
        session.headers.update({"User-Agent": "Mozilla/5.0", "Referer": url})
        soup, search_url = step_agree(session, url)
        soup = step_select_city(session, search_url, soup)
        soup = step_final_search(session, search_url, soup)

        tables = soup.find_all("table")
        print(f"Found {len(tables)} tables.\n")

        for i, table in enumerate(tables):
            print(f"=== Table {i} (rows={len(table.find_all('tr'))}) ===")

            # Walk backwards through preceding siblings at the table's own level
            prev_texts = []
            node = table.find_previous_sibling()
            hops = 0
            while node and hops < 5:
                text = node.get_text(strip=True)
                if text:
                    prev_texts.append(f"<{node.name}> {text[:150]}")
                node = node.find_previous_sibling()
                hops += 1
            print("Preceding siblings (nearest first):")
            for t in prev_texts:
                print(f"  {t}")

            # Also check the immediate parent's preceding siblings, in case
            # the table is wrapped in its own container div.
            parent = table.parent
            if parent is not None:
                parent_prev = parent.find_previous_sibling()
                if parent_prev is not None:
                    ptext = parent_prev.get_text(strip=True)
                    print(f"Parent's preceding sibling: <{parent_prev.name}> {ptext[:150]}")

            # First row of the table itself, in case the courthouse name is
            # actually a header row INSIDE the table rather than before it.
            first_row = table.find("tr")
            if first_row:
                print(f"Table's own first row: {first_row.get_text(' | ', strip=True)[:200]}")

            print()
    """Pull each results-table row as a flat text string for searching/logging."""
    rows = []
    for table in soup.find_all("table"):
        for tr in table.find_all("tr"):
            text = " | ".join(cell.get_text(strip=True) for cell in tr.find_all(["td", "th"]))
            if text.strip():
                rows.append(text)
    return rows


_MONTHS = ("January|February|March|April|May|June|July|August|"
           "September|October|November|December")
_DATE_RE = re.compile(rf"({_MONTHS})\s+(\d{{1,2}}),\s*(\d{{4}})")


def table_metadata(table):
    """
    Find the address <p> that precedes this table (walking back through
    siblings, skipping the intervening <h2>/other elements). Returns
    (courthouse_label, hearing_date) where hearing_date is a datetime.date
    parsed straight from that same paragraph's text (e.g. "...Friday July
    17, 2026" -> date(2026, 7, 17)), or None if it can't be found/parsed.
    Using the page's own printed date rather than assuming "today"/
    "tomorrow" from the system clock avoids any timezone/rollover surprises.
    """
    courthouse_label = "Unknown courthouse"
    hearing_date = None

    node = table.find_previous_sibling("p")
    if node is not None:
        text = node.get_text(" ", strip=True)

        for key, label in COURTHOUSES.items():
            if key in text:
                courthouse_label = label
                break
        else:
            courthouse_label = f"Unknown courthouse (address text: {text[:80]})"

        m = _DATE_RE.search(text)
        if m:
            month_name, day_str, year_str = m.groups()
            try:
                hearing_date = datetime.strptime(
                    f"{month_name} {day_str}, {year_str}", "%B %d, %Y"
                ).date()
            except ValueError:
                hearing_date = None

    return courthouse_label, hearing_date


def extract_rows(soup: BeautifulSoup) -> list:
    """
    Pull each results-table row. Returns a list of dicts:
        {"courthouse": <friendly name>, "date": <datetime.date or None>,
         "text": "<Party | Case | ... >", "cells": [...]}
    Keeping the raw cells (not just the joined text) lets us pull out the
    Room field for the Zoom-link lookup, and the date field is used to
    build the calendar invite, without re-parsing the string.
    """
    rows = []
    for table in soup.find_all("table"):
        courthouse, hearing_date = table_metadata(table)
        for tr in table.find_all("tr"):
            cells = [cell.get_text(strip=True) for cell in tr.find_all(["td", "th"])]
            text = " | ".join(cells)
            if text.strip():
                rows.append({"courthouse": courthouse, "date": hearing_date,
                             "text": text, "cells": cells})
    return rows


def load_external_config():
    """
    Load SMTP_USER/SMTP_PASSWORD/SMTP_FROM/EMAIL_TO/SEARCH_TERM from
    courtwatch_config.json if it exists, overriding the placeholder defaults
    above. If the file doesn't exist yet, create a template there (using
    whatever defaults are currently set) and stop, so the person can fill in
    real values without ever needing to edit courtwatch.py itself.
    """
    global SMTP_USER, SMTP_PASSWORD, SMTP_FROM, EMAIL_TO, SEARCH_TERM, HEALTHCHECK_URL
    global SEARCH_LOCATIONS

    if not CONFIG_FILE.exists():
        template = {
            "smtp_user": SMTP_USER,
            "smtp_password": SMTP_PASSWORD,
            "smtp_from": SMTP_FROM,
            "email_to": EMAIL_TO,
            "search_term": SEARCH_TERM,
        }
        CONFIG_FILE.write_text(json.dumps(template, indent=2), encoding="utf-8")
        msg = (f"No config file found - created a template at {CONFIG_FILE}. "
               f"Edit it with your real email/password/search term, then rerun.")
        logger.error(msg)
        print(f"\n{msg}\n")
        sys.exit(1)

    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        msg = f"Config file {CONFIG_FILE} is not valid JSON: {e}"
        logger.error(msg)
        print(msg)
        sys.exit(1)

    SMTP_USER = data.get("smtp_user", SMTP_USER)
    SMTP_PASSWORD = data.get("smtp_password", SMTP_PASSWORD)
    # Fall back to "CourtWatch <actual login address>" if smtp_from
    # isn't in the config yet (e.g. an older config file from before this
    # setting existed) - add "smtp_from" to courtwatch_config.json to customize.
    SMTP_FROM = data.get("smtp_from", f"CourtWatch <{SMTP_USER}>")
    EMAIL_TO = data.get("email_to", EMAIL_TO)
    SEARCH_TERM = data.get("search_term", SEARCH_TERM)
    HEALTHCHECK_URL = data.get("healthcheck_url", HEALTHCHECK_URL)

    # Optional: override which courts are searched, e.g. to add Peterborough or
    # Cobourg, without editing this script. Each entry needs label/city/office;
    # a malformed list is refused rather than silently searching nothing.
    locations = data.get("locations")
    if locations:
        bad = [l for l in locations
               if not all(k in l for k in ("label", "city", "office"))
               or not (l.get("min_rows_per_day") or l.get("min_rows"))]
        if bad:
            msg = (f"Config 'locations' entries must each have label, city, "
                   f"office and min_rows_per_day. Offending entries: {bad}")
            logger.error(msg)
            print(msg)
            sys.exit(1)
        SEARCH_LOCATIONS = locations


def load_state() -> set:
    if STATE_FILE.exists():
        return set(json.loads(STATE_FILE.read_text()))
    return set()


def save_state(seen: set):
    STATE_FILE.write_text(json.dumps(sorted(seen)))


def parse_docket_time(time_str: str):
    """Parse a docket time like '10:00 am' -> (hour, minute) in 24h. None if unparseable."""
    try:
        t = datetime.strptime(time_str.strip(), "%I:%M %p")
        return t.hour, t.minute
    except (ValueError, AttributeError):
        return None


def ordinal(n: int) -> str:
    if 11 <= (n % 100) <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def format_date_ordinal(d: date) -> str:
    return f"{d.strftime('%B')} {ordinal(d.day)}, {d.year}"


def ics_escape(text: str) -> str:
    """Escape text per RFC 5545 (backslash, semicolon, comma, newline)."""
    return (str(text).replace("\\", "\\\\")
                       .replace(";", "\\;")
                       .replace(",", "\\,")
                       .replace("\n", "\\n"))


# Standard North American Eastern Time DST rules (2nd Sun of March / 1st Sun
# of November, in effect since 2007) - embedded so DTSTART/DTEND in the .ics
# resolve to the correct local Toronto time on any calendar client, rather
# than relying on a bare UTC offset that would be wrong for half the year.
TORONTO_VTIMEZONE = (
    "BEGIN:VTIMEZONE\r\n"
    "TZID:America/Toronto\r\n"
    "BEGIN:DAYLIGHT\r\n"
    "TZOFFSETFROM:-0500\r\n"
    "TZOFFSETTO:-0400\r\n"
    "TZNAME:EDT\r\n"
    "DTSTART:19700308T020000\r\n"
    "RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=2SU\r\n"
    "END:DAYLIGHT\r\n"
    "BEGIN:STANDARD\r\n"
    "TZOFFSETFROM:-0400\r\n"
    "TZOFFSETTO:-0500\r\n"
    "TZNAME:EST\r\n"
    "DTSTART:19701101T020000\r\n"
    "RRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=1SU\r\n"
    "END:STANDARD\r\n"
    "END:VTIMEZONE"
)

# Docket only gives a start time, never a duration - this is a guess.
EVENT_DURATION_MINUTES = 120


def build_google_calendar_link(summary: str, description: str, location: str,
                                start, end) -> str:
    """
    Build a Google Calendar 'quick add' URL: clicking it opens Google
    Calendar directly with the event pre-filled and a Save button - no
    .ics file involved, so it can't get hijacked by Outlook or any other
    program registered as the default handler for calendar files.
    start/end are naive datetimes already in Toronto local time; ctz tells
    Google Calendar to interpret them as that timezone.
    """
    dates = f"{start.strftime('%Y%m%dT%H%M%S')}/{end.strftime('%Y%m%dT%H%M%S')}"
    params = {
        "action": "TEMPLATE",
        "text": summary,
        "dates": dates,
        "details": description,
        "location": location,
        "ctz": "America/Toronto",
    }
    query = "&".join(f"{k}={quote(str(v))}" for k, v in params.items())
    return f"https://calendar.google.com/calendar/render?{query}"


def build_ics(events: list, organizer_email: str, attendee_emails: list) -> str:
    """
    events: list of dicts with keys uid, summary, description, location,
            start (naive datetime, Toronto local), end (naive datetime, Toronto local).
    Returns a full .ics file as a proper meeting invite (METHOD:REQUEST) so
    Gmail on both the organizer's and attendees' side offers to add/RSVP.
    """
    now_utc = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "PRODID:-//courtwatch//EN",
        "VERSION:2.0",
        "CALSCALE:GREGORIAN",
        "METHOD:REQUEST",
        TORONTO_VTIMEZONE,
    ]
    for ev in events:
        lines += [
            "BEGIN:VEVENT",
            f"UID:{ev['uid']}",
            f"DTSTAMP:{now_utc}",
            f"DTSTART;TZID=America/Toronto:{ev['start'].strftime('%Y%m%dT%H%M%S')}",
            f"DTEND;TZID=America/Toronto:{ev['end'].strftime('%Y%m%dT%H%M%S')}",
            f"SUMMARY:{ics_escape(ev['summary'])}",
            f"DESCRIPTION:{ics_escape(ev['description'])}",
            f"LOCATION:{ics_escape(ev['location'])}",
            f"ORGANIZER:mailto:{organizer_email}",
        ]
        for attendee in attendee_emails:
            lines.append(f"ATTENDEE;RSVP=TRUE;PARTSTAT=NEEDS-ACTION:mailto:{attendee}")
        lines += ["STATUS:CONFIRMED", "SEQUENCE:0", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def ping_healthcheck(suffix: str = "", payload: str = ""):
    """
    Ping healthchecks.io. suffix is "" for success, "start" when a run begins,
    or "fail" to mark the check down. payload is logged alongside the ping on
    the healthchecks side so a failure says WHY without opening the log file.

    Deliberately swallows every exception: monitoring must never be able to
    break, or change the outcome of, the thing it is monitoring. A ping that
    cannot be delivered is logged and otherwise ignored.
    """
    if not HEALTHCHECK_URL:
        logger.info("No healthcheck_url configured - skipping ping.")
        return
    url = HEALTHCHECK_URL.rstrip("/")
    if suffix:
        url = f"{url}/{suffix}"
    try:
        requests.post(url, data=payload.encode("utf-8")[:10000], timeout=10)
        logger.info(f"Healthcheck ping sent: {url}")
    except Exception as e:
        logger.warning(f"Healthcheck ping to {url} FAILED ({e}) - "
                        f"the run itself is unaffected.")


def send_email(subject: str, body: str, ics_content: str = None):
    if ics_content:
        msg = MIMEMultipart("mixed")
        msg["Subject"] = subject
        msg["From"] = SMTP_FROM
        msg["To"] = ", ".join(EMAIL_TO)
        msg.attach(MIMEText(body, "plain"))

        cal_part = MIMEText(ics_content, "calendar", "utf-8")
        cal_part.set_param("method", "REQUEST")
        cal_part.add_header("Content-Disposition", "attachment", filename="invite.ics")
        msg.attach(cal_part)
    else:
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = SMTP_FROM
        msg["To"] = ", ".join(EMAIL_TO)   # display header - shows both recipients

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.sendmail(SMTP_USER, EMAIL_TO, msg.as_string())  # actual delivery list
    logger.info(f"Email sent: {subject!r} to {EMAIL_TO}" +
                (" (with calendar invite)" if ics_content else ""))


# Public holiday lookup for the "no court today" note. canada-holidays.ca is a
# free, keyless JSON API covering federal and provincial holidays.
HOLIDAY_API = "https://canada-holidays.ca/api/v1/holidays?year={year}"


def lookup_holiday(d):
    """Federal or Ontario public holiday(s) on date d, e.g. "National Day for
    Truth and Reconciliation (federal)", or None. Best effort: a lookup
    failure only costs the name in the email, so it never fails the run."""
    if d is None:
        return None
    try:
        resp = requests.get(HOLIDAY_API.format(year=d.year), timeout=10)
        resp.raise_for_status()
        names = []
        for h in resp.json().get("holidays", []):
            if d.isoformat() not in (h.get("date"), h.get("observedDate")):
                continue
            ontario = any(p.get("id") == "ON" for p in h.get("provinces") or [])
            scope = [s for s, on in (("federal", h.get("federal")), ("Ontario", ontario)) if on]
            if scope:
                names.append(f"{h.get('nameEn')} ({', '.join(scope)})")
        return "; ".join(names) or None
    except Exception as e:
        logger.warning(f"Holiday lookup failed for {d}: {e}")
        return None


def watch(term: str, force: bool = False):
    seen = set() if force else load_state()
    if force:
        logger.info("--force given: ignoring courtwatch_state.json for this run (won't be modified).")
    new_matches = []
    all_rows_seen_this_run = 0
    rows_by_location = {}

    # One entry per (date, location) scrape: how many rows it returned and what
    # date the page itself said it was for. Thresholds are applied to these
    # individually.
    scrapes = []

    ping_healthcheck("start")

    with requests.Session() as session:
        session.headers.update({"User-Agent": "Mozilla/5.0"})

        for label, landing_url in DOCKET_URLS.items():
          for loc in SEARCH_LOCATIONS:
            # step_agree again for each search: after a search we are sitting on
            # dockets_view.aspx, and the next search needs a fresh VIEWSTATE from
            # daily-docket.aspx.
            # An HTTP error is recorded as a 0-row scrape rather than aborting
            # the run: on a court holiday the site answers 500 for a court with
            # no docket, and one court (or one date) failing must not stop the
            # others from being checked. The threshold check below still sees
            # it as 0 rows, so nothing slips through as an all-clear.
            error = None
            try:
                soup, search_url = step_agree(session, landing_url)
                soup = step_select_city(session, search_url, soup, loc["city"])
                soup = step_final_search(session, search_url, soup, loc["city"], loc["office"])
                rows = extract_rows(soup)
            except requests.RequestException as e:
                error = str(e)
                rows = []
                logger.warning(f"  {label}/{loc['label']}: search failed: {error}")
            all_rows_seen_this_run += len(rows)
            rows_by_location[loc["label"]] = rows_by_location.get(loc["label"], 0) + len(rows)

            # The page prints its own date above every results table - trust
            # that over any assumption about what "tomorrow" means. Only fall
            # back to the offset when there are no rows, and so no date to read.
            page_date = next((r["date"] for r in rows if r["date"]), None)
            assumed = False
            if page_date is None:
                offset = DOCKET_DATE_OFFSETS.get(label)
                if offset is not None:
                    page_date = date.today() + timedelta(days=offset)
                    assumed = True

            scrapes.append({"label": label, "loc": loc, "rows": len(rows),
                             "date": page_date, "assumed": assumed, "error": error})
            logger.info(f"  {label}/{loc['label']}: {len(rows)} rows"
                        f"{' [HTTP error]' if error else ''}"
                        f"  (page date: {page_date}"
                        f"{' [assumed - page had no rows]' if assumed else ''}"
                        f"{', ' + page_date.strftime('%A') if page_date else ''})")

            for row in rows:
                if term.lower() in row["text"].lower():
                    key = f"{label}|{loc['label']}|{row['courthouse']}|{row['text']}"
                    if key not in seen:
                        seen.add(key)
                        cells = row["cells"]
                        # Column layout confirmed via --discover-tables:
                        # Party | Case | Title | Time | Room | Event | Method [| Docket Line]
                        party_name = cells[0] if len(cells) > 0 else None
                        time_str = cells[3] if len(cells) > 3 else None
                        room = cells[4] if len(cells) > 4 else None
                        new_matches.append({
                            "label": label, "courthouse": row["courthouse"],
                            "text": row["text"], "date": row["date"],
                            "party_name": party_name, "time_str": time_str, "room": room,
                        })

    summary_by_loc = ", ".join(f"{k}={v}" for k, v in sorted(rows_by_location.items()))
    logger.info(f"Checked {len(DOCKET_URLS)} date(s) x {len(SEARCH_LOCATIONS)} location(s), "
                f"saw {all_rows_seen_this_run} total result rows ({summary_by_loc}).")

    # Scraping 0 rows is a FAILURE, not a quiet all-clear. A weekday docket
    # always has hundreds of rows, so 0 means the agree/city/search flow or the
    # results-page structure changed and we are not reading results at all.
    # Without this check that situation would send a reassuring "no matches
    # for <name>" email and report success to healthchecks - the worst
    # possible outcome, because it looks exactly like good news.
    # Checked PER LOCATION, not just on the total. With Toronto returning ~4,500
    # rows, a Lindsay search that silently broke would still leave the total
    # healthy and report success - the same false all-clear this gate exists to
    # prevent, just hidden behind a bigger city.
    # Each scrape is judged on its own, against that location's per-day floor.
    # Weekend dates are skipped: courts do not sit, so 0 rows there is correct
    # rather than a failure. Whether a Friday run even produces a Saturday
    # scrape is decided by the date the page printed, not by assumption.
    low, skipped = [], []      # low: (scrape, description) under its floor
    checked = {}               # date label -> scrapes that thresholds apply to
    for sc in scrapes:
        name, d = sc["loc"]["label"], sc["date"]
        if d is not None and d.weekday() >= 5:
            skipped.append(f"{sc['label']}/{name} ({d} {d.strftime('%a')}, weekend)")
            continue
        checked.setdefault(sc["label"], []).append(sc)
        floor = max(int(sc["loc"].get("min_rows_per_day",
                                       sc["loc"].get("min_rows", MIN_EXPECTED_ROWS))),
                    MIN_EXPECTED_ROWS)
        if sc["rows"] < floor:
            low.append((sc, f"{sc['label']}/{name} on {d} "
                            f"({sc['rows']} rows{', HTTP error' if sc['error'] else ''}, "
                            f"expected >= {floor})"))

    if skipped:
        logger.info(f"Skipped from thresholds (weekend): {'; '.join(skipped)}")

    # Court holiday: EVERY location is low or failed for one date while another
    # date in the same run is fully healthy. The site itself works (the healthy
    # date proves the flow and page structure are fine), so the likely reason is
    # that no court sits that day - e.g. 2026-09-30, Truth and Reconciliation
    # Day, when Toronto and Lindsay answered HTTP 500 and Newmarket had 2 rows.
    # That gets reported in the normal email instead of as a failure. A date
    # where only SOME locations are low is still a failure: a holiday closes
    # every court, a broken search usually breaks one.
    # Without a healthy date to compare against (e.g. Good Friday, when
    # "tomorrow" is a Saturday and skipped), a date still counts as closed if
    # the holiday lookup finds a public holiday on it.
    low_ids = {id(sc) for sc, _ in low}
    healthy = [l for l, scs in checked.items() if not any(id(x) in low_ids for x in scs)]
    all_low = [l for l in DOCKET_URLS
               if checked.get(l) and all(id(x) in low_ids for x in checked[l])]
    holiday = {l: lookup_holiday(checked[l][0]["date"]) for l in all_low}
    closed = {l for l in all_low if healthy or holiday[l]}
    notes = []
    for l in all_low:
        if l not in closed:
            continue
        d = checked[l][0]["date"]
        day = d.strftime("%a %Y-%m-%d") if d else l
        notes.append(f"No court {l} ({day}): every location returned no docket or an error"
                     + (f", while {', '.join(healthy)} looked normal" if healthy else "") + ". "
                     + (f"It's a public holiday: {holiday[l]}." if holiday[l] else
                        "No public holiday is listed for that date, so this is probably a court "
                        "closure, but if court should be sitting, check the docket manually."))
    closed_note = "\n".join(notes)
    if closed_note:
        logger.warning(closed_note)
    dead = [text for sc, text in low if sc["label"] not in closed]

    # Matches go out FIRST, so a hit is never dropped because some other scrape
    # came back low and the run then fails below.
    if new_matches:
        body_lines = [f"Found '{term}' on the daily court list:\n"]
        zoom_links_seen = {}    # dedupe: (courthouse, room) -> link or None
        events_seen = {}        # dedupe: (courthouse, room, date, time_str) -> event dict

        for m in new_matches:
            body_lines.append(f"[{m['label']}] [{m['courthouse']}]\n{m['text']}\n")
            zoom_links_seen[(m["courthouse"], m["room"])] = get_zoom_link(m["courthouse"], m["room"])

            event_key = (m["courthouse"], m["room"], m["date"], m["time_str"])
            if event_key not in events_seen:
                events_seen[event_key] = m   # keep first match's details for this event

        body_lines.append("\nZoom link(s) for the courtroom(s) above:")
        for (courthouse, room), link in zoom_links_seen.items():
            if link:
                body_lines.append(f"  Room {room} ({courthouse}): {link}")
            else:
                body_lines.append(f"  Room {room} ({courthouse}): no known Zoom link on file")

        # Build calendar events for everything we have a valid date+time for.
        ics_events = []
        gcal_links = []
        for (courthouse, room, hearing_date, time_str), m in events_seen.items():
            parsed_time = parse_docket_time(time_str) if time_str else None
            if not hearing_date or not parsed_time:
                logger.warning(f"Skipping calendar event (missing date/time): {m['text']}")
                continue
            hour, minute = parsed_time
            start = datetime(hearing_date.year, hearing_date.month, hearing_date.day, hour, minute)
            end = start + timedelta(minutes=EVENT_DURATION_MINUTES)

            first_name = m["party_name"]
            if first_name and "," in first_name:
                first_name = first_name.split(",", 1)[1].strip().title()

            link = get_zoom_link(courthouse, room)
            description = f"{m['text']}\n\nCourthouse: {courthouse} - Room {room}"
            summary = f"{first_name or term} Hearing"
            # Location holds the Zoom link itself when we have one - Google
            # Calendar recognizes a URL there and shows it as a clickable
            # "Join" link at the top of the event, rather than it being
            # buried in the description text.
            location = link if link else f"{courthouse} - Room {room} (no known Zoom link on file)"

            ics_events.append({
                "uid": f"courtwatch-{courthouse}-{room}-{start.isoformat()}@courtwatch",
                "summary": summary, "description": description, "location": location,
                "start": start, "end": end,
            })
            gcal_links.append((summary, start, build_google_calendar_link(
                summary, description, location, start, end)))

        if gcal_links:
            body_lines.append("\nAdd to Google Calendar (one click, no Outlook involved):")
            for summary, start, link in gcal_links:
                body_lines.append(f"  {summary} - {start.strftime('%I:%M %p')}: {link}")

        if closed_note:
            body_lines.append("\n" + closed_note)
        body = "\n".join(body_lines)

        subject = f"Court docket alert: '{term}' found"
        if ics_events:
            first_ev = ics_events[0]
            subject = f"{first_ev['summary']} ({format_date_ordinal(first_ev['start'].date())})"

        ics_content = build_ics(ics_events, SMTP_USER, EMAIL_TO) if ics_events else None

        logger.info(body)
        send_email(subject, body, ics_content=ics_content)
        if force:
            logger.info("--force given: not saving state, so this match will re-alert next run too.")
        else:
            save_state(seen)

    if dead:
        msg = (f"SCRAPE FAILURE / COURT CLOSED: {'; '.join(dead)}.\n\n"
               f"Rows per location this run: {summary_by_loc or '(none)'}\n"
               f"Per-scrape detail: "
               + "; ".join(f"{s['label']}/{s['loc']['label']}={s['rows']} "
                           f"({s['date']}{' assumed' if s['assumed'] else ''})"
                           for s in scrapes) + "\n"
               + (f"Skipped as weekend: {'; '.join(skipped)}\n" if skipped else "")
               + "\n"
               f"Either the docket is genuinely much lighter than usual - a statutory "
               f"holiday or long weekend, which you asked to be told about - or that "
               f"location's search flow, page structure or courthouse code has changed "
               f"and rows are being missed.\n\n"
               f"EITHER WAY this is NOT an all-clear for '{term}': check the docket "
               f"manually. If the site moved, re-run the --discover* modes to find out "
               f"what changed."
               + (f"\n\n{closed_note}" if closed_note else ""))
        logger.error(msg)
        ping_healthcheck("fail", msg)
        if ALWAYS_SEND_EMAIL:
            send_email(f"CourtWatch: low/no rows from "
                        f"{', '.join(sorted({d.split('/')[1].split(' on ')[0] for d in dead}))} "
                        f"- check '{term}' manually", msg)
        sys.exit(2)

    if not new_matches:
        logger.info(f"No new matches for '{term}' this run.")
        if ALWAYS_SEND_EMAIL:
            body = (f"Checked today's and tomorrow's court lists for "
                     f"{', '.join(l['label'] for l in SEARCH_LOCATIONS)} "
                     f"({all_rows_seen_this_run} total rows scanned; "
                     f"{summary_by_loc}) - no matches for '{term}'.")
            subject = f"Court docket check: no matches for '{term}'"
            if closed_note:
                body += "\n\n" + closed_note
                names = [holiday[l].split(" (")[0] for l in DOCKET_URLS if l in closed and holiday[l]]
                subject += (f" (no court {' / '.join(l for l in DOCKET_URLS if l in closed)}"
                            f" - {', '.join(names) if names else 'likely a holiday'})")
            send_email(subject, body)

    ping_healthcheck("", f"OK - {all_rows_seen_this_run} rows scanned ({summary_by_loc}) "
                          f"across {len(DOCKET_URLS)} date(s), {len(new_matches)} new "
                          f"match(es) for '{term}'." + (f" {closed_note}" if closed_note else ""))


def main():
    logger.info(f"--- courtwatch v{__version__} invoked with args: {sys.argv[1:]!r} ---")

    parser = argparse.ArgumentParser()
    parser.add_argument("--version", action="version",
                         version=f"courtwatch {__version__}")
    parser.add_argument("--discover", metavar="URL", help="Print form fields found on URL (landing page)")
    parser.add_argument("--discover-search", metavar="URL",
                         help="Agree, then print form fields of the resulting search page")
    parser.add_argument("--discover-city", metavar="URL",
                         help="Agree, then select Toronto and print the resulting fields")
    parser.add_argument("--discover-final", metavar="URL",
                         help="Run the full agree->city->search flow and dump raw HTML for inspection")
    parser.add_argument("--discover-tables", metavar="URL",
                         help="Run the full flow and inspect what precedes each results table (for courthouse names)")
    parser.add_argument("--watch", action="store_true", help="Run the actual watch/search")
    parser.add_argument("--force", action="store_true",
                         help="Bypass courtwatch_state.json dedupe for this run (useful for repeated "
                              "testing); the file is neither read nor written when this is set")
    parser.add_argument("--term", default=None,
                         help="Override SEARCH_TERM for this run only (e.g. --term SWANEK for testing)")
    args = parser.parse_args()

    try:
        if args.discover:
            discover(args.discover)
        elif args.discover_search:
            discover_search(args.discover_search)
        elif args.discover_city:
            discover_city(args.discover_city)
        elif args.discover_final:
            discover_final(args.discover_final)
        elif args.discover_tables:
            discover_tables(args.discover_tables)
        elif args.watch:
            load_external_config()
            watch(args.term or SEARCH_TERM, force=args.force)
        else:
            logger.warning("No mode flag given (expected --watch or a --discover* flag) - printing help and exiting.")
            parser.print_help()
            sys.exit(1)
    except Exception:
        tb = traceback.format_exc()
        logger.error(f"UNHANDLED ERROR:\n{tb}")
        # Only --watch loads the config, so HEALTHCHECK_URL is only populated
        # for that path; for the --discover* modes this is a no-op.
        ping_healthcheck("fail", f"UNHANDLED ERROR:\n{tb}")
        sys.exit(1)


if __name__ == "__main__":
    main()
