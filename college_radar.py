from apify_client import ApifyClient
from datetime import datetime
from pathlib import Path
import base64
import json
import pandas as pd
import requests
import streamlit as st

APIFY_TOKEN = st.secrets["APIFY_TOKEN"]
SPOTIFY_CLIENT_ID = st.secrets["SPOTIFY_CLIENT_ID"]
SPOTIFY_CLIENT_SECRET = st.secrets["SPOTIFY_CLIENT_SECRET"]

client = ApifyClient(APIFY_TOKEN)
RESULTS_FILE = Path.home() / ".college_radar_results.json"

ALL_TARGET_TOWNS: dict[str, str | list[str]] = {
    "Madison": "WI", "Bloomington": "IN", "East Lansing": "MI", "Champaign": "IL",
    "Iowa City": "IA", "Lawrence": "KS", "Columbia": "MO", "Manhattan": "KS",
    "Lincoln": "NE", "Fayetteville": "AR", "Lexington": "KY", "Tuscaloosa": "AL",
    "Minneapolis": "MN", "Athens": "OH", "Oxford": ["OH", "MS"], "West Lafayette": "IN",
    "Kalamazoo": "MI", "Muncie": "IN", "Ames": "IA",
}

# Major routing hubs within driving distance of each college town.
# Artists playing these cities are already in the region — prime debut targets.
CITY_HUBS: dict[str, list[str]] = {
    "Madison":        ["Chicago", "Milwaukee", "Minneapolis"],
    "Bloomington":    ["Indianapolis", "Chicago"],
    "East Lansing":   ["Detroit", "Chicago", "Grand Rapids"],
    "Champaign":      ["Chicago", "St. Louis", "Indianapolis"],
    "Iowa City":      ["Chicago", "Des Moines", "Minneapolis"],
    "Lawrence":       ["Kansas City"],
    "Columbia":       ["St. Louis", "Kansas City"],
    "Manhattan":      ["Kansas City", "Wichita"],
    "Lincoln":        ["Omaha", "Kansas City"],
    "Fayetteville":   ["Kansas City", "Little Rock", "Tulsa"],
    "Lexington":      ["Cincinnati", "Louisville", "Nashville"],
    "Tuscaloosa":     ["Birmingham", "Nashville", "Atlanta"],
    "Minneapolis":    ["Chicago", "Milwaukee"],
    "Athens":         ["Columbus", "Pittsburgh", "Cleveland"],
    "Oxford":         ["Columbus", "Cleveland", "Detroit"],
    "West Lafayette": ["Indianapolis", "Chicago"],
    "Kalamazoo":      ["Detroit", "Chicago", "Grand Rapids"],
    "Muncie":         ["Indianapolis", "Cincinnati"],
    "Ames":           ["Des Moines", "Minneapolis", "Chicago"],
}

# Spotify genre seed batches for the recommendations endpoint
GENRE_SEED_BATCHES = [
    ["electronic", "house", "techno"],
    ["dance", "edm", "deep-house"],
    ["drum-and-bass", "dubstep", "ambient"],
    ["minimal-techno", "chicago-house", "uk-garage"],
    ["downtempo", "trip-hop", "electronica"],
]


# ── SPOTIFY ───────────────────────────────────────────────────────────────────

@st.cache_data(ttl=3600)
def get_spotify_token() -> str:
    creds = base64.b64encode(f"{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}".encode()).decode()
    r = requests.post(
        "https://accounts.spotify.com/api/token",
        headers={"Authorization": f"Basic {creds}", "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "client_credentials"},
        timeout=10,
    )
    return r.json().get("access_token", "")


def batch_fetch_artists(artist_ids: list[str], token: str) -> list[dict]:
    artists = []
    headers = {"Authorization": f"Bearer {token}"}
    for i in range(0, len(artist_ids), 50):
        r = requests.get(
            "https://api.spotify.com/v1/artists",
            headers=headers,
            params={"ids": ",".join(artist_ids[i:i+50])},
            timeout=10,
        )
        artists.extend(a for a in r.json().get("artists", []) if a)
    return artists


def discover_emerging_artists(token: str, max_popularity: int, target: int) -> list[dict]:
    """
    Query Spotify's recommendations endpoint across multiple electronic genre seeds.
    Returns artists filtered to max_popularity — genuinely underground acts.
    """
    headers = {"Authorization": f"Bearer {token}"}
    seen_ids: set[str] = set()
    artist_ids: list[str] = []
    status = st.sidebar.empty()

    for seeds in GENRE_SEED_BATCHES:
        if len(artist_ids) >= target * 3:
            break
        try:
            status.caption(f"Querying Spotify: {', '.join(seeds)}...")
            r = requests.get(
                "https://api.spotify.com/v1/recommendations",
                headers=headers,
                params={
                    "seed_genres": ",".join(seeds),
                    "limit": 100,
                    "max_popularity": max_popularity,
                    "market": "US",
                },
                timeout=10,
            )
            for track in r.json().get("tracks", []):
                for a in track.get("artists", []):
                    if a.get("id") and a["id"] not in seen_ids:
                        seen_ids.add(a["id"])
                        artist_ids.append(a["id"])
        except Exception as e:
            st.warning(f"Spotify error ({seeds}): {e}")

    status.empty()

    # Batch-fetch full artist details and apply popularity + follower filters
    raw = batch_fetch_artists(artist_ids, token)
    seen_names: set[str] = set()
    result = []
    for a in raw:
        if a["name"] in seen_names:
            continue
        if a.get("popularity", 100) > max_popularity:
            continue
        if a.get("followers", {}).get("total", 0) < 500:
            continue
        seen_names.add(a["name"])
        result.append({
            "name": a["name"],
            "popularity": a["popularity"],
            "followers": a["followers"]["total"],
            "genres": ", ".join(a.get("genres", [])) or "—",
            "spotify_id": a["id"],
        })
        if len(result) >= target:
            break

    return result


# ── BANDSINTOWN ───────────────────────────────────────────────────────────────

def get_show_cities(artist_name: str, date_filter: str, cache: dict) -> set[str]:
    """Return set of cities where artist has shows. Cached per artist + date_filter."""
    key = f"{artist_name}::{date_filter}"
    if key not in cache:
        try:
            run = client.actor("solidcode/bandsintown-scraper").call(
                run_input={"artists": [artist_name], "queryType": "events", "dateFilter": date_filter}
            )
            cache[key] = {
                event.get("venue", {}).get("city", "")
                for event in client.dataset(run.default_dataset_id).iterate_items()
            }
        except Exception:
            cache[key] = set()
    return cache[key]


# ── PERSISTENCE ───────────────────────────────────────────────────────────────

def save_results(results: list[dict], meta: dict) -> None:
    RESULTS_FILE.write_text(json.dumps({"metadata": meta, "results": results}, indent=2))


def load_results() -> tuple[list[dict], dict]:
    if not RESULTS_FILE.exists():
        return [], {}
    try:
        data = json.loads(RESULTS_FILE.read_text())
        return data.get("results", []), data.get("metadata", {})
    except Exception:
        return [], {}


# ── PAGE ──────────────────────────────────────────────────────────────────────

st.set_page_config(page_title="College Debut Radar", layout="wide", page_icon="🎓")
st.title("🎓 College Town Debut Radar")
st.caption("Emerging electronic artists routing near your college markets — verified never played there.")

tab_dashboard, tab_run = st.tabs(["📊 Dashboard", "🔍 Run New Scan"])


# ── DASHBOARD ─────────────────────────────────────────────────────────────────

with tab_dashboard:
    saved_results, meta = load_results()
    if not saved_results:
        st.info("No data yet. Go to **Run New Scan** to generate your first report.")
    else:
        df_all = pd.DataFrame(saved_results)
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Opportunities", len(saved_results))
        c2.metric("Cities with Matches", df_all["City"].nunique())
        c3.metric("Unique Artists", df_all["Artist"].nunique())
        c4.metric("Artists Scanned", meta.get("candidates_scanned", "—"))
        st.caption(f"Last updated: **{meta.get('run_at', 'unknown')}**")
        st.divider()

        for city in sorted(df_all["City"].unique()):
            city_df = df_all[df_all["City"] == city].copy()
            state = "/".join(city_df["State"].unique())
            display = city_df.drop(columns=["City", "State"]).sort_values("Popularity Score")
            display["Followers"] = display["Followers"].apply(lambda x: f"{int(x):,}")
            with st.expander(f"📍 **{city}, {state}** — {len(city_df)} opportunit{'y' if len(city_df)==1 else 'ies'}", expanded=True):
                st.dataframe(display, use_container_width=True, hide_index=True)

        st.divider()
        st.download_button("⬇ Download CSV", df_all.to_csv(index=False), "debut_radar_results.csv", "text/csv")


# ── RUN NEW SCAN ──────────────────────────────────────────────────────────────

with tab_run:
    col_left, col_right = st.columns(2)

    with col_left:
        st.subheader("📍 Target Markets")
        city_labels = [
            f"{city}, {'/'.join(state) if isinstance(state, list) else state}"
            for city, state in ALL_TARGET_TOWNS.items()
        ]
        b1, b2 = st.columns(2)
        if b1.button("Select All", use_container_width=True):
            st.session_state["selected_cities"] = city_labels
        if b2.button("Clear All", use_container_width=True):
            st.session_state["selected_cities"] = []

        selected_labels = st.pills(
            "Select markets:", city_labels, selection_mode="multi",
            default=st.session_state.get("selected_cities", [])
        )
        TARGET_TOWNS = {
            city: state for city, state in ALL_TARGET_TOWNS.items()
            if any(lbl.startswith(city + ",") for lbl in (selected_labels or []))
        }
        st.caption(f"{len(TARGET_TOWNS)} markets selected.")

    with col_right:
        st.subheader("⚙️ Settings")
        candidate_pool = st.slider("Artist pool size", 20, 200, 100, step=10,
            help="How many artists to pull from Spotify's recommendations engine.")
        max_popularity = st.slider("Max Spotify popularity score", 10, 60, 40, step=5,
            help="0–100. Artists below this are emerging/underground. 40 = solidly underground, 60 = rising indie.")
        st.caption(
            f"**Popularity ≤ {max_popularity}** — underground to emerging.  \n"
            "**Hub city logic** — flags artists with upcoming shows near each college town who have never played there."
        )

    st.divider()

    if not TARGET_TOWNS:
        st.warning("Select at least one market to continue.")
    else:
        run_clicked = st.button("▶ Run Analysis", type="primary", use_container_width=True)

        if run_clicked:
            token = get_spotify_token()
            if not token:
                st.error("Spotify authentication failed — check SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET in secrets.")
                st.stop()

            # Step 1: Discover artists
            with st.status("Step 1 — Finding emerging electronic artists via Spotify...", expanded=False) as s:
                candidates = discover_emerging_artists(token, max_popularity=max_popularity, target=candidate_pool)
                s.update(label=f"Step 1 complete — {len(candidates)} artists found (popularity ≤ {max_popularity}).", state="complete")

            if not candidates:
                st.error("No artists found. Try raising the max popularity score.")
                st.stop()

            # Step 2: Per-city routing + debut check
            st.subheader("📍 Live Results by City")
            bit_cache: dict = {}
            all_results: list[dict] = []
            city_slots = {city: st.empty() for city in sorted(TARGET_TOWNS.keys())}
            progress = st.progress(0, text="Starting...")
            total = len(TARGET_TOWNS) * len(candidates)

            for c_idx, city in enumerate(sorted(TARGET_TOWNS.keys())):
                state = TARGET_TOWNS[city]
                states = state if isinstance(state, list) else [state]
                state_str = "/".join(states)
                hubs = CITY_HUBS.get(city, [])
                city_results = []

                for a_idx, artist in enumerate(candidates):
                    pct = int(((c_idx * len(candidates) + a_idx) / total) * 100)
                    progress.progress(pct, text=f"{city} — {artist['name']} ({a_idx+1}/{len(candidates)})")

                    # Gate 1: artist must have upcoming shows in a hub near this city
                    upcoming = get_show_cities(artist["name"], "upcoming", bit_cache)
                    routing_hubs = [h for h in hubs if h in upcoming]
                    if not routing_hubs:
                        continue

                    # Gate 2: artist must never have played the college town itself
                    past = get_show_cities(artist["name"], "past", bit_cache)
                    if city in past:
                        continue

                    for s in states:
                        city_results.append({
                            "Artist": artist["name"],
                            "City": city,
                            "State": s,
                            "Routing Through": ", ".join(routing_hubs),
                            "Popularity Score": artist["popularity"],
                            "Followers": artist["followers"],
                            "Genres": artist["genres"],
                        })

                all_results.extend(city_results)

                with city_slots[city].container():
                    if city_results:
                        df_city = pd.DataFrame(city_results).drop(columns=["City", "State"])
                        df_city["Followers"] = df_city["Followers"].apply(lambda x: f"{x:,}")
                        st.markdown(f"**📍 {city}, {state_str}** — {len(city_results)} debut opportunit{'y' if len(city_results)==1 else 'ies'}")
                        st.dataframe(df_city.sort_values("Popularity Score"), use_container_width=True, hide_index=True)
                    else:
                        st.caption(f"📍 {city}, {state_str} — no opportunities found")

            progress.progress(100, text="Complete.")

            if all_results:
                save_results(all_results, {
                    "run_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                    "candidates_scanned": len(candidates),
                    "cities_scanned": len(TARGET_TOWNS),
                })
                st.success(f"✅ {len(all_results)} debut opportunities saved. Switch to **Dashboard** to view.")
            else:
                st.info("No opportunities found. Try raising the popularity score or expanding the artist pool.")
