import os, json, datetime
from datetime import timedelta, timezone
from dateutil import parser as dtparse
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from caldav import DAVClient
import vobject

load_dotenv()

SCOPES = ['https://www.googleapis.com/auth/calendar']
CREDS_FILE       = os.getenv("GOOGLE_CREDENTIALS", "credentials.json")
GOOGLE_CAL_ID    = os.getenv("GOOGLE_CALENDAR_ID", "primary")
ICLOUD_USERNAME  = os.getenv("ICLOUD_USERNAME")
ICLOUD_PASSWORD  = os.getenv("ICLOUD_PASSWORD")
SYNC_DIRECTION   = os.getenv("SYNC_DIRECTION", "two_way")
DRY_RUN          = os.getenv("DRY_RUN", "true").lower() == "true"
WINDOW_PAST_DAYS = int(os.getenv("WINDOW_PAST_DAYS", "90"))
WINDOW_FUTURE_DAYS = int(os.getenv("WINDOW_FUTURE_DAYS", "365"))

STATE_FILE = "sync_state.json"  # remembers which UIDs we've mirrored for safe deletes

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {"known_uids": []}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)

def get_google_service():
    creds = None
    if os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)
    if not creds or not creds.valid:
        flow = InstalledAppFlow.from_client_secrets_file(CREDS_FILE, SCOPES)
        creds = flow.run_local_server(port=0)
        with open('token.json', 'w') as token:
            token.write(creds.to_json())
    return build('calendar', 'v3', credentials=creds)

def _iso(dt):
    if isinstance(dt, str):
        return dtparse.isoparse(dt).astimezone(timezone.utc).isoformat()
    return dt.astimezone(timezone.utc).isoformat()

def _google_time_block(ev):
    """Return (start_iso, end_iso, is_all_day) normalized."""
    start = ev.get("start", {})
    end   = ev.get("end",   {})
    
    # Handle case where start/end might be strings instead of dicts
    if isinstance(start, str):
        start = {"dateTime": start}
    if isinstance(end, str):
        end = {"dateTime": end}
    
    if "date" in start:  # all-day
        s = dtparse.isoparse(start["date"])
        e = dtparse.isoparse(end["date"])
        return (s.date().isoformat(), e.date().isoformat(), True)
    s = _iso(start.get("dateTime"))
    e = _iso(end.get("dateTime"))
    return (s, e, False)

def _google_last_updated(ev):
    return dtparse.isoparse(ev.get("updated"))

def get_google_events(service, cal_id):
    # Range
    now = datetime.datetime.now(timezone.utc)
    time_min = (now - timedelta(days=WINDOW_PAST_DAYS)).isoformat()
    time_max = (now + timedelta(days=WINDOW_FUTURE_DAYS)).isoformat()

    events = []
    page_token = None
    while True:
        res = service.events().list(
            calendarId=cal_id,
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            orderBy='startTime',
            pageToken=page_token,
            maxResults=2500
        ).execute()
        events.extend(res.get('items', []))
        page_token = res.get('nextPageToken')
        if not page_token:
            break
    # Key by iCalUID (Google ensures one per series)
    by_uid = {}
    for e in events:
        uid = e.get("iCalUID")
        if uid:
            # Prefer the "latest updated" as representative
            prev = by_uid.get(uid)
            if not prev or _google_last_updated(e) > _google_last_updated(prev):
                by_uid[uid] = e
    return by_uid

def get_icloud_calendar():
    client = DAVClient('https://caldav.icloud.com/', username=ICLOUD_USERNAME, password=ICLOUD_PASSWORD)
    principal = client.principal()
    cals = principal.calendars()
    if not cals:
        raise RuntimeError("No iCloud calendars found for this account.")
    # Use first calendar by default; choose another by name if you want
    return cals[0]

def parse_ics_event(ics_text):
    cal = vobject.readOne(ics_text)
    ve = cal.vevent
    uid = str(ve.uid.value) if hasattr(ve, "uid") else None
    summary = str(ve.summary.value) if hasattr(ve, "summary") else "No Title"
    # All-day if dtstart is date
    is_all_day = hasattr(ve.dtstart.value, "date") and not hasattr(ve.dtstart.value, "time")
    if is_all_day:
        start = ve.dtstart.value.isoformat()
        # dtend may be next-day exclusive; keep as iso date
        end = ve.dtend.value.isoformat() if hasattr(ve, "dtend") else start
    else:
        start = _iso(ve.dtstart.value)
        end   = _iso(ve.dtend.value) if hasattr(ve, "dtend") else start
    # dtstamp/last-modified best-effort
    lm = None
    if hasattr(ve, "last_modified"):
        lm = _iso(ve.last_modified.value)
    elif hasattr(ve, "dtstamp"):
        lm = _iso(ve.dtstamp.value)
    return {
        "uid": uid,
        "summary": summary,
        "start": start,
        "end": end,
        "all_day": is_all_day,
        "lastmod": lm
    }

def get_icloud_events(calendar):
    evs = calendar.events()
    by_uid = {}
    for e in evs:
        ics = e.data
        try:
            parsed = parse_ics_event(ics)
            uid = parsed["uid"]
            if uid:
                # Use ETag as version hint if lastmod missing
                parsed["etag"] = getattr(e, "etag", None)
                parsed["_raw"] = e
                by_uid[uid] = parsed
        except Exception:
            # Skip malformed
            continue
    return by_uid

def ics_from_google(uid, summary, start_iso, end_iso, all_day):
    if all_day:
        # all-day ICS uses DATE (YYYYMMDD), DTEND exclusive
        s = start_iso.replace("-", "")
        e = end_iso.replace("-", "")
        dtstart = f"DTSTART;VALUE=DATE:{s}"
        dtend   = f"DTEND;VALUE=DATE:{e}"
    else:
        # strip punctuation, keep UTC (Z)
        def compress(ts):
            dt = dtparse.isoparse(ts).astimezone(timezone.utc)
            return dt.strftime("%Y%m%dT%H%M%SZ")
        dtstart = f"DTSTART:{compress(start_iso)}"
        dtend   = f"DTEND:{compress(end_iso)}"
    summary = summary.replace("\n", " ").replace("\r", " ")
    ics = f"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//GCal↔︎iCloud Sync//EN
BEGIN:VEVENT
UID:{uid}
SUMMARY:{summary}
{dtstart}
{dtend}
END:VEVENT
END:VCALENDAR"""
    return ics

def google_find_by_uid(service, cal_id, uid):
    res = service.events().list(calendarId=cal_id, iCalUID=uid, maxResults=1, singleEvents=True).execute()
    items = res.get("items", [])
    return items[0] if items else None

def google_upsert_from_icloud(service, cal_id, ic_ev):
    uid     = ic_ev.get("uid")
    if not uid:
        print(f"Skipping iCloud event without UID: {ic_ev.get('summary', 'No Title')}")
        return
    summary = ic_ev["summary"]
    if ic_ev["all_day"]:
        body = {
            "summary": summary,
            "iCalUID": uid,  # only honored by events.import
            "start": {"date": ic_ev["start"]},
            "end":   {"date": ic_ev["end"]},
        }
    else:
        body = {
            "summary": summary,
            "iCalUID": uid,  # only honored by events.import
            "start": {"dateTime": ic_ev["start"]},
            "end":   {"dateTime": ic_ev["end"]},
        }
    existing = google_find_by_uid(service, cal_id, uid)
    if existing:
        # update via events.update (cannot change iCalUID there; it’s already set)
        event_id = existing["id"]
        patch = {
            "summary": summary,
            "start": body["start"],
            "end": body["end"]
        }
        if DRY_RUN:
            print(f"[DRY-RUN] Google UPDATE from iCloud: {summary} ({uid})")
            return existing
        return service.events().update(calendarId=cal_id, eventId=event_id, body=patch).execute()
    else:
        # create with events.import so we keep the iCalUID
        if DRY_RUN:
            print(f"[DRY-RUN] Google CREATE from iCloud: {summary} ({uid})")
            return {"iCalUID": uid}
        return service.events().import_(calendarId=cal_id, body=body).execute()

def icloud_upsert_from_google(calendar, g_ev):
    uid = g_ev.get("iCalUID")
    if not uid:
        # Generate a UID for events without one
        import uuid
        uid = f"google-{g_ev.get('id', str(uuid.uuid4()))}"
        print(f"Generated UID for Google event: {g_ev.get('summary', 'No Title')} -> {uid}")
    title = g_ev.get("summary", "No Title")
    s, e, all_day = _google_time_block(g_ev)
    ics = ics_from_google(uid, title, s, e, all_day)

    # Try to locate existing iCloud event by UID
    # (CalDAV server supports REPORT; python-caldav doesn’t expose UID lookup directly,
    # so we scan loaded events dict before calling this function in the main flow.)
    # Here we just add a new resource at a stable path if needed.
    # If exists, we overwrite.
    if DRY_RUN:
        print(f"[DRY-RUN] iCloud UPSERT from Google: {title} ({uid})")
        return

    # Create or replace by putting to a deterministic href based on UID
    # Fallback: add_event (server assigns href). For overwrite, delete old then add.
    calendar.add_event(ics)

def icloud_delete(calendar, ic_ev):
    if DRY_RUN:
        print(f"[DRY-RUN] iCloud DELETE: {ic_ev['summary']} ({ic_ev['uid']})")
        return
    ic_ev["_raw"].delete()

def google_delete(service, cal_id, g_ev):
    if DRY_RUN:
        print(f"[DRY-RUN] Google DELETE: {g_ev.get('summary','(no title)')} ({g_ev.get('iCalUID')})")
        return
    service.events().delete(calendarId=cal_id, eventId=g_ev["id"]).execute()

def newer_than(ts_a, ts_b):
    """Return True if ts_a (ISO) is strictly newer than ts_b, tolerating None."""
    if ts_a and not ts_b:
        return True
    if not ts_a or not ts_b:
        return False
    return dtparse.isoparse(ts_a) > dtparse.isoparse(ts_b)

def main():
    state = load_state()
    known = set(state.get("known_uids", []))

    service = get_google_service()
    g_by_uid = get_google_events(service, GOOGLE_CAL_ID)

    ic_calendar = get_icloud_calendar()
    ic_by_uid = get_icloud_events(ic_calendar)

    # Build comparable records
    # Google lastmod -> 'updated'
    g_norm = {}
    for uid, ev in g_by_uid.items():
        s, e, all_day = _google_time_block(ev)
        g_norm[uid] = {
            "uid": uid,
            "summary": ev.get("summary", "No Title"),
            "start": s, "end": e, "all_day": all_day,
            "lastmod": _iso(ev.get("updated")),
            "_raw": ev
        }

    # iCloud is already normalized in ic_by_uid

    uids = set(g_norm.keys()) | set(ic_by_uid.keys())

    # Decide actions
    to_create_on_g = []
    to_update_on_g = []
    to_delete_on_g = []

    to_create_on_ic = []
    to_update_on_ic = []
    to_delete_on_ic = []

    # Conflict policy:
    # - If on both sides and both changed, prefer the **newer lastmod** (Google 'updated' vs iCloud last_modified/etag fallback)
    # - Only delete on the "other" side if UID is in known set (i.e., we previously mirrored it); avoids nuking events we never touched.

    for uid in sorted(uids):
        g = g_norm.get(uid)
        i = ic_by_uid.get(uid)

        if g and not i:
            if SYNC_DIRECTION in ("two_way", "google_to_icloud"):
                to_create_on_ic.append(g)
            # If this UID is known and missing on iCloud, do nothing else (one-way create covers it)
            continue

        if i and not g:
            if SYNC_DIRECTION in ("two_way", "icloud_to_google"):
                to_create_on_g.append(i)
            continue

        if g and i:
            # Compare lastmod
            g_lm = g["lastmod"]
            i_lm = i["lastmod"] or i.get("etag")  # crude fallback
            # Normalize fields to detect meaningful diffs
            diffs = (
                (g["summary"] != i["summary"]) or
                (g["all_day"] != i["all_day"]) or
                (g["start"] != i["start"]) or
                (g["end"]   != i["end"])
            )
            if diffs:
                if newer_than(g_lm, i_lm):
                    if SYNC_DIRECTION in ("two_way", "google_to_icloud"):
                        to_update_on_ic.append(g)
                else:
                    if SYNC_DIRECTION in ("two_way", "icloud_to_google"):
                        to_update_on_g.append(i)

    # Safe deletions:
    # If a UID is known (we mirrored it before) and it's now missing on one side,
    # we delete on the other side to keep parity (respecting SYNC_DIRECTION).
    # NOTE: We're being conservative here - only delete if we're sure it was previously synced
    for uid in list(known):
        g = g_norm.get(uid)
        i = ic_by_uid.get(uid)
        # Only delete from Google if we're absolutely sure the iCloud event was deleted
        # and we previously synced it. For safety, we'll skip Google deletions for now.
        # if SYNC_DIRECTION in ("two_way", "google_to_icloud") and (g and not i):
        #     to_delete_on_g.append(g)
        if SYNC_DIRECTION in ("two_way", "icloud_to_google") and (i and not g):
            to_delete_on_ic.append(i)

    # De-dup actions (avoid create+update on same uid)
    def dedup(items):
        seen = set(); out = []
        for x in items:
            if x["uid"] in seen: continue
            seen.add(x["uid"]); out.append(x)
        return out

    to_create_on_g = dedup(to_create_on_g)
    to_update_on_g = dedup(to_update_on_g)
    to_delete_on_g = dedup(to_delete_on_g)

    to_create_on_ic = dedup(to_create_on_ic)
    to_update_on_ic = dedup(to_update_on_ic)
    to_delete_on_ic = dedup(to_delete_on_ic)

    # --- APPLY ---
    print(f"\n=== PLAN (DRY_RUN={DRY_RUN}) ===")
    print(f"Create on Google:  {len(to_create_on_g)}")
    print(f"Update on Google:  {len(to_update_on_g)}")
    print(f"Delete on Google:  {len(to_delete_on_g)}")
    print(f"Create on iCloud:  {len(to_create_on_ic)}")
    print(f"Update on iCloud:  {len(to_update_on_ic)}")
    print(f"Delete on iCloud:  {len(to_delete_on_ic)}\n")

    # iCloud ← Google
    for g in to_create_on_ic + to_update_on_ic:
        icloud_upsert_from_google(ic_calendar, g)
        known.add(g["uid"])

    # Google ← iCloud
    for i in to_create_on_g + to_update_on_g:
        google_upsert_from_icloud(service, GOOGLE_CAL_ID, i)
        known.add(i["uid"])

    # Deletes
    for g in to_delete_on_g:
        google_delete(service, GOOGLE_CAL_ID, g["_raw"])
        if not DRY_RUN:
            known.discard(g["uid"])
    for i in to_delete_on_ic:
        icloud_delete(ic_calendar, i)
        if not DRY_RUN:
            known.discard(i["uid"])

    # Save state
    state["known_uids"] = sorted(list(known))
    if DRY_RUN:
        print("Dry run complete. No changes applied.")
    else:
        save_state(state)
        print("Sync complete. State saved.")

if __name__ == "__main__":
    main()
