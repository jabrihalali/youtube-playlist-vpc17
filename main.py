import io
import json
import os
import random
import time
import urllib.request
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload

SHEET_ID = os.environ["SHEET_ID"]
FIXED_VIDEO_ID = os.environ.get("FIXED_VIDEO_ID", "TtLaH4OASWI").strip()
INTRO_VIDEO_ID = os.environ.get("INTRO_VIDEO_ID", "u5fkWFh9wAM").strip()
MAX_PER_RUN = int(os.environ.get("MAX_PER_RUN", "1"))
TOTAL = 15  # videos per playlist: #1 intro, #2 fixed, #3-15 related

# Sheet layout: A=Title, B=Description, C=Est. Monthly Searches, D=Status
STATUS_COL = 3  # 0-based index of the Status column (D)


def get_services():
    info = json.loads(os.environ["YT_TOKEN_JSON"])
    creds = Credentials(
        token=None,
        refresh_token=info["refresh_token"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=info["client_id"],
        client_secret=info["client_secret"],
        scopes=[
            "https://www.googleapis.com/auth/youtube.force-ssl",
            "https://www.googleapis.com/auth/spreadsheets",
        ],
    )
    creds.refresh(Request())
    yt = build("youtube", "v3", credentials=creds)
    gs = build("sheets", "v4", credentials=creds)
    return yt, gs


def api_call(fn, *args, **kwargs):
    """Run a YouTube/Sheets API call with retries.
    Returns (ok, result). On quota errors: ok=False so the caller stops and
    the row stays pending — the next run resumes the same title."""
    delay = 5
    for attempt in range(5):
        try:
            return True, fn(*args, **kwargs).execute()
        except HttpError as e:
            msg = str(e).lower()
            if "quota" in msg or "ratelimit" in msg or "userrate" in msg:
                print("Quota/rate error — stopping run, will resume next run:", e)
                return False, None
            if e.resp is not None and e.resp.status in (500, 502, 503, 504) and attempt < 4:
                print(f"Transient {e.resp.status}, retry in {delay}s...")
                time.sleep(delay)
                delay = min(delay * 2, 60)
                continue
            print("API error (not retryable):", e)
            return False, None
        except Exception as e:
            if attempt < 4:
                print(f"Network error {e}, retry in {delay}s...")
                time.sleep(delay)
                delay = min(delay * 2, 60)
                continue
            print("Network error (gave up):", e)
            return False, None
    return False, None


def search_video_ids(yt, query, max_results=25, exclude=()):
    """Search for a video matching the query; returns list of video IDs.
    NEVER raises: retries are attempts, failures just return []."""
    for attempt in range(5):
        try:
            resp = yt.search().list(
                part="snippet",
                q=query,
                type="video",
                maxResults=max_results,
                safeSearch="none",
            ).execute()
            ids = []
            for item in resp.get("items", []):
                vid = item["id"]["videoId"]
                if vid not in exclude and vid not in ids:
                    ids.append(vid)
            return ids
        except HttpError as e:
            msg = str(e).lower()
            if "quota" in msg or "ratelimit" in msg or "userrate" in msg:
                raise
            if e.resp is not None and e.resp.status in (500, 502, 503, 504) and attempt < 4:
                time.sleep(5 * (attempt + 1))
                continue
            print("Search failed:", e)
            return []
        except Exception as e:
            if attempt < 4:
                time.sleep(5 * (attempt + 1))
                continue
            print("Search failed:", e)
            return []
    return []


def upload_thumbnail(youtube, playlist_id, query):
    """Search a video for the playlist title, download its thumbnail and set
    it as the playlist thumbnail. Never raises."""
    try:
        ids = search_video_ids(youtube, query, max_results=1)
        if not ids:
            return
        for quality in ("maxresdefault", "hqdefault"):
            url = f"https://i.ytimg.com/vi/{ids[0]}/{quality}.jpg"
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                data = urllib.request.urlopen(req, timeout=30).read()
                if len(data) > 2000:
                    media = MediaIoBaseUpload(io.BytesIO(data), mimetype="image/jpeg")
                    youtube.thumbnails().set(playlistId=playlist_id, media_body=media).execute()
                    print(f"Thumbnail set for playlist {playlist_id} (from video {ids[0]})")
                    return
            except Exception as e:
                print("Thumbnail fetch failed:", e)
    except HttpError as e:
        if "quota" in str(e).lower():
            raise
        print("Thumbnail upload failed:", e)
    except Exception as e:
        print("Thumbnail upload failed:", e)


def find_my_playlist_by_title(yt, title):
    """Return (playlist_id, [videoIds in order]) for MY playlist with this
    exact title (the most complete one if several), or (None, [])."""
    best = (None, [])
    page = None
    while True:
        ok, resp = api_call(
            yt.playlists().list,
            part="snippet",
            mine=True,
            maxResults=50,
            pageToken=page,
        )
        if not ok or resp is None:
            return best
        for pl in resp.get("items", []):
            if pl["snippet"]["title"].strip() == title.strip():
                ok2, items = api_call(
                    yt.playlistItems().list,
                    part="contentDetails",
                    playlistId=pl["id"],
                    maxResults=50,
                )
                vids = []
                if ok2 and items:
                    vids = [it["contentDetails"]["videoId"] for it in items.get("items", [])]
                if len(vids) > len(best[1]):
                    best = (pl["id"], vids)
        page = resp.get("nextPageToken")
        if not page:
            return best


def add_videos(yt, playlist_id, video_ids, start_pos):
    """Insert videos at consecutive positions. Returns the position after the
    last successfully added video, or -1 on quota stop."""
    pos = start_pos
    for vid in video_ids:
        ok, _ = api_call(
            yt.playlistItems().insert,
            part="snippet",
            body={
                "snippet": {
                    "playlistId": playlist_id,
                    "resourceId": {"kind": "youtube#video", "videoId": vid},
                    "position": pos,
                }
            },
        )
        if not ok:
