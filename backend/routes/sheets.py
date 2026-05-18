"""
Google Sheets integration routes.

Endpoints:
  GET/PUT /api/sheets/<account_id>/config        – get/save the Google Sheet ID
  GET     /api/sheets/<account_id>/preview        – preview matched campaigns (sheet ↔ DB)
  POST    /api/sheets/<account_id>/sync-budgets   – pull monthly budgets from sheet → DB
  POST    /api/sheets/<account_id>/write-spend    – push MTD spend + last paced date → sheet

Meta section column layout (each data row under the ``Meta`` header):

  A — Campaign label (matched to the tracked campaign name in the app)
  B — Monthly budget
  C — MTD spend (usually written back by the app)
  D — Optional **account scope**: Budget Buddy ``account_name`` or Meta ad account id
      (``act_…`` or numeric). When filled, the row is only used when syncing **that**
      account. Leave blank for legacy sheets (row applies to whichever account is syncing).
  E — Reserved / free-form
  F — Notes (ABO allocation lines, ``Name - X%``)
  G — Last paced date

Requires:
  - GOOGLE_CREDENTIALS_JSON env var (Railway secret) containing a service account JSON key
  - The service account must have Viewer (for read) or Editor (for write) access to the sheet
"""

import json
import logging
import os
import re
import time
from datetime import datetime

from flask import Blueprint, jsonify, request, session

from database import Account, AccountSettings, Campaign, PacingData, db
from routes.auth import login_required

logger = logging.getLogger(__name__)

sheets_bp = Blueprint("sheets", __name__, url_prefix="/api/sheets")


def _sheets_retry(fn, *args, max_retries=4, **kwargs):
    """Call a gspread function with exponential backoff on 429 quota errors.

    Waits 15 s → 30 s → 60 s between retries. After max_retries failures the
    last exception is re-raised so callers can surface a clean error message.
    """
    for attempt in range(max_retries):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            is_quota = "429" in str(exc) or "quota" in str(exc).lower()
            if is_quota and attempt < max_retries - 1:
                wait = 15 * (2 ** attempt)   # 15 s, 30 s, 60 s
                logger.warning(
                    "Google Sheets 429 rate-limit hit (attempt %d/%d); "
                    "retrying in %s s…",
                    attempt + 1, max_retries, wait,
                )
                time.sleep(wait)
            else:
                raise


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_gspread_client():
    """Build an authenticated gspread client from GOOGLE_CREDENTIALS_JSON env var."""
    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError:
        raise RuntimeError(
            "gspread / google-auth not installed. "
            "Add them to requirements.txt and redeploy."
        )

    creds_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
    if not creds_json:
        raise ValueError("GOOGLE_CREDENTIALS_JSON environment variable is not set on this server.")

    try:
        creds_dict = json.loads(creds_json)
    except json.JSONDecodeError as e:
        raise ValueError(f"GOOGLE_CREDENTIALS_JSON is not valid JSON: {e}")

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive.readonly",
    ]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    return gspread.authorize(creds)


def _sheet_id_from_url_or_id(value: str) -> str:
    """Extract the spreadsheet ID from a full Google Sheets URL or return the raw value."""
    m = re.search(r"/d/([a-zA-Z0-9_-]+)", value)
    return m.group(1) if m else value.strip()


def _parse_float(val: str):
    """Strip currency symbols and commas, return float or None."""
    if not val:
        return None
    cleaned = str(val).replace("$", "").replace(",", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


def _norm_meta_id_digits(raw: str) -> str:
    """Digits-only form of a Meta ad account id for comparison (handles act_ / punctuation)."""
    if not raw:
        return ""
    return re.sub(r"\D+", "", str(raw).lower().replace("act_", ""))


def _sheet_row_matches_account(scope_cell: str, account: Account) -> bool:
    """Blank column D → row applies to whichever Budget Buddy account is syncing (legacy).

    Otherwise the cell must match this account's name (case-insensitive) or Meta ad account id.

    If column D contains a numeric value (e.g. a Daily Spend formula like $9.65), it is
    NOT treated as an account scope — the row applies to all accounts. Account scopes are
    always text (account names or Meta IDs), never numbers.
    """
    if scope_cell is None or not str(scope_cell).strip():
        return True
    # If the cell is numeric (e.g. a Daily Spend formula like $9.65), ignore it for scoping.
    cleaned = str(scope_cell).strip().replace("$", "").replace(",", "")
    try:
        float(cleaned)
        return True  # Numeric value — not an account scope, legacy behaviour
    except ValueError:
        pass
    # Treat Google Sheets formula errors (#DIV/0!, #N/A, #VALUE!, #REF!, etc.) as non-scope.
    # Column D in this sheet contains a daily-spend formula; when the budget cell (B) is
    # blank or zero the formula resolves to an error string rather than an account name.
    if cleaned.strip().startswith('#'):
        return True
    if not account:
        return False
    s = str(scope_cell).strip()
    if s.lower() == (account.account_name or "").strip().lower():
        return True
    cell_digits = _norm_meta_id_digits(s)
    acct_digits = _norm_meta_id_digits(account.meta_account_id or "")
    return bool(cell_digits and acct_digits and cell_digits == acct_digits)


def _scope_is_explicit_match(scope_cell: str, account: Account) -> bool:
    """Return True when column D is a real text scope that explicitly names this account.

    Distinguishes the three cases where _sheet_row_matches_account returns True:
      1. Blank / empty  → implicit (applies to all) → False here
      2. Numeric value  → implicit (formula result, not a scope) → False here
      3. Text that names this account or its Meta ID → explicit → True here

    Used to skip prefix-based routing (_row_prefix_matches_account) when the sheet
    row has already been pinned to a specific account via column D.  This matters for
    campaigns whose name starts with an agency prefix (e.g. "Commit - Boosting 2026"
    for the Choice Greens account) — the prefix would otherwise route the row to the
    Commit account even though col D says "Choice Greens".
    """
    if not scope_cell or not str(scope_cell).strip():
        return False  # blank — implicit
    cleaned = str(scope_cell).strip().replace("$", "").replace(",", "")
    try:
        float(cleaned)
        return False  # numeric formula result — implicit
    except ValueError:
        pass
    if cleaned.strip().startswith('#'):
        return False  # formula error — implicit
    # It's a real text scope — check if it names this account
    return _sheet_row_matches_account(scope_cell, account)


def _extract_bracket_scope(name: str, fallback_scope: str):
    """Detect bracket-scope notation in a sheet row name.

    If the name starts with "[Account Name]", extract the bracketed text as an
    explicit account scope and return the remainder as the clean campaign name.

    Examples:
      "[Phoenix Raceway - NASCAR] FB/IG Ads (Primary Geo)"
        → name="FB/IG Ads (Primary Geo)", scope="Phoenix Raceway - NASCAR"

      "Camelback - Lodge"  (no brackets)
        → name="Camelback - Lodge", scope=fallback_scope  (unchanged)

    The bracket scope takes priority over whatever is in column D (fallback_scope).
    """
    import re
    if not name:
        return name, fallback_scope
    m = re.match(r'^\[([^\]]+)\]\s*(.*)', name)
    if not m:
        return name, fallback_scope
    bracket_content = m.group(1).strip()
    remainder = m.group(2).strip()
    # Use the bracketed text as scope; fall back to original name if remainder is empty
    return (remainder or name), (bracket_content or fallback_scope)


def _get_meta_section(worksheet):
    """
    Return rows from the Meta section of the worksheet.

    Scans for a row where column A is exactly "Meta" (case-insensitive header),
    then reads data rows until a "LinkedIn" or "TikTok" header or EOF.

    Data is loaded with a fixed **A:G** range per row so empty column C (MTD)
    does not collapse — ``get_all_values()`` jagged rows used to shift column D
    into index 2, making monthly budget look like it came from D.

    Column layout: A name, B monthly budget, C MTD spend, D account scope,
    E reserved, F notes, G last paced.

    Returns a list of dicts:
      { row_index (1-based int), name (str), account_scope (str),
        monthly_budget (float|None), mtd_spend (float|None), notes (str), last_paced (str) }
    """
    all_values = _sheets_retry(worksheet.get_all_values)
    STOP_KEYWORDS = {"linkedin", "tiktok"}

    meta_idx = None
    stop_idx = len(all_values)
    for i, row in enumerate(all_values):
        col_a = (row[0] if row else "").strip().lower()
        if meta_idx is None:
            if col_a == "meta":
                meta_idx = i
            continue
        if col_a in STOP_KEYWORDS:
            stop_idx = i
            break

    if meta_idx is None:
        return []

    # 1-based sheet rows: "Meta" is meta_idx+1; first data row is meta_idx+2.
    first_sr = meta_idx + 2
    # stop_idx is 0-based index of LinkedIn/TikTok row, or len(all_values).
    # Last Meta data row (1-based) equals stop_idx when terminator exists, else len.
    last_sr = stop_idx if stop_idx < len(all_values) else len(all_values)

    if first_sr > last_sr:
        return []

    # Slice the already-fetched all_values instead of making a second API call.
    # first_sr and last_sr are 1-based; Python slicing is 0-based exclusive end.
    grid = all_values[first_sr - 1 : last_sr]

    rows = []
    if grid:
        for off, raw in enumerate(grid):
            r = list(raw) + [""] * (7 - len(raw))
            r = r[:7]
            if not any(str(c).strip() for c in r):
                continue
            name = r[0].strip()
            monthly_budget = _parse_float(r[1])
            mtd_spend = _parse_float(r[2])
            account_scope = r[3].strip()
            notes = r[5].strip()
            last_paced = r[6].strip()
            sheet_row = first_sr + off
            # Bracket-scope notation: "[Account Name] Campaign Name"
            # Takes priority over column D. Strips the bracket from the name
            # so campaign matching only sees "Campaign Name".
            name, account_scope = _extract_bracket_scope(name, account_scope)
            if name:
                rows.append({
                    "row_index": sheet_row,
                    "name": name,
                    "account_scope": account_scope,
                    "monthly_budget": monthly_budget,
                    "mtd_spend": mtd_spend,
                    "notes": notes,
                    "last_paced": last_paced,
                })
        return rows

    # Fallback if range read failed: old jagged behavior (best-effort).
    in_meta = False
    for i, row in enumerate(all_values):
        col_a = (row[0] if row else "").strip().lower()
        if not in_meta:
            if col_a == "meta":
                in_meta = True
            continue
        if col_a in STOP_KEYWORDS:
            break
        if not any(cell.strip() for cell in row):
            continue
        name = row[0].strip() if len(row) > 0 else ""
        account_scope = row[3].strip() if len(row) > 3 else ""
        monthly_budget = _parse_float(row[1]) if len(row) > 1 else None
        mtd_spend = _parse_float(row[2]) if len(row) > 2 else None
        notes = row[5].strip() if len(row) > 5 else ""
        last_paced = row[6].strip() if len(row) > 6 else ""
        name, account_scope = _extract_bracket_scope(name, account_scope)
        if name:
            rows.append({
                "row_index": i + 1,
                "name": name,
                "account_scope": account_scope,
                "monthly_budget": monthly_budget,
                "mtd_spend": mtd_spend,
                "notes": notes,
                "last_paced": last_paced,
            })
    return rows


# Common words we ignore when scoring overlap — they appear everywhere and would
# inflate the score without indicating a real match.
_STOP_TOKENS = {
    "the", "and", "ads", "ad", "campaign", "campaigns", "fb", "ig", "facebook",
    "instagram", "meta", "social", "for", "of", "to", "in", "on", "at", "a",
    "an", "is", "by", "or", "with", "now", "new",
}


def _stem(token: str) -> str:
    """Light stemmer — strip common English suffixes so 'weddings' == 'wedding'.

    Not a real Porter stemmer; just enough to handle plural / -ing / -ed variants
    that show up in campaign names. Cheap, no external deps.
    """
    if len(token) <= 3:
        return token
    for suf in ("ings", "ing", "ies", "ied", "ed", "es", "s"):
        if token.endswith(suf) and len(token) - len(suf) >= 3:
            base = token[: -len(suf)]
            # "ies" → "y" (stories → story)
            if suf == "ies":
                base += "y"
            return base
    return token


def _tokenise(s: str) -> set:
    """Lowercase alphanumeric tokens, length > 1, with light stemming and stopwords removed."""
    import re
    raw = [t for t in re.findall(r"[a-z0-9]+", (s or "").lower()) if len(t) > 1]
    return {_stem(t) for t in raw if t not in _STOP_TOKENS}


def _word_overlap_score(name1: str, name2: str) -> float:
    """
    Fraction of the shorter name's meaningful tokens that appear in the longer name.

    Uses light stemming so "weddings" matches "wedding" and "boosting" matches "boost".
    Stopwords ("the", "ads", "campaign", "fb", "ig"…) are ignored so they don't
    inflate scores or count as matches on their own.
    """
    t1, t2 = _tokenise(name1), _tokenise(name2)
    if not t1 or not t2:
        return 0.0
    shorter = t1 if len(t1) <= len(t2) else t2
    return len(shorter & t1 & t2) / len(shorter)   # overlap / shorter-set size


# Threshold for fuzzy match. 0.5 means: at least half of the shorter side's
# distinctive tokens must appear in the longer side. Loose enough that
# "Resort - Weddings" matches "Resort 2026: Wedding Booking" but tight enough
# that two clearly-different campaigns don't get cross-matched.
_FUZZY_MATCH_THRESHOLD = 0.5
_SCORE_TIE_EPS = 1e-6


def _match_campaign_with_score(sheet_name: str, db_campaigns: list):
    """
    Match a sheet row name to a Campaign object, returning (campaign, score).

    Priority:
      1. Exact match            → score 4.0 (sentinel)
      2. Case-insensitive match → score 3.0 (sentinel)
      3. Substring match (unique) → score 2.0 (sentinel)
      4. Word-overlap ≥ threshold with a clear winner → score = overlap fraction

    Returns (None, 0.0) when no credible match is found.
    """
    if not sheet_name or not db_campaigns:
        return None, 0.0

    campaigns = sorted(db_campaigns, key=lambda c: (c.campaign_name or "").lower())

    for c in campaigns:
        if c.campaign_name == sheet_name:
            return c, 4.0

    sheet_lower = sheet_name.lower()
    for c in campaigns:
        if c.campaign_name.lower() == sheet_lower:
            return c, 3.0

    substring_hits = []
    for c in campaigns:
        meta_lower = c.campaign_name.lower()
        if sheet_lower in meta_lower or meta_lower in sheet_lower:
            substring_hits.append(c)
    if len(substring_hits) == 1:
        return substring_hits[0], 2.0
    pool = substring_hits if len(substring_hits) > 1 else campaigns

    scored = [(_word_overlap_score(sheet_name, c.campaign_name), c) for c in pool]
    scored.sort(key=lambda x: (-x[0], (x[1].campaign_name or "").lower()))
    best_score, best_c = scored[0]
    second_score = scored[1][0] if len(scored) > 1 else -1.0
    if best_score >= _FUZZY_MATCH_THRESHOLD and (best_score - second_score) > _SCORE_TIE_EPS:
        return best_c, best_score
    return None, 0.0


def _match_campaign(sheet_name: str, db_campaigns: list):
    """Convenience wrapper — returns just the campaign (or None)."""
    campaign, _ = _match_campaign_with_score(sheet_name, db_campaigns)
    return campaign


def _match_all_campaigns(sheet_name: str, db_campaigns: list) -> list:
    """Return ALL campaigns that are a credible match for a sheet row name.

    Used by write-spend so that one sheet row (e.g. "Camelback - Weddings")
    can collect spend from multiple Meta campaigns (e.g. "Commit 2026: Weddings
    - Traffic" + "Commit 2026: Wedding Brochure") and write their combined MTD.

    Unlike _match_campaign (1-to-1, tie-breaking), this intentionally skips the
    substring shortcut and goes straight to full fuzzy scoring so that a literal
    match on one campaign ("weddings" in "Weddings - Traffic") doesn't prevent
    the stemmed token pass from also catching "Wedding Brochure".  Both score
    1.0 against the stripped sheet name "Weddings" (shared token: "wedding"),
    so both are returned and their spends are summed.
    """
    if not sheet_name or not db_campaigns:
        return []

    campaigns = sorted(db_campaigns, key=lambda c: (c.campaign_name or "").lower())

    # Exact (unambiguous — return immediately without scoring)
    for c in campaigns:
        if c.campaign_name == sheet_name:
            return [c]

    sheet_lower = sheet_name.lower()
    # Case-insensitive exact (unambiguous)
    for c in campaigns:
        if c.campaign_name.lower() == sheet_lower:
            return [c]

    # Full fuzzy scoring — no substring shortcut so stemming catches ties.
    # Return every campaign that shares the top score above the threshold.
    scored = [(_word_overlap_score(sheet_name, c.campaign_name), c) for c in campaigns]
    scored.sort(key=lambda x: (-x[0], (x[1].campaign_name or "").lower()))
    best_score = scored[0][0]
    if best_score < _FUZZY_MATCH_THRESHOLD:
        return []
    return [c for score, c in scored if score >= best_score - _SCORE_TIE_EPS]


def _match_adset(needle_name: str, adsets: list):
    """Match a Notes-column label to an AdSet (same rules as _match_campaign)."""
    if not needle_name or not adsets:
        return None

    rows = sorted(adsets, key=lambda a: (a.adset_name or "").lower())
    needle_lower = needle_name.lower()

    for a in rows:
        if a.adset_name == needle_name:
            return a
    for a in rows:
        if a.adset_name.lower() == needle_lower:
            return a

    substring_hits = []
    for a in rows:
        a_lower = a.adset_name.lower()
        if needle_lower in a_lower or a_lower in needle_lower:
            substring_hits.append(a)
    if len(substring_hits) == 1:
        return substring_hits[0]
    pool = substring_hits if len(substring_hits) > 1 else rows

    scored = [(_word_overlap_score(needle_name, a.adset_name), a) for a in pool]
    scored.sort(key=lambda x: (-x[0], (x[1].adset_name or "").lower()))
    best_score, best_a = scored[0]
    second_score = scored[1][0] if len(scored) > 1 else -1.0
    if best_score >= _FUZZY_MATCH_THRESHOLD and (best_score - second_score) > _SCORE_TIE_EPS:
        return best_a
    return None


def _parse_allocations_from_notes(notes: str):
    """Try to read allocation %s out of a Notes-column cell.

    Handles two formats (can be mixed within the same cell):

      Format A — ABO adset style:  ``"<name> - <pct>%"``
        "Pays to Play - 40% / Sports Bar - 30% / Free Slot Play - 30%"
          → [("Pays to Play", 40.0), ("Sports Bar", 30.0), ("Free Slot Play", 30.0)]

      Format B — CBO campaign split:  ``"<pct>% to <name>"``
        "70% to Weddings / 30% to Wedding Brochure"
          → [("Weddings", 70.0), ("Wedding Brochure", 30.0)]

    Returns ``None`` if any chunk doesn't match either pattern, or if the parsed
    %s don't sum to ~100 (±1.5). This is conservative on purpose — flight notes
    like "Cinco De Mayo (5/2 End)" must not be misread as allocations.
    """
    if not notes or not notes.strip():
        return None

    import re
    chunks = [c.strip() for c in re.split(r"\s*/\s*|\n", notes) if c.strip()]
    if not chunks:
        return None

    # Format A: "Name - 40%"  (any dash variant)
    pat_a = re.compile(r"^(.+?)\s*[-–—]\s*(\d+(?:\.\d+)?)\s*%\s*$")
    # Format B: "70% to Name"
    pat_b = re.compile(r"^(\d+(?:\.\d+)?)\s*%\s+to\s+(.+?)\s*$", re.IGNORECASE)

    parsed = []
    for chunk in chunks:
        m = pat_a.match(chunk)
        if m:
            name, pct = m.group(1).strip(), float(m.group(2))
        else:
            m2 = pat_b.match(chunk)
            if m2:
                pct, name = float(m2.group(1)), m2.group(2).strip()
            else:
                return None  # non-conforming chunk → bail (flight notes, dates, etc.)
        if not name or pct < 0 or pct > 100:
            return None
        parsed.append((name, pct))

    total = sum(p for _, p in parsed)
    if abs(total - 100.0) > 1.5:
        return None  # don't apply partial allocations

    return parsed


def _strip_account_prefix(sheet_name: str, current_account, all_accounts: list) -> str:
    """Strip the account-identifier prefix from a sheet row name before campaign matching.

    Sheet rows follow "AccountPrefix - Campaign Name" format. Without stripping, the
    prefix tokens bleed into the word-overlap score and cause false matches.

    Handles account names that themselves contain " - " (e.g. "Phoenix Raceway - NASCAR").
    The old approach split only on the *first* " - " and would leave "NASCAR - Campaign"
    instead of "Campaign" for rows like "Phoenix Raceway - NASCAR - Campaign Name".

    Now tries every possible " - "-delimited prefix (shortest to longest) and picks the
    one whose tokens best match the account name (symmetric overlap: penalises both
    under-match and over-match). Ties go to the longest prefix so that "Phoenix Raceway -
    NASCAR" beats "Phoenix Raceway" when the account name IS "Phoenix Raceway - NASCAR".

    Strips only when the best-scoring prefix matches the current account at least as well
    as any other account (no other account owns this prefix more strongly).

    Returns the original name unchanged when stripping is inappropriate.
    """
    if not sheet_name or ' - ' not in sheet_name:
        return sheet_name

    parts = sheet_name.split(' - ')
    acct_tokens = _tokenise(current_account.account_name or "")
    if not acct_tokens:
        return sheet_name

    best_score = -1.0
    best_i = None

    # Try each possible prefix length (1 segment up to N-1 segments).
    for i in range(1, len(parts)):
        prefix = ' - '.join(parts[:i]).strip()
        if not prefix:
            continue

        prefix_tokens = _tokenise(prefix)
        if not prefix_tokens:
            continue

        # Symmetric score: overlap / max(len) penalises prefixes that contain
        # extra tokens not in the account name AND prefixes that are too short.
        overlap = len(prefix_tokens & acct_tokens)
        score = overlap / max(len(prefix_tokens), len(acct_tokens))

        if score < _FUZZY_MATCH_THRESHOLD:
            continue

        # Verify this prefix doesn't match another account better.
        better_exists = False
        for acct in all_accounts:
            if acct.id == current_account.id:
                continue
            other_tokens = _tokenise(acct.account_name or "")
            if not other_tokens:
                continue
            other_overlap = len(prefix_tokens & other_tokens)
            other_score = other_overlap / max(len(prefix_tokens), len(other_tokens))
            if other_score > score:
                better_exists = True
                break
        if better_exists:
            continue

        # Prefer higher score; ties go to the longest prefix (largest i).
        if score > best_score or (score == best_score and best_i is not None and i > best_i):
            best_score = score
            best_i = i

    if best_i is None:
        return sheet_name

    stripped = ' - '.join(parts[best_i:]).strip()
    return stripped if stripped else sheet_name


def _row_prefix_matches_account(sheet_name: str, current_account, all_accounts: list) -> bool:
    """Account-prefix scoping for sheets that use "AccountPrefix - Campaign Name" naming.

    Many sheets embed a short account identifier at the start of each row name, e.g.:
      "Amara - Amara Resort and Spa - Commit 2026: Boosting"  → Amara account only
      "Commit - 2026: Boosted Posts"                          → Commit Agency only
      "Camelback - Lodge"                                     → Camelback account only

    This is important when the agency name ("Commit") appears in campaign names
    across many accounts — without prefix scoping, "Commit - 2026: Boosted Posts"
    would fuzzy-match Amara's "Commit 2026: Boosting" campaign via word overlap.

    Returns False (skip this row) only when:
      - The row name contains " - " (has a prefix segment), AND
      - That prefix is a significantly better fuzzy match for a *different* account
        than for the current account.

    Returns True in all other cases (no prefix, ambiguous prefix, prefix matches
    current account).
    """
    if not sheet_name or ' - ' not in sheet_name:
        return True

    prefix = sheet_name.split(' - ')[0].strip()
    if not prefix:
        return True

    current_score = _word_overlap_score(prefix, current_account.account_name or "")

    for acct in all_accounts:
        if acct.id == current_account.id:
            continue
        other_score = _word_overlap_score(prefix, acct.account_name or "")
        # Skip this row if another account is a clearly better match for the prefix.
        # Require both: other_score exceeds current AND clears the fuzzy threshold,
        # so a prefix like "Boost" that loosely matches many accounts doesn't over-filter.
        if other_score > current_score and other_score >= _FUZZY_MATCH_THRESHOLD:
            return False

    return True


def _match_type_label(sheet_name: str, campaign) -> str:
    if campaign is None:
        return "none"
    if sheet_name == campaign.campaign_name:
        return "exact"
    if sheet_name.lower() == campaign.campaign_name.lower():
        return "case_insensitive"
    sheet_lower = sheet_name.lower()
    meta_lower = campaign.campaign_name.lower()
    if sheet_lower in meta_lower or meta_lower in sheet_lower:
        return "partial"
    return "word_overlap"


def _user_owns_account(account_id: int) -> bool:
    """Returns True iff the account exists.

    Name kept for back-compat. Session 13 — shared workspace: every logged-in
    user can read/write every account's sheet config and run sync against it.
    The @login_required decorator on each endpoint guarantees auth.
    """
    return Account.query.get(account_id) is not None


def _open_month_worksheet(spreadsheet):
    """Open the current month's tab (e.g. 'May 2026'), case-insensitive."""
    month_name = datetime.utcnow().strftime("%B %Y")
    try:
        return spreadsheet.worksheet(month_name), month_name
    except Exception:
        titles = [s.title for s in spreadsheet.worksheets()]
        match = next((t for t in titles if t.lower() == month_name.lower()), None)
        if not match:
            raise ValueError(
                f"No tab found for '{month_name}'. "
                f"Available tabs: {', '.join(titles)}"
            )
        return spreadsheet.worksheet(match), match


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@sheets_bp.route("/<int:account_id>/config", methods=["GET", "PUT"])
@login_required
def sheet_config(account_id):
    """Get or save the Google Sheet URL/ID for this account."""
    if not _user_owns_account(account_id):
        return jsonify({"error": "Not found"}), 404

    settings = AccountSettings.query.filter_by(account_id=account_id).first()
    if not settings:
        return jsonify({"error": "Account settings not found"}), 404

    if request.method == "GET":
        return jsonify({"google_sheet_id": settings.google_sheet_id or ""}), 200

    data = request.get_json() or {}
    raw = data.get("google_sheet_id", "")
    settings.google_sheet_id = _sheet_id_from_url_or_id(raw) if raw.strip() else ""
    db.session.commit()
    return jsonify({"google_sheet_id": settings.google_sheet_id}), 200


@sheets_bp.route("/<int:account_id>/preview", methods=["GET"])
@login_required
def preview_matches(account_id):
    """
    Open the current month's sheet tab and show which rows match DB campaigns.

    Returns:
      { sheet_tab, total_sheet_rows, matched, unmatched,
        matches: [ { sheet_name, monthly_budget, mtd_spend, last_paced,
                     row_index, matched_campaign_id, matched_campaign_name, match_type } ] }
    """
    if not _user_owns_account(account_id):
        return jsonify({"error": "Not found"}), 404

    settings = AccountSettings.query.filter_by(account_id=account_id).first()
    if not settings or not (settings.google_sheet_id or "").strip():
        return jsonify({"error": "Google Sheet not configured. Save a Sheet URL first."}), 400

    try:
        gc = _get_gspread_client()
        spreadsheet = gc.open_by_key(settings.google_sheet_id)
        ws, tab_name = _open_month_worksheet(spreadsheet)
    except (ValueError, RuntimeError) as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        # Don't echo the raw exception text — google-auth/gspread errors can include
        # request URLs, internal paths, or token fragments. Log full detail server-side.
        logger.exception("Could not open Google Sheet for account %s", account_id)
        return jsonify({"error": "Could not open Google Sheet. Check the URL and that the service account has access."}), 400

    sheet_rows = _get_meta_section(ws)
    db_campaigns = Campaign.query.filter_by(account_id=account_id, is_active=True).all()
    account = Account.query.get(account_id)
    # Session 13 — shared workspace: prefix scoping considers every account
    # in the DB, not just the originally-linked user's accounts.
    all_user_accounts = Account.query.all()

    matches = []
    for row in sheet_rows:
        scope = row.get("account_scope") or ""
        if not _sheet_row_matches_account(scope, account):
            matches.append({
                "sheet_name": row["name"],
                "account_scope": scope,
                "monthly_budget": row["monthly_budget"],
                "mtd_spend": row["mtd_spend"],
                "last_paced": row["last_paced"],
                "row_index": row["row_index"],
                "matched_campaign_id": None,
                "matched_campaign_name": None,
                "match_type": "account_scope_mismatch",
            })
            continue
        # When col D is a real text scope that explicitly names this account, trust it
        # and skip the prefix check.  See _scope_is_explicit_match for rationale.
        if not _scope_is_explicit_match(scope, account) and not _row_prefix_matches_account(row["name"], account, all_user_accounts):
            matches.append({
                "sheet_name": row["name"],
                "account_scope": scope,
                "monthly_budget": row["monthly_budget"],
                "mtd_spend": row["mtd_spend"],
                "last_paced": row["last_paced"],
                "row_index": row["row_index"],
                "matched_campaign_id": None,
                "matched_campaign_name": None,
                "match_type": "account_scope_mismatch",
            })
            continue
        match_name = _strip_account_prefix(row["name"], account, all_user_accounts)
        campaign = _match_campaign(match_name, db_campaigns)
        matches.append({
            "sheet_name": row["name"],
            "account_scope": scope,
            "monthly_budget": row["monthly_budget"],
            "mtd_spend": row["mtd_spend"],
            "last_paced": row["last_paced"],
            "row_index": row["row_index"],
            "matched_campaign_id": campaign.id if campaign else None,
            "matched_campaign_name": campaign.campaign_name if campaign else None,
            "match_type": _match_type_label(match_name, campaign),
        })

    bad_types = {"none", "account_scope_mismatch"}
    return jsonify({
        "sheet_tab": tab_name,
        "total_sheet_rows": len(sheet_rows),
        "matched": sum(1 for m in matches if m["match_type"] not in bad_types),
        "unmatched": sum(1 for m in matches if m["match_type"] in bad_types),
        "matches": matches,
    }), 200


def sync_budgets_for_account(account_id):
    """Pull monthly budgets (and ABO adset allocations) from the configured sheet.

    Single source of truth used by:
      - the manual "Sync Budgets" button (POST /api/sheets/<id>/sync-budgets)
      - /api/pacing/<id>/run, opportunistically before each pacing run
      - the daily background scheduler in app.py
      - account creation when a sheet ID is configured up-front

    For each matched campaign:
      * Updates campaign.monthly_budget from col B.
      * For ABO campaigns, parses col F (Notes) for "Name - X%" patterns. If every
        chunk parses and they sum to ~100, applies them to the matched ad sets'
        allocation_pct. If notes don't conform (flight info etc.) the existing
        allocations are left untouched.

    Raises ValueError on misconfiguration (no sheet, missing tab, bad creds) so
    callers can decide whether to surface or swallow the error.
    """
    settings = AccountSettings.query.filter_by(account_id=account_id).first()
    if not settings or not (settings.google_sheet_id or "").strip():
        raise ValueError("Google Sheet not configured.")

    account = Account.query.get(account_id)
    if not account:
        raise ValueError("Account not found.")

    gc = _get_gspread_client()
    spreadsheet = _sheets_retry(gc.open_by_key, settings.google_sheet_id)
    ws, tab_name = _open_month_worksheet(spreadsheet)

    sheet_rows = _get_meta_section(ws)
    db_campaigns = Campaign.query.filter_by(account_id=account_id, is_active=True).all()
    # Used by prefix scoping — so rows like "Commit - Campaign" are only processed
    # for the Commit Agency account, not for Amara or other accounts whose campaigns
    # happen to contain the word "Commit".
    # Session 13 — shared workspace: prefix scoping considers every account
    # in the DB, not just the originally-linked user's accounts.
    all_user_accounts = Account.query.all()

    updated = []          # campaign budget changes
    allocations_updated = []  # adset allocation changes
    skipped = []

    # ------------------------------------------------------------------
    # Two-pass matching: best-score-wins per campaign.
    #
    # Problem this solves: when the master sheet has rows from multiple
    # clients and a row like "Peter Piper Pizza Boosting" has no
    # "AccountName - " prefix, it passes the prefix filter for every
    # account. If another row ("Hawkeye Electric - Boosting" → stripped
    # to "Boosting") also matches the same campaign, the last row wins
    # — whichever appears later in the sheet stomps the correct budget.
    #
    # Solution: collect (sheet_row, match_name, campaign, score) for all
    # candidate rows, then for each campaign keep only the row with the
    # highest score. Ties stay with the first/higher-priority row.
    # ------------------------------------------------------------------
    candidate_rows = []  # list of (row, match_name, campaign, score)

    for row in sheet_rows:
        scope = row.get("account_scope") or ""
        if not _sheet_row_matches_account(scope, account):
            skipped.append({
                "sheet_name": row["name"],
                "reason": "Column D does not match this Budget Buddy account — row skipped",
            })
            continue
        if not _scope_is_explicit_match(scope, account):
            if not _row_prefix_matches_account(row["name"], account, all_user_accounts):
                skipped.append({
                    "sheet_name": row["name"],
                    "reason": "Row name prefix matches a different account — row skipped",
                })
                continue
        match_name = _strip_account_prefix(row["name"], account, all_user_accounts)
        campaign, score = _match_campaign_with_score(match_name, db_campaigns)
        if not campaign:
            skipped.append({"sheet_name": row["name"], "reason": "No matching DB campaign"})
            continue
        candidate_rows.append((row, match_name, campaign, score))

    # Keep only the best-scoring row per campaign.
    best_by_campaign = {}  # campaign.id → (row, match_name, campaign, score)
    for row, match_name, campaign, score in candidate_rows:
        prev = best_by_campaign.get(campaign.id)
        if prev is None or score > prev[3]:
            best_by_campaign[campaign.id] = (row, match_name, campaign, score)
        else:
            skipped.append({
                "sheet_name": row["name"],
                "reason": (
                    f"Outscored by '{prev[0]['name']}' for campaign "
                    f"'{campaign.campaign_name}' (score {prev[3]:.2f} vs {score:.2f})"
                ),
            })

    for row, match_name, campaign, _score in best_by_campaign.values():
        # Campaign-level budget
        if row["monthly_budget"] is not None:
            total_budget = row["monthly_budget"]

            # CBO budget split — notes like "70% to Weddings / 30% to Wedding Brochure"
            # distribute the row's total budget across multiple CBO campaigns by %.
            # Example: one sheet row covers two campaigns; notes define the split.
            cbo_split_applied = False
            if campaign.budget_mode == 'CBO':
                split_allocs = _parse_allocations_from_notes(row.get("notes", ""))
                if split_allocs:
                    split_proposed = []
                    split_ok = True
                    for alloc_name, alloc_pct in split_allocs:
                        matched_c = _match_campaign(alloc_name, db_campaigns)
                        if not matched_c:
                            split_ok = False
                            break
                        split_proposed.append((matched_c, alloc_pct, alloc_name))
                    # Reject if two chunks resolve to the same campaign
                    if split_ok:
                        seen_ids = set()
                        for c, _, _ in split_proposed:
                            if c.id in seen_ids:
                                split_ok = False
                                break
                            seen_ids.add(c.id)
                    if split_ok and split_proposed:
                        for c, pct, alloc_name in split_proposed:
                            new_budget = round(total_budget * pct / 100, 2)
                            old_budget = c.monthly_budget
                            if old_budget != new_budget:
                                c.monthly_budget = new_budget
                                updated.append({
                                    "campaign_name": c.campaign_name,
                                    "sheet_name": row["name"],
                                    "old_budget": old_budget,
                                    "new_budget": new_budget,
                                    "match_type": "cbo_split",
                                    "split_pct": pct,
                                })
                        cbo_split_applied = True

            if not cbo_split_applied:
                old_budget = campaign.monthly_budget
                if old_budget != total_budget:
                    campaign.monthly_budget = total_budget
                    updated.append({
                        "campaign_name": campaign.campaign_name,
                        "sheet_name": row["name"],
                        "old_budget": old_budget,
                        "new_budget": total_budget,
                        "match_type": _match_type_label(match_name, campaign),
                    })
        else:
            skipped.append({"sheet_name": row["name"], "reason": "No budget value in column B"})

        # ABO adset allocations — only attempt if this campaign is ABO and notes parse
        if campaign.budget_mode == 'ABO':
            allocations = _parse_allocations_from_notes(row.get("notes", ""))
            if allocations:
                active_adsets = [a for a in campaign.adsets if a.is_active]
                proposed = []  # [(adset, new_pct, parsed_name)]
                ok = True
                for parsed_name, parsed_pct in allocations:
                    matched = _match_adset(parsed_name, active_adsets)
                    if not matched:
                        ok = False
                        break
                    proposed.append((matched, parsed_pct, parsed_name))

                # Reject if duplicate adsets matched (two notes chunks → same adset)
                if ok:
                    seen_ids = set()
                    for ad, _, _ in proposed:
                        if ad.id in seen_ids:
                            ok = False
                            break
                        seen_ids.add(ad.id)

                if ok and proposed:
                    for ad, new_pct, parsed_name in proposed:
                        old_pct = ad.allocation_pct
                        if old_pct != new_pct:
                            ad.allocation_pct = new_pct
                            allocations_updated.append({
                                "campaign_name": campaign.campaign_name,
                                "adset_name": ad.adset_name,
                                "sheet_label": parsed_name,
                                "old_pct": round(old_pct, 2),
                                "new_pct": round(new_pct, 2),
                            })

    db.session.commit()

    return {
        "sheet_tab": tab_name,
        "updated_count": len(updated),
        "skipped_count": len(skipped),
        "allocations_updated_count": len(allocations_updated),
        "updated": updated,
        "skipped": skipped,
        "allocations_updated": allocations_updated,
    }


@sheets_bp.route("/<int:account_id>/sync-budgets", methods=["POST"])
@login_required
def sync_budgets(account_id):
    """
    Read monthly budgets from column B (and ABO allocations from col F notes) of
    the current month's tab and write them into the matched DB campaigns/adsets.
    Thin wrapper around sync_budgets_for_account.
    """
    if not _user_owns_account(account_id):
        return jsonify({"error": "Not found"}), 404

    try:
        result = sync_budgets_for_account(account_id)
    except (ValueError, RuntimeError) as e:
        return jsonify({"error": str(e)}), 400
    except Exception:
        logger.exception("Could not open Google Sheet for account %s", account_id)
        return jsonify({"error": "Could not open Google Sheet. Check the URL and that the service account has access."}), 400

    msg_parts = [f"Synced budgets for {result['updated_count']} campaign(s)"]
    if result["allocations_updated_count"]:
        msg_parts.append(f"and allocations for {result['allocations_updated_count']} ad set(s)")
    msg_parts.append(f"from '{result['sheet_tab']}'")

    return jsonify({
        "message": " ".join(msg_parts),
        **result,
    }), 200


def _campaign_mtd_spend(campaign):
    """Return the MTD spend for a campaign using direct DB queries.

    Uses explicit PacingData queries (not the ORM relationship) so that values
    committed earlier in the same request are always visible — ORM identity-map
    caching can mask newly-written rows when this is called right after a commit.

    - CBO: most recent campaign-level row (adset_id IS NULL).
    - ABO: sum of the highest-id row per active ad set on the latest date, so the
           sheet shows the full campaign total instead of a single ad set's spend.
    """
    if campaign.budget_mode == 'ABO':
        # Use the ORM relationship for ad set IDs (stable; not affected by the run).
        active_adset_ids = [a.id for a in campaign.adsets if a.is_active]
        if not active_adset_ids:
            return None
        rows = (
            PacingData.query
            .filter(
                PacingData.campaign_id == campaign.id,
                PacingData.adset_id.in_(active_adset_ids),
            )
            .order_by(PacingData.date.desc(), PacingData.id.desc())
            .all()
        )
        if not rows:
            return None
        last_date = rows[0].date
        # Keep only the highest-id (most recently written) row per adset on the
        # latest date so multiple same-day runs don't double-count.
        latest_per_adset = {}
        for p in rows:
            if p.date != last_date:
                break
            if p.adset_id not in latest_per_adset:
                latest_per_adset[p.adset_id] = p
        return sum(p.actual_spend or 0 for p in latest_per_adset.values())

    # CBO: most recent campaign-level row
    row = (
        PacingData.query
        .filter_by(campaign_id=campaign.id, adset_id=None)
        .order_by(PacingData.date.desc(), PacingData.id.desc())
        .first()
    )
    return row.actual_spend if row else None


def write_spend_for_account(account_id):
    """Push MTD spend + today's date into the configured sheet for one account.

    Single source of truth used by:
      - the manual "Write Spend to Sheet" button (POST /api/sheets/<id>/write-spend)
      - /api/pacing/<id>/run, opportunistically after a successful run
      - the daily background scheduler in app.py

    Returns a result dict (same shape across all callers). Raises ValueError when
    something is misconfigured (e.g. sheet not set, tab not found, credentials bad)
    so callers can decide whether to surface the error or swallow it.
    """
    settings = AccountSettings.query.filter_by(account_id=account_id).first()
    if not settings or not (settings.google_sheet_id or "").strip():
        raise ValueError("Google Sheet not configured.")

    account = Account.query.get(account_id)
    if not account:
        raise ValueError("Account not found.")

    gc = _get_gspread_client()
    spreadsheet = _sheets_retry(gc.open_by_key, settings.google_sheet_id)
    ws, tab_name = _open_month_worksheet(spreadsheet)

    sheet_rows = _get_meta_section(ws)
    db_campaigns = Campaign.query.filter_by(account_id=account_id, is_active=True).all()
    # Session 13 — shared workspace: prefix scoping considers every account
    # in the DB, not just the originally-linked user's accounts.
    all_user_accounts = Account.query.all()

    # %-m / %-d are Linux/macOS specific. Build portably for Windows local dev too.
    now = datetime.utcnow()
    today_str = f"{now.month}/{now.day}/{now.year}"
    cell_updates = []
    written = []
    skipped = []

    for row in sheet_rows:
        scope = row.get("account_scope") or ""
        if not _sheet_row_matches_account(scope, account):
            skipped.append({
                "sheet_name": row["name"],
                "reason": "Column D does not match this Budget Buddy account — row skipped",
            })
            continue
        # When col D is a real text scope that explicitly names this account, trust it
        # and skip the prefix check.  See _scope_is_explicit_match for rationale.
        if not _scope_is_explicit_match(scope, account):
            if not _row_prefix_matches_account(row["name"], account, all_user_accounts):
                skipped.append({
                    "sheet_name": row["name"],
                    "reason": "Row name prefix matches a different account — row skipped",
                })
                continue
        match_name = _strip_account_prefix(row["name"], account, all_user_accounts)
        matched_campaigns = _match_all_campaigns(match_name, db_campaigns)
        if not matched_campaigns:
            skipped.append({"sheet_name": row["name"], "reason": "No matching DB campaign"})
            continue

        # Collect spend from all matched campaigns and sum them so that one
        # sheet row can represent multiple Meta campaigns (e.g. "Weddings" row
        # = "Weddings - Traffic" + "Wedding Brochure").
        total_spend = 0.0
        spends_found = 0
        campaign_names = []
        for campaign in matched_campaigns:
            spend_value = _campaign_mtd_spend(campaign)
            if spend_value is not None:
                total_spend += spend_value
                spends_found += 1
                campaign_names.append(campaign.campaign_name)

        if spends_found == 0:
            skipped.append({"sheet_name": row["name"], "reason": "No pacing data available yet — run pacing first"})
            continue

        mtd_spend = round(total_spend, 2)
        r = row["row_index"]
        # Col C = MTD spend, Col G = Last Paced date
        cell_updates.append({"range": f"C{r}", "values": [[mtd_spend]]})
        cell_updates.append({"range": f"G{r}", "values": [[today_str]]})
        written.append({
            "campaign_name": " + ".join(campaign_names),
            "sheet_name": row["name"],
            "mtd_spend": mtd_spend,
            "last_paced": today_str,
            "row_index": r,
            "match_type": _match_type_label(match_name, matched_campaigns[0]),
        })

    if cell_updates:
        # USER_ENTERED lets the Sheets API parse numeric strings correctly and
        # avoids RAW-mode quirks where Google Sheets can misinterpret the value.
        _sheets_retry(ws.batch_update, cell_updates, value_input_option="USER_ENTERED")

        # Stamp column-C spend cells with an explicit 2-decimal number format so
        # that existing integer-formatted cells don't truncate e.g. 80.41 → 80.
        if written:
            format_requests = [
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": ws.id,
                            "startRowIndex": w["row_index"] - 1,  # 0-based
                            "endRowIndex": w["row_index"],
                            "startColumnIndex": 2,  # column C (0-based)
                            "endColumnIndex": 3,
                        },
                        "cell": {
                            "userEnteredFormat": {
                                "numberFormat": {
                                    "type": "NUMBER",
                                    "pattern": "0.00",
                                }
                            }
                        },
                        "fields": "userEnteredFormat.numberFormat",
                    }
                }
                for w in written
            ]
            try:
                ws.spreadsheet.batch_update({"requests": format_requests})
            except Exception as fmt_err:
                # Formatting failure is non-fatal — the values are already written.
                logger.warning("Could not apply number format to spend cells: %s", fmt_err)

    return {
        "sheet_tab": tab_name,
        "written_count": len(written),
        "skipped_count": len(skipped),
        "written": written,
        "skipped": skipped,
    }


@sheets_bp.route("/<int:account_id>/write-spend", methods=["POST"])
@login_required
def write_spend(account_id):
    """Manual write-back endpoint. Wraps write_spend_for_account with HTTP error handling."""
    if not _user_owns_account(account_id):
        return jsonify({"error": "Not found"}), 404

    try:
        result = write_spend_for_account(account_id)
    except (ValueError, RuntimeError) as e:
        return jsonify({"error": str(e)}), 400
    except Exception:
        logger.exception("write_spend_for_account failed for account %s", account_id)
        return jsonify({"error": "Could not write to Google Sheet. See server logs for details."}), 400

    return jsonify({
        "message": f"Wrote spend for {result['written_count']} campaign(s) to '{result['sheet_tab']}'",
        **result,
    }), 200
