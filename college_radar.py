from apify_client import ApifyClient
from datetime import datetime
from pathlib import Path
import json
import pandas as pd
import requests
import streamlit as st

APIFY_TOKEN = st.secrets["APIFY_TOKEN"]
client = ApifyClient(APIFY_TOKEN)
LASTFM_API_KEY = st.secrets["LASTFM_API_KEY"]

RESULTS_FILE = Path.home() / ".college_radar_results.json"

ALL_TARGET_TOWNS: dict[str, str | list[str]] = {
    "Madison": "WI", "Bloomington": "IN", "East Lansing": "MI", "Champaign": "IL",
    "Iowa City": "IA", "Lawrence": "KS", "Columbia": "MO", "Manhattan": "KS",
    "Lincoln": "NE", "Fayetteville": "AR", "Lexington": "KY", "Tuscaloosa": "AL",
    "Minneapolis": "MN", "Athens": "OH", "Oxford": ["OH", "MS"], "West Lafayette": "IN",
    "Kalamazoo": "MI", "Muncie": "IN", "Ames": "IA",
}

UNDERGROUND_TAGS = [
    "uk bass", "future bass", "melodic techno", "afro house", "organic house",
    "leftfield", "footwork", "bass music", "lo-fi house", "wave",
    "dark clubbing", "speed garage", "gqom", "amapiano", "balearic",
    "new rave", "electroclash", "dancefloor", "nu-disco underground",
]

ELECTRONIC_GENRE_KEYWORDS = {
    "electronic", "dance", "house", "techno", "drum and bass", "bass",
    "edm", "electronica", "jungle", "garage", "trance", "ambient",
    "downtempo", "breakbeat", "dubstep", "future bass", "uk bass",
    "afro house", "amapiano", "gqom", "lo-fi", "wave", "rave",
}


# ── PERSISTENCE ───────────────────────────────────────────────────────────────

def save_results(results: list[dict], metadata: dict) -> None:
    data = {"metadata": metadata, "results": results}
    RESULTS_FILE.write_text(json.dumps(data, indent=2))


def load_results() -> tuple[list[dict], dict]:
    if not RESULTS_FILE.exists():
        return [], {}
    try:
        data = json.loads(RESULTS_FILE.read_text())
        return data.get("results", []), data.get("metadata", {})
    except Exception:
        return [], {}


# ── HELPERS ───────────────────────────────────────────────────────────────────

def lastfm_artist_info(name: str) -> tuple[int, bool]:
    try:
        r = requests.get("http://ws.audioscrobbler.com/2.0/", params={
            "method": "artist.getInfo", "artist": name,
            "api_key": LASTFM_API_KEY, "format": "json",
        }, timeout=8)
        data = r.json().get("artist", {})
        listeners = int(data.get("stats", {}).get("listeners", 0))
        top_tags = [t.get("name", "").lower() for t in data.get("tags", {}).get("tag", [])]
        is_electronic = any(kw in tag for tag in top_tags for kw in ELECTRONIC_GENRE_KEYWORDS)
        return listeners, is_electronic
    except Exception:
        return 0, False


def pull_candidates(max_listeners: int, target: int) -> list[str]:
    seen: set[str] = set()
    artists: list[str] = []
    status = st.sidebar.empty()
    for tag in UNDERGROUND_TAGS:
        if len(artists) >= target:
            break
        try:
            r = requests.get("http://ws.audioscrobbler.com/2.0/", params={
                "method": "tag.gettoptracks", "tag": tag,
                "api_key": LASTFM_API_KEY, "format": "json", "limit": 50,
            }, timeout=10)
            r.raise_for_status()
            for track in r.json().get("tracks", {}).get("track", []):
                if len(artists) >= target:
                    break
                artist_obj = track.get("artist", {})
                name = (artist_obj.get("name", "") if isinstance(artist_obj, dict) else str(artist_obj)).strip()
                if not name or name in seen:
                    continue
                seen.add(name)
                count, is_electronic = lastfm_artist_info(name)
                if not is_electronic:
                    status.caption(f"⛔ {name} — not electronic")
                    continue
                if count > max_listeners:
                    status.caption(f"❌ {name} — too big ({count:,})")
                    continue
                status.caption(f"✅ {name} — {count:,} listeners")
                artists.append(name)
        except Exception:
            pass
    status.empty()
    return artists


def get_spotify_data(artist: str, cache: dict) -> dict:
    if artist not in cache:
        try:
            run = client.actor("khadinakbar/spotify-artist-scraper").call(
                run_input={"artistNames": [artist], "proxyCountry": "US"}
            )
            for item in client.dataset(run.default_dataset_id).iterate_items():
                all_cities = item.get("topCities", [])
                top_countries = [c.get("country", "").lower() for c in item.get("topCountries", [])]
                us_in_countries = any(c in ("united states", "us", "usa") for c in top_countries)
                us_cities_exist = any(
                    c.get("country", "").lower() in ("united states", "us", "usa") for c in all_cities
                )
                cache[artist] = {
                    "monthly_listeners": item.get("monthlyListeners", 0),
                    "us_present": us_in_countries or us_cities_exist,
                    "top_cities": {
                        c.get("city", ""): c.get("listeners", 0)
                        for c in all_cities
                        if c.get("country", "").lower() in ("united states", "us", "usa")
                    },
                }
        except Exception:
            cache[artist] = {"monthly_listeners": 0, "us_present": False, "top_cities": {}}
    return cache.get(artist, {})


def has_debuted(artist: str, city: str, cache: dict) -> bool:
    if artist not in cache:
        try:
            run = client.actor("solidcode/bandsintown-scraper").call(
                run_input={"artists": [artist], "queryType": "events", "dateFilter": "past"}
            )
            past = set()
            for event in client.dataset(run.default_dataset_id).iterate_items():
                past.add(event.get("venue", {}).get("city", ""))
            cache[artist] = past
        except Exception:
            cache[artist] = set()
    return city in cache[artist]


# ── PAGE ──────────────────────────────────────────────────────────────────────

st.set_page_config(page_title="College Debut Radar", layout="wide", page_icon="🎓")
st.title("🎓 College Town Debut Radar")

tab_dashboard, tab_run = st.tabs(["📊 Dashboard", "🔍 Run New Scan"])


# ── TAB 1: DASHBOARD ──────────────────────────────────────────────────────────

with tab_dashboard:
    saved_results, meta = load_results()

    if not saved_results:
        st.info("No data yet. Go to **Run New Scan** to generate your first report.")
    else:
        last_run = meta.get("run_at", "unknown")
        city_count = len(set(r["City"] for r in saved_results))
        st.caption(f"Last updated: **{last_run}** · {len(saved_results)} debut opportunities across {city_count} cities")

        df_all = pd.DataFrame(saved_results)

        # Summary metric row
        cols = st.columns(4)
        cols[0].metric("Total Opportunities", len(saved_results))
        cols[1].metric("Cities with Matches", city_count)
        cols[2].metric("Unique Artists", df_all["Artist"].nunique())
        cols[3].metric("Artists Scanned", meta.get("candidates_scanned", "—"))

        st.divider()

        # City-by-city breakdown
        for city in sorted(df_all["City"].unique()):
            city_df = df_all[df_all["City"] == city].copy()
            state = "/".join(city_df["State"].unique())
            city_df = city_df.drop(columns=["City", "State"]).sort_values("City Listeners", ascending=False)
            city_df["City Listeners"] = city_df["City Listeners"].apply(lambda x: f"{int(x):,}")
            city_df["Global Monthly Listeners"] = city_df["Global Monthly Listeners"].apply(lambda x: f"{int(x):,}")

            with st.expander(f"📍 **{city}, {state}** — {len(city_df)} debut opportunit{'y' if len(city_df)==1 else 'ies'}", expanded=True):
                st.dataframe(city_df, use_container_width=True, hide_index=True)

        st.divider()
        csv = df_all.to_csv(index=False)
        st.download_button("⬇ Download CSV", csv, "debut_radar_results.csv", "text/csv")


# ── TAB 2: RUN NEW SCAN ───────────────────────────────────────────────────────

with tab_run:
    col_left, col_right = st.columns([1, 1])

    with col_left:
        st.subheader("📍 Target Markets")
        city_labels = [
            f"{city}, {'/'.join(state) if isinstance(state, list) else state}"
            for city, state in ALL_TARGET_TOWNS.items()
        ]

        btn_col1, btn_col2 = st.columns([1, 1])
        if btn_col1.button("Select All", use_container_width=True):
            st.session_state["selected_cities"] = city_labels
        if btn_col2.button("Clear All", use_container_width=True):
            st.session_state["selected_cities"] = []

        default_cities = st.session_state.get("selected_cities", city_labels)
        selected_labels = st.pills("Select markets:", city_labels, selection_mode="multi", default=default_cities)

        TARGET_TOWNS: dict[str, str | list[str]] = {
            city: state for city, state in ALL_TARGET_TOWNS.items()
            if any(lbl.startswith(city + ",") for lbl in (selected_labels or []))
        }
        st.caption(f"{len(TARGET_TOWNS)} markets selected.")

    with col_right:
        st.subheader("⚙️ Settings")
        candidate_pool = st.slider("Candidate pool size", 20, 200, 100, step=10,
            help="Artists to pull from underground tags. Larger = better coverage but slower.")
        max_lastfm = st.select_slider("Max artist size (Last.fm listeners)",
            options=[10_000, 25_000, 50_000, 100_000, 250_000, 500_000],
            value=100_000, format_func=lambda x: f"{x:,}",
            help="Cuts artists above this size. Lower = more underground.")
        min_city_listeners = int(st.number_input("Min Spotify listeners per city",
            value=5_000, step=1_000, format="%d") or 5_000)
        global_floor = int(st.number_input("Min global Spotify listeners",
            value=10_000, step=5_000, format="%d") or 10_000)

    st.divider()

    if not TARGET_TOWNS:
        st.warning("Select at least one market to continue.")
    else:
        run_clicked = st.button("▶ Run Full Analysis", type="primary", use_container_width=True)

        if run_clicked:
            # Step 1: Candidate pool
            with st.status("Step 1 — Building electronic artist candidate pool...", expanded=False) as s:
                candidates = pull_candidates(max_listeners=max_lastfm, target=candidate_pool)
                s.update(label=f"Step 1 complete — {len(candidates)} electronic artists found.", state="complete")

            if not candidates:
                st.error("No candidates found. Try raising the max artist size.")
                st.stop()

            # Step 2: Per-city analysis
            st.subheader("Live Results")
            spotify_cache: dict = {}
            bandsintown_cache: dict = {}
            all_results: list[dict] = []
            city_placeholders = {city: st.empty() for city in sorted(TARGET_TOWNS.keys())}
            progress = st.progress(0, text="Starting...")

            for c_idx, city in enumerate(sorted(TARGET_TOWNS.keys())):
                state = TARGET_TOWNS[city]
                states = state if isinstance(state, list) else [state]
                state_str = "/".join(states)
                city_results = []

                for a_idx, artist in enumerate(candidates):
                    pct = int(((c_idx * len(candidates) + a_idx) / (len(TARGET_TOWNS) * len(candidates))) * 100)
                    progress.progress(pct, text=f"{city}, {state_str} — {artist} ({a_idx+1}/{len(candidates)})")

                    spotify = get_spotify_data(artist, spotify_cache)
                    if not spotify.get("us_present", False):
                        continue
                    if spotify.get("monthly_listeners", 0) < global_floor:
                        continue
                    city_listeners = spotify.get("top_cities", {}).get(city, 0)
                    if city_listeners < min_city_listeners:
                        continue
                    if has_debuted(artist, city, bandsintown_cache):
                        continue

                    for s in states:
                        row = {
                            "Artist": artist,
                            "City": city,
                            "State": s,
                            "City Listeners": city_listeners,
                            "Global Monthly Listeners": spotify["monthly_listeners"],
                        }
                        city_results.append(row)
                        all_results.append(row)

                # Update city placeholder live
                with city_placeholders[city].container():
                    if city_results:
                        df_city = pd.DataFrame(city_results).drop(columns=["City", "State"])
                        df_city = df_city.sort_values("City Listeners", ascending=False)
                        df_city["City Listeners"] = df_city["City Listeners"].apply(lambda x: f"{int(x):,}")
                        df_city["Global Monthly Listeners"] = df_city["Global Monthly Listeners"].apply(lambda x: f"{int(x):,}")
                        st.markdown(f"**📍 {city}, {state_str}** — {len(city_results)} opportunit{'y' if len(city_results)==1 else 'ies'}")
                        st.dataframe(df_city, use_container_width=True, hide_index=True)
                    else:
                        st.caption(f"📍 {city}, {state_str} — no opportunities found")

            progress.progress(100, text="Done.")

            # Save results
            if all_results:
                metadata = {
                    "run_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                    "candidates_scanned": len(candidates),
                    "cities_scanned": len(TARGET_TOWNS),
                }
                save_results(all_results, metadata)
                st.success(f"✅ {len(all_results)} debut opportunities saved. Switch to **Dashboard** to view.")
            else:
                st.info("No opportunities found. Try lowering the city listener threshold or expanding the pool.")
