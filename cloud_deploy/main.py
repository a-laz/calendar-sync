import os
import json
import datetime
from datetime import timedelta, timezone
from dateutil import parser as dtparse
from google.oauth2 import service_account
from googleapiclient.discovery import build
from caldav import DAVClient
import vobject
import hashlib
import uuid

# Configuration
GOOGLE_CALENDAR_ID = os.getenv('GOOGLE_CALENDAR_ID', 'primary')
ICLOUD_USERNAME = os.getenv('ICLOUD_USERNAME')
ICLOUD_PASSWORD = os.getenv('ICLOUD_PASSWORD')
SYNC_DIRECTION = os.getenv('SYNC_DIRECTION', 'two_way')
DRY_RUN = os.getenv('DRY_RUN', 'false').lower() == 'true'
WINDOW_PAST_DAYS = int(os.getenv('WINDOW_PAST_DAYS', '90'))
WINDOW_FUTURE_DAYS = int(os.getenv('WINDOW_FUTURE_DAYS', '365'))

# Google Calendar API scopes
SCOPES = ['https://www.googleapis.com/auth/calendar']

def get_google_service():
    """Get Google Calendar service using service account or default credentials"""
    try:
        # Try to use service account credentials if available
        if os.path.exists('service-account-key.json'):
            credentials = service_account.Credentials.from_service_account_file(
                'service-account-key.json', scopes=SCOPES)
            return build('calendar', 'v3', credentials=credentials)
        else:
            # Use default credentials (for Cloud Functions)
            from google.auth import default
            credentials, _ = default(scopes=SCOPES)
            return build('calendar', 'v3', credentials=credentials)
    except Exception as e:
        print(f"Error getting Google service: {e}")
        return None

def get_icloud_calendar():
    """Get iCloud calendar client"""
    try:
        client = DAVClient('https://caldav.icloud.com/', username=ICLOUD_USERNAME, password=ICLOUD_PASSWORD)
        principal = client.principal()
        calendars = principal.calendars()
        
        if not calendars:
            print("No calendars found in iCloud")
            return None
            
        # Use the first calendar
        return calendars[0]
    except Exception as e:
        print(f"Error connecting to iCloud: {e}")
        return None

def _iso(dt_str):
    """Parse ISO datetime string and return ISO format"""
    if not dt_str:
        return None
    try:
        dt = dtparse.isoparse(dt_str)
        return dt.isoformat()
    except:
        return dt_str

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

def parse_ics_event(ics_text):
    """Parse iCalendar text into a dictionary"""
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
        start = ve.dtstart.value.isoformat()
        end   = ve.dtend.value.isoformat() if hasattr(ve, "dtend") else start
    # dtstamp/last-modified best-effort
    lm = None
    if hasattr(ve, "last_modified"):
        lm = ve.last_modified.value.isoformat()
    elif hasattr(ve, "dtstamp"):
        lm = ve.dtstamp.value.isoformat()
    return {
        "uid": uid,
        "summary": summary,
        "start": start,
        "end": end,
        "all_day": is_all_day,
        "lastmod": lm
    }

def _icloud_time_block(ev):
    """Return (start_iso, end_iso, is_all_day) normalized."""
    start = ev.get("start")
    end   = ev.get("end")
    
    if not start:
        return (None, None, False)
    
    # Handle all-day events
    if ev.get("all_day", False):
        return (start, end, True)
    
    # Handle datetime events
    return (start, end, False)

def google_upsert_from_icloud(service, cal_id, ic_ev):
    """Create/update Google Calendar event from iCloud event"""
    uid     = ic_ev.get("uid")
    if not uid:
        print(f"Skipping iCloud event without UID: {ic_ev.get('summary', 'No Title')}")
        return
    summary = ic_ev["summary"]
    start_iso, end_iso, is_all_day = _icloud_time_block(ic_ev)
    
    if not start_iso:
        print(f"Skipping iCloud event without start time: {summary}")
        return
    
    # Build Google Calendar event
    g_ev = {
        "summary": summary,
        "iCalUID": uid,
    }
    
    if is_all_day:
        g_ev["start"] = {"date": start_iso}
        g_ev["end"] = {"date": end_iso}
    else:
        g_ev["start"] = {"dateTime": start_iso}
        g_ev["end"] = {"dateTime": end_iso}
    
    # Add description if available
    if "description" in ic_ev:
        g_ev["description"] = ic_ev["description"]
    
    # Add location if available
    if "location" in ic_ev:
        g_ev["location"] = ic_ev["location"]
    
    try:
        # Check if event already exists
        existing = service.events().list(
            calendarId=cal_id,
            iCalUID=uid,
            singleEvents=True
        ).execute()
        
        if existing.get("items"):
            # Update existing event
            event_id = existing["items"][0]["id"]
            if not DRY_RUN:
                service.events().update(
                    calendarId=cal_id,
                    eventId=event_id,
                    body=g_ev
                ).execute()
            print(f"Updated Google event: {summary}")
        else:
            # Create new event
            if not DRY_RUN:
                service.events().insert(
                    calendarId=cal_id,
                    body=g_ev
                ).execute()
            print(f"Created Google event: {summary}")
            
    except Exception as e:
        print(f"Error upserting Google event {summary}: {e}")

def icloud_upsert_from_google(calendar, g_ev):
    """Create/update iCloud calendar event from Google event"""
    uid = g_ev.get("iCalUID")
    if not uid:
        # Generate a consistent UID based on Google event ID
        google_id = g_ev.get('id')
        if google_id:
            uid = f"google-{google_id}"
        else:
            # Fallback to a hash of the event details for consistency
            event_key = f"{g_ev.get('summary', '')}-{g_ev.get('start', {}).get('dateTime', g_ev.get('start', {}).get('date', ''))}"
            uid = f"google-{hashlib.md5(event_key.encode()).hexdigest()[:8]}"
        print(f"Generated UID for Google event: {g_ev.get('summary', 'No Title')} -> {uid}")
    
    summary = g_ev["summary"]
    start_iso, end_iso, is_all_day = _google_time_block(g_ev)
    
    if not start_iso:
        print(f"Skipping Google event without start time: {summary}")
        return
    
    # Build iCalendar event as raw text (like original code)
    if is_all_day:
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
    
    summary_clean = summary.replace("\n", " ").replace("\r", " ")
    description = g_ev.get("description", "").replace("\n", " ").replace("\r", " ") if "description" in g_ev else ""
    location = g_ev.get("location", "").replace("\n", " ").replace("\r", " ") if "location" in g_ev else ""
    
    ics = f"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//GCal↔︎iCloud Sync//EN
BEGIN:VEVENT
UID:{uid}
SUMMARY:{summary_clean}
{dtstart}
{dtend}
{f"DESCRIPTION:{description}" if description else ""}
{f"LOCATION:{location}" if location else ""}
END:VEVENT
END:VCALENDAR"""
    
    try:
        # Check if event already exists
        existing_events = calendar.search(uid=uid)
        
        if existing_events:
            # Update existing event
            if not DRY_RUN:
                existing_events[0].data = ics
                existing_events[0].save()
            print(f"Updated iCloud event: {summary}")
        else:
            # Create new event
            if not DRY_RUN:
                calendar.save_event(ics)
            print(f"Created iCloud event: {summary}")
            
    except Exception as e:
        print(f"Error upserting iCloud event {summary}: {e}")

def main(request=None):
    """Cloud Function entry point"""
    print("Starting calendar sync...")
    
    # Get services
    service = get_google_service()
    if not service:
        print("Failed to get Google Calendar service")
        return {"status": "error", "message": "Google service unavailable"}
    
    calendar = get_icloud_calendar()
    if not calendar:
        print("Failed to get iCloud calendar")
        return {"status": "error", "message": "iCloud service unavailable"}
    
    # Calculate time window
    now = datetime.datetime.now(timezone.utc)
    time_min = now - timedelta(days=WINDOW_PAST_DAYS)
    time_max = now + timedelta(days=WINDOW_FUTURE_DAYS)
    
    print(f"Syncing events from {time_min.isoformat()} to {time_max.isoformat()}")
    
    try:
        # Get Google Calendar events
        print("Fetching Google Calendar events...")
        g_events = service.events().list(
            calendarId=GOOGLE_CALENDAR_ID,
            timeMin=time_min.isoformat(),
            timeMax=time_max.isoformat(),
            singleEvents=True,
            orderBy='startTime'
        ).execute()
        
        g_events_list = g_events.get('items', [])
        print(f"Found {len(g_events_list)} Google Calendar events")
        
        # Get iCloud events
        print("Fetching iCloud events...")
        ic_events_raw = calendar.search(
            start=time_min,
            end=time_max,
            event=True,
            expand=True
        )
        
        # Parse iCloud events into dictionaries
        ic_events = []
        for ev in ic_events_raw:
            try:
                parsed = parse_ics_event(ev.data)
                ic_events.append(parsed)
            except Exception as e:
                print(f"Skipping malformed iCloud event: {e}")
                continue
        
        print(f"Found {len(ic_events)} iCloud events")
        
        # Create lookup dictionaries
        g_by_uid = {}
        ic_by_uid = {}
        
        for g_ev in g_events_list:
            uid = g_ev.get("iCalUID") or f"google-{g_ev.get('id', 'unknown')}"
            g_by_uid[uid] = g_ev
        
        for ic_ev in ic_events:
            uid = ic_ev.get("uid")
            if uid:
                ic_by_uid[uid] = ic_ev
        
        # Sync Google to iCloud
        if SYNC_DIRECTION in ("two_way", "google_to_icloud"):
            print("Syncing Google Calendar events to iCloud...")
            for uid, g_ev in g_by_uid.items():
                if uid not in ic_by_uid:
                    icloud_upsert_from_google(calendar, g_ev)
        
        # Sync iCloud to Google
        if SYNC_DIRECTION in ("two_way", "icloud_to_google"):
            print("Syncing iCloud events to Google Calendar...")
            for uid, ic_ev in ic_by_uid.items():
                if uid not in g_by_uid:
                    google_upsert_from_icloud(service, GOOGLE_CALENDAR_ID, ic_ev)
        
        print("Calendar sync completed successfully!")
        return {"status": "success", "message": "Sync completed"}
        
    except Exception as e:
        print(f"Error during sync: {e}")
        return {"status": "error", "message": str(e)}

if __name__ == "__main__":
    result = main()
    print(json.dumps(result, indent=2))
