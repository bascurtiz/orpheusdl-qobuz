import os
import unicodedata
import re
import logging
import socket
import threading
import webbrowser
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse
import concurrent.futures

from utils.models import *
from .qobuz_api import Qobuz


module_information = ModuleInformation(
    service_name = 'Qobuz',
    module_supported_modes = ModuleModes.download | ModuleModes.credits,
    login_behaviour = ManualEnum.manual,
    global_settings = {'app_id': '798273057', 'app_secret': 'abb21364945c0583309667d13ca3d93a', 'quality_format': '{sample_rate}kHz/{bit_depth}bit'},
    session_settings = {'username': '', 'password': '', 'user_id': '', 'auth_token': '', 'use_id_token': 'false'},
    session_storage_variables = ['token', 'user_id'],
    netlocation_constant = 'qobuz',
    url_constants={
        'track': DownloadTypeEnum.track,
        'album': DownloadTypeEnum.album,
        'playlist': DownloadTypeEnum.playlist,
        'artist': DownloadTypeEnum.artist,
        'interpreter': DownloadTypeEnum.artist,
        'label': DownloadTypeEnum.label,
    },
    test_url = 'https://open.qobuz.com/track/52151405'
)


class ModuleInterface:
    def __init__(self, module_controller: ModuleController):
        settings = module_controller.module_settings
        self.session = Qobuz(settings['app_id'], settings['app_secret'], module_controller.module_error)
        self.module_controller = module_controller
        
        # Load credentials from both persistent settings and session storage
        storage = module_controller.temporary_settings_controller
        auth_token = storage.read('token') or settings.get('auth_token')
        user_id = storage.read('user_id') or settings.get('user_id')
        
        self.session.auth_token = auth_token
        # Ensure user_id is in module_settings for GUI visibility
        if not settings.get('user_id') and user_id:
            settings['user_id'] = user_id

        # Trust the saved token at startup - we will re-validate only if an API call fails
        if self.session.auth_token:
            pass
        else:
            # No token, try fallback to email/pass if present
            username = (settings.get('username') or '').strip()
            password = (settings.get('password') or '').strip()
            if username and password:
                try:
                    self.login(username, password)
                except:
                    pass




        # 5 = 320 kbps MP3, 6 = 16-bit FLAC, 7 = 24-bit / =< 96kHz FLAC, 27 =< 192 kHz FLAC
        self.quality_parse = {
            QualityEnum.MINIMUM: 5,
            QualityEnum.LOW: 5,
            QualityEnum.MEDIUM: 5,
            QualityEnum.HIGH: 5,
            QualityEnum.LOSSLESS: 6,
            QualityEnum.HIFI: 27,
            QualityEnum.ATMOS: 27
        }
        self.quality_tier = module_controller.orpheus_options.quality_tier
        self.quality_format = settings.get('quality_format')

    def _ensure_credentials(self, force=False, status_callback=None):
        """Require valid user credentials before download/metadata that leads to download.
        Without this, only previews would be downloaded. Matches TIDAL behavior: 
        auto-triggers OAuth flow if missing/force=True."""
        if getattr(self.session, 'auth_token', None):
            return
            
        settings = self.module_controller.module_settings
        username = (settings.get('username') or '').strip()
        password = (settings.get('password') or '').strip()
        user_id = (settings.get('user_id') or '').strip()
        auth_token = (settings.get('auth_token') or '').strip()
        has_email_pass = username and password
        has_id_token = user_id and auth_token
        
        # Priority: 1. ID/Token (OAuth) 2. Email/Pass
        if has_id_token:
            self.session.auth_token = auth_token
            # We don't pre-validate here; let the actual API call handle errors.
            return

        if has_email_pass:
            try:
                self.login(username, password)
                return
            except Exception as e:
                logging.debug(f"Qobuz: Email login failed, falling back to OAuth if forced: {e}")

        # If we got here, we have no valid token. 
        # In CLI mode, or if forced (GUI button/download started), trigger OAuth
        is_gui_mode = os.environ.get('ORPHEUS_GUI') == '1'
        if not is_gui_mode or force:
            self._start_oauth_flow(status_callback)
        else:
            # GUI only - allow guest mode for now
            pass

    def _start_oauth_flow(self, status_callback=None):
        """
        Implementation of Qobuz OAuth flow.
        Opens the browser, waits for the redirect on a local port, 
        and then completes the login.
        """
        def _log(msg):
            if status_callback: status_callback(msg)
            else: self.module_controller.printer_controller.oprint(f"Qobuz: {msg}")

        try:
            # 0. Check if we already have a token to avoid unnecessary flows
            if self.is_authenticated():
                return

            # 1. Scrape current bundle info to ensure we have the latest app_id/private_key
            _log("Scraping Qobuz tokens...")
            info = self.session.get_bundle_info()
            app_id = info['app_id']
            # Update session with the latest scraped app_id
            self.session.app_id = app_id

            # 2. Find a free port for the redirect server
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(('', 0))
                port = s.getsockname()[1]
            
            oauth_url = f"https://www.qobuz.com/signin/oauth?ext_app_id={app_id}&redirect_url=http://localhost:{port}"

            class OAuthHandler(BaseHTTPRequestHandler):
                code = None
                def do_GET(self):
                    parsed = urlparse(self.path)
                    params = parse_qs(parsed.query)
                    code = params.get("code", [params.get("code_autorisation", [""])[0]])[0]
                    if code:
                        OAuthHandler.code = code
                        self.send_response(200)
                        self.send_header("Content-type", "text/html")
                        self.end_headers()
                        self.wfile.write(b"<html><body style='font-family:sans-serif;text-align:center;padding:50px;background:#121212;color:white;'>")
                        self.wfile.write(b"<h2 style='color:#6ee7f7'>Qobuz Login Successful!</h2>")
                        self.wfile.write(b"<p>You can now close this tab and return to OrpheusDL.</p>")
                        self.wfile.write(b"</body></html>")
                    else:
                        self.send_response(400)
                        self.end_headers()
                        self.wfile.write(b"No code received.")

                def log_message(self, format, *args):
                    return # Silence logging

            # Event to signal completion
            completion_event = threading.Event()

            _log(f"Waiting for browser login... (Link: {oauth_url})")
            webbrowser.open(oauth_url)

            # Start server in a thread to capture the code
            def _run_server():
                try:
                    server = HTTPServer(('127.0.0.1', port), OAuthHandler)
                    server.handle_request() # Wait for one request then exit
                    server.server_close()
                    
                    if OAuthHandler.code:
                        _log("Logging in with code...")
                        self.login_with_oauth_code(OAuthHandler.code)
                        _log("OAuth Login Successful!")
                    else:
                        _log("OAuth failed: No code received.")
                except Exception as e:
                    _log(f"Login process error: {str(e)}")
                finally:
                    completion_event.set()

            threading.Thread(target=_run_server, daemon=True).start()

            # Wait for completion (blocking) - timeout after 3 minutes
            if not completion_event.wait(timeout=180):
                _log("OAuth timeout: Login took too long.")
                return False

            return self.is_authenticated()

        except Exception as e:
            _log(f"OAuth Flow Error: {str(e)}")
            return False

    def is_authenticated(self) -> bool:
        """Return True if we have a valid auth token."""
        return bool(self.session.auth_token)

    def ensure_can_download(self) -> bool:
        """Called by downloader to ensure we are logged in."""
        if not self.is_authenticated():
            self._ensure_credentials(force=True)
        return True

    def login(self, email, password):
        settings = self.module_controller.module_settings
        user_id = (settings.get('user_id') or '').strip()
        auth_token = (settings.get('auth_token') or '').strip()
        # ID/Token mode (previously saved OAuth or manual token)
        if user_id and auth_token:
            self.session.auth_token = auth_token
            self.module_controller.temporary_settings_controller.set('token', auth_token)
            return True
        # Email/Password mode
        token = self.session.login(email, password)
        self.session.auth_token = token
        self.module_controller.temporary_settings_controller.set('token', token)

    def login_with_oauth_code(self, code):
        """Exchange OAuth code and update both settings and session."""
        try:
            usr_info = self.session.login_with_oauth_code(code)
            auth_token = usr_info.get('user_auth_token') or self.session.auth_token
            user_id = str(usr_info.get('user', {}).get('id', ''))
            
            # Persist to persistent settings so it survives restarts
            self.module_controller.module_settings['auth_token'] = auth_token
            self.module_controller.module_settings['user_id'] = user_id
            
            # Clears email/pass to indicate OAuth is active
            self.module_controller.module_settings['username'] = ''
            self.module_controller.module_settings['password'] = ''
            
            # Save to temporary session storage (loginstorage.bin)
            self.module_controller.temporary_settings_controller.set('token', auth_token)
            self.module_controller.temporary_settings_controller.set('user_id', user_id)
            
            return True
        except Exception as e:
            logging.error(f"Qobuz OAuth login failed: {e}")
            raise e

    def _get_year(self, date_val):
        if not date_val:
            return None
        if isinstance(date_val, (int, float)):
            try:
                return datetime.utcfromtimestamp(date_val).strftime('%Y')
            except (OSError, ValueError):
                return None
        try:
            return str(date_val).split('-')[0]
        except (AttributeError, IndexError):
            return None

    def get_track_info(self, track_id, quality_tier: QualityEnum, codec_options: CodecOptions, data={}):
        self._ensure_credentials()
        # Resolve proxy IDs (e.g. from Apple Music search) if needed
        # We only do this if we have credentials, which is ensured by the line above.
        if isinstance(data, dict) and data.get('proxy_platform') == 'applemusic':
             # The result_id passed here is the Apple Music ID.
             # We should have stored the ISRC in the SearchResult if possible, 
             # but here we'll try to find the Qobuz ID via ISRC lookup using our logged-in session.
             # For now, we'll try to find it in the search results if it was cached.
             pass

        # For guest mode, we don't ensure credentials here; get_track will use the guest app_id if not logged in.
        # However, we only allow this for metadata-only calls.
        track_data = data.get(track_id) if data and isinstance(data, dict) else None
        if not track_data:
            try:
                track_data = self.session.get_track(track_id)
            except Exception as e:
                # If track_id is not a Qobuz ID (e.g. it's an Apple Music ID),
                # this will fail. We should ideally handle this better.
                raise e
        album_data = track_data.get('album') or track_data
        if isinstance(album_data, dict) and 'artist' not in album_data and track_data.get('album'):
            album_data = track_data['album']

        main_artist = track_data.get('performer') or (album_data.get('artist') if isinstance(album_data, dict) else None)
        if not main_artist:
            main_artist = {'name': 'Unknown Artist', 'id': ''}
        artists = [
            unicodedata.normalize('NFKD', main_artist['name'])
            .encode('ascii', 'ignore')
            .decode('utf-8')
        ]
        role_mapping = {
            'Lyricist': 'Lyricist',
            'Lyricists': 'Lyricist',
            'Vocals': 'Lyricist',
            'Composer': 'Composer',
            'Composers': 'Composer',
            'Producer': 'Producer',
            'Producers': 'Producer'
        }
        if track_data.get('performers'):
            performers = []
            for credit in track_data['performers'].split(' - '):
                contributor_role = [role_mapping.get(r, r) for r in credit.split(', ')[1:]]
                contributor_name = credit.split(', ')[0]
                for contributor in ['MainArtist', 'FeaturedArtist', 'Artist']:
                    if contributor in contributor_role:
                        if contributor_name not in artists:
                            artists.append(contributor_name)
                        contributor_role.remove(contributor)
                if not contributor_role:
                    continue
                performers.append(f"{contributor_name}, {', '.join(contributor_role)}")
            track_data = dict(track_data)
            track_data['performers'] = ' - '.join(performers)
        artists[0] = main_artist['name']

        # Extract the primary album artist name.
        album_artist = album_data.get('artist', {}).get('name', '') if isinstance(album_data.get('artist'), dict) else ''

        tags = Tags(
            album_artist = album_artist,
            composer = track_data.get('composer', {}).get('name') if isinstance(track_data.get('composer'), dict) else None,
            release_date = album_data.get('release_date_original'),
            track_number = track_data.get('track_number'),
            total_tracks = album_data.get('tracks_count'),
            disc_number = track_data.get('media_number'),
            total_discs = album_data.get('media_count'),
            isrc = track_data.get('isrc'),
            upc = album_data.get('upc'),
            label = album_data.get('label', {}).get('name') if isinstance(album_data.get('label'), dict) else None,
            copyright = album_data.get('copyright'),
            genres = [album_data.get('genre', {}).get('name')] if isinstance(album_data.get('genre'), dict) else [],
            track_url = f"https://open.qobuz.com/track/{track_id}"
        )

        # track and album title fix to include version tag
        track_name = f"{track_data.get('work')} - " if track_data.get('work') else ""
        track_name += (track_data.get('title') or '').rstrip()
        track_name += f' ({track_data.get("version")})' if track_data.get("version") else ''
        album_name = (album_data.get('title') or '').rstrip()
        album_name += f' ({album_data.get("version")})' if album_data.get("version") else ''

        cover_url = ''
        if isinstance(album_data.get('image'), dict):
            cover_url = (album_data['image'].get('large') or '').split('_')[0] + '_org.jpg' if album_data['image'].get('large') else ''

        # When not authenticated: return display-only TrackInfo (no download URL); expand works, download will raise in get_track_download
        if not getattr(self.session, 'auth_token', None):
            self._ensure_credentials()
            preview_url = None
            try:
                preview_url = self.session.get_sample_url(str(track_id))
            except Exception:
                pass
            return TrackInfo(
                id=str(track_id),
                name=track_name,
                album_id=album_data.get('id', ''),
                album=album_name,
                artists=artists,
                artist_id=main_artist.get('id', ''),
                bit_depth=None,
                bitrate=320,
                sample_rate=None,
                release_year=int(self._get_year(album_data.get('release_date_original')) or 0),
                explicit=bool(track_data.get('parental_warning')),
                cover_url=cover_url,
                tags=tags,
                codec=CodecEnum.MP3,
                duration=track_data.get('duration'),
                credits_extra_kwargs={'data': {track_id: track_data}},
                download_extra_kwargs={},
                error=None,
                preview_url=preview_url,
            )

        quality_tier_id = self.quality_parse[quality_tier]
        try:
            stream_data = self.session.get_file_url(track_id, quality_tier_id)
        except Exception as e:
            # If we get a 401 for MP3 (format 5), it might be an API quirk.
            # Don't crash; fall back to basic info and let get_track_download handle it later.
            is_401 = '"code":401' in str(e) or "authentication is required" in str(e).lower()
            if is_401 and quality_tier_id == 5:
                stream_data = {'bit_depth': 16, 'sampling_rate': 44.1, 'format_id': 5, 'url': None}
            else:
                raise e

        bitrate = 320
        if stream_data.get('format_id') in {6, 7, 27}:
            bitrate = int((stream_data['sampling_rate'] * 1000 * stream_data['bit_depth'] * 2) // 1000)
        elif not stream_data.get('format_id'):
            bitrate = stream_data.get('format_id')

        return TrackInfo(
            id=str(track_id),
            name=track_name,
            album_id=album_data['id'],
            album=album_name,
            artists=artists,
            artist_id=main_artist['id'],
            bit_depth=stream_data['bit_depth'],
            bitrate=bitrate,
            sample_rate=stream_data['sampling_rate'],
            release_year=int(self._get_year(album_data.get('release_date_original')) or 0),
            explicit=track_data['parental_warning'],
            cover_url=cover_url or (album_data['image']['large'].split('_')[0] + '_org.jpg'),
            tags=tags,
            codec=CodecEnum.FLAC if stream_data.get('format_id') in {6, 7, 27} else CodecEnum.NONE if not stream_data.get('format_id') else CodecEnum.MP3,
            duration=track_data.get('duration'),
            credits_extra_kwargs={'data': {track_id: track_data}},
            download_extra_kwargs={'url_or_track_id': stream_data.get('url')},
            error=f'Track "{track_data["title"]}" is not streamable!' if not track_data.get('streamable') else None
        )

    def get_track_download(self, url_or_track_id, quality_tier=None, codec_options=None, **kwargs):
        # Called either as get_track_download(url) from download_extra_kwargs or get_track_download(track_id, quality_tier, codec_options) from core fallback
        if isinstance(url_or_track_id, str) and url_or_track_id.startswith('http'):
            url = url_or_track_id
        else:
            self._ensure_credentials()
            track_id = url_or_track_id
            quality_id = self.quality_parse.get(quality_tier, 5) if quality_tier is not None else 5
            stream_data = self.session.get_file_url(str(track_id), quality_id)
            url = stream_data.get('url')
        return TrackDownloadInfo(download_type=DownloadEnum.URL, file_url=url)

    def get_album_info(self, album_id):
        self._ensure_credentials()
        album_data = self.session.get_album(album_id)

        booklet_url = None
        if album_data.get('goodies'):
            try:
                booklet_url = album_data['goodies'][0].get('url')
            except (IndexError, KeyError, TypeError):
                pass

        tracks, extra_kwargs = [], {}
        for track in album_data.pop('tracks')['items']:
            track_id = str(track['id'])
            tracks.append(track_id)
            track['album'] = album_data
            extra_kwargs[track_id] = track

        # get the wanted quality for an actual album quality_format string
        quality_tier = self.quality_parse[self.quality_tier]
        # TODO: Ignore sample_rate and bit_depth if album_data['hires'] is False?
        bit_depth = 24 if quality_tier == 27 and album_data['hires_streamable'] else 16
        sample_rate = album_data['maximum_sampling_rate'] if quality_tier == 27 and album_data[
            'hires_streamable'] else 44.1

        quality_tags = {
            'sample_rate': sample_rate,
            'bit_depth': bit_depth
        }

        # album title fix to include version tag
        album_name = album_data.get('title').rstrip()
        album_name += f' ({album_data.get("version")})' if album_data.get("version") else ''

        album_quality = self.quality_format.format(**quality_tags) if self.quality_format != '' else None
        if sample_rate == 44.1 and (bit_depth == 16 or bit_depth == 24):
            album_quality = None
        else:
            is_hi_res = (bit_depth == 24 and sample_rate >= 88.2) or (bit_depth > 24)
            if album_quality and is_hi_res:
                album_quality = f'🅷 HI-RES / {album_quality}'

        return AlbumInfo(
            name = album_name,
            artist = album_data['artist']['name'],
            artist_id = album_data['artist']['id'],
            tracks = tracks,
            release_year = int(self._get_year(album_data.get('release_date_original')) or 0),
            explicit = album_data['parental_warning'],
            quality = album_quality,
            description = album_data.get('description'),
            cover_url = album_data['image']['large'].split('_')[0] + '_org.jpg',
            all_track_cover_jpg_url = album_data['image']['large'],
            upc = album_data.get('upc'),
            duration = album_data.get('duration'),
            booklet_url = booklet_url,
            track_extra_kwargs = {'data': extra_kwargs}
        )

    def get_playlist_info(self, playlist_id):
        self._ensure_credentials()
        # Fetch first batch to get total track count
        playlist_data = self.session.get_playlist(playlist_id)

        
        tracks, extra_kwargs = [], {}
        
        # Process first batch of tracks
        for track in playlist_data['tracks']['items']:
            track_id = str(track['id'])
            extra_kwargs[track_id] = track
            tracks.append(track_id)
        
        # Check if there are more tracks to fetch (pagination)
        total_tracks = playlist_data['tracks'].get('total', len(playlist_data['tracks']['items']))
        fetched_tracks = len(playlist_data['tracks']['items'])
        
        # Fetch remaining tracks if playlist has more than initial batch
        if fetched_tracks < total_tracks:
            offset = fetched_tracks
            limit = 500  # Qobuz API limit per request
            
            while offset < total_tracks:
                # Fetch next batch
                batch_data = self.session.get_playlist(playlist_id, limit=limit, offset=offset)
                
                if not batch_data['tracks']['items']:
                    break  # No more tracks to fetch
                
                # Process batch tracks
                for track in batch_data['tracks']['items']:
                    track_id = str(track['id'])
                    extra_kwargs[track_id] = track
                    tracks.append(track_id)
                
                offset += len(batch_data['tracks']['items'])

        return PlaylistInfo(
            name = playlist_data['name'],
            creator = playlist_data['owner']['name'],
            creator_id = playlist_data['owner']['id'],
            release_year = self._get_year(playlist_data.get('created_at')),
            description = playlist_data.get('description'),
            duration = playlist_data.get('duration'),
            tracks = tracks,
            track_extra_kwargs = {'data': extra_kwargs}
        )

    def get_artist_info(self, artist_id, get_credited_albums):
        self._ensure_credentials()
        artist_data = self.session.get_artist(artist_id)

        albums_raw = (artist_data.get('albums') or {}).get('items') or []
        
        # Batch fetch missing album metadata (tracks_count and duration)
        missing_metadata = [idx for idx, a in enumerate(albums_raw) if isinstance(a, dict) and (not a.get('tracks_count') or not a.get('duration'))]
        if missing_metadata:
            a_meta = {}
            def _fetch_qobuz_album_meta(aid):
                try:
                    return aid, self.session.get_album(aid)
                except: pass
                return aid, None

            with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
                fetch_ids = [albums_raw[idx]['id'] for idx in missing_metadata]
                for aid, full_data in executor.map(_fetch_qobuz_album_meta, fetch_ids):
                    if full_data: a_meta[str(aid)] = full_data
            
            for idx in missing_metadata:
                aid = str(albums_raw[idx]['id'])
                if aid in a_meta:
                    albums_raw[idx].update(a_meta[aid])

        albums_out = []

        for album in albums_raw:
            # Fallback: if album isn't a dict, store stringified ID only
            if not isinstance(album, dict):
                albums_out.append(str(album))
                continue

            album_id = str(album.get('id') or '')

            # Build human-readable album name (title + optional version)
            name = album.get('name') or album.get('title') or 'Unknown Album'
            if album.get('version'):
                name += f" ({album.get('version')})"

            # Prefer album artist name, otherwise fall back to main artist name
            artist_name = None
            if isinstance(album.get('artist'), dict):
                artist_name = album['artist'].get('name')
            if not artist_name:
                artist_name = artist_data.get('name')

            # Extract release year from known date fields
            release_year = None
            release_date = (
                album.get('release_date_original')
                or album.get('released_at')
                or album.get('release_date')
            )
            if release_date:
                try:
                    release_year = self._get_year(release_date)
                    if release_year: release_year = int(release_year)
                except (ValueError, TypeError):
                    release_year = None

            # Album cover image - mirror search() album logic
            cover_url = None
            image = album.get('image')
            if isinstance(image, dict):
                cover_url = image.get('small') or image.get('thumbnail') or image.get('large')

            # Duration in seconds (for GUI to format)
            duration = album.get('duration')

            # Quality / sampling info (matches album search "Additional" column)
            # Only show quality when it's genuinely hi-res (above 44.1kHz/24-bit CD/Enhanced baseline)
            additional_parts = []
            
            # Add track count
            tc = album.get('tracks_count')
            if tc:
                additional_parts.append(f"1 track" if tc == 1 else f"{tc} tracks")

            if 'maximum_sampling_rate' in album:
                sr = album.get('maximum_sampling_rate')
                bd = album.get('maximum_bit_depth')
                if sr and bd:
                    if sr == 44.1 and (bd == 16 or bd == 24):
                        pass
                    else:
                        is_hi_res = (bd == 24 and sr >= 88.2) or (bd > 24)
                        if is_hi_res:
                            additional_parts.extend(["🅷 HI-RES", f"{sr}kHz/{bd}bit"])
                        else:
                            additional_parts.append(f"{sr}kHz/{bd}bit")
                elif sr:
                    if sr > 44.1:
                        additional_parts.extend(["🅷 HI-RES", f"{sr}kHz"])
                    else:
                        additional_parts.append(f"{sr}kHz")
            
            additional = additional_parts if additional_parts else None

            albums_out.append({
                'id': album_id,
                'name': name,
                'artist': artist_name,
                'release_year': release_year,
                'cover_url': cover_url,
                'duration': duration,
                'additional': additional,
                'explicit': bool(album.get('parental_warning')),
            })

        # Fallback: if we couldn't parse metadata, keep old behaviour (IDs only)
        if not albums_out:
            albums_out = [str(album['id']) for album in artist_data.get('albums', {}).get('items', [])]

        return ArtistInfo(
            name = artist_data['name'],
            albums = albums_out
        )

    def get_label_info(self, label_id: str, get_credited_albums: bool = True, **kwargs) -> ArtistInfo:
        self._ensure_credentials()
        """Return label metadata and albums as ArtistInfo (same shape as artist for download flow)."""
        label_data = self.session.get_label(label_id)

        label_name = label_data.get('name') or 'Unknown Label'
        albums_raw = (label_data.get('albums') or {}).get('items') or []
        
        # Batch fetch missing album metadata (tracks_count and duration)
        missing_metadata = [idx for idx, a in enumerate(albums_raw) if isinstance(a, dict) and (not a.get('tracks_count') or not a.get('duration'))]
        if missing_metadata:
            a_meta = {}
            def _fetch_qobuz_album_meta(aid):
                try:
                    return aid, self.session.get_album(aid)
                except: pass
                return aid, None

            with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
                fetch_ids = [albums_raw[idx]['id'] for idx in missing_metadata]
                for aid, full_data in executor.map(_fetch_qobuz_album_meta, fetch_ids):
                    if full_data: a_meta[str(aid)] = full_data
            
            for idx in missing_metadata:
                aid = str(albums_raw[idx]['id'])
                if aid in a_meta:
                    albums_raw[idx].update(a_meta[aid])

        albums_out = []

        for album in albums_raw:
            if not isinstance(album, dict):
                albums_out.append(str(album))
                continue
            album_id = str(album.get('id') or '')
            name = album.get('name') or album.get('title') or 'Unknown Album'
            if album.get('version'):
                name += f" ({album.get('version')})"
            artist_name = None
            if isinstance(album.get('artist'), dict):
                artist_name = album['artist'].get('name')
            if not artist_name:
                artist_name = label_name
            release_date = (
                album.get('release_date_original')
                or album.get('released_at')
                or album.get('release_date')
            )
            release_year = self._get_year(release_date)
            if release_year: 
                try: release_year = int(release_year)
                except: release_year = None
            cover_url = None
            image = album.get('image')
            if isinstance(image, dict):
                cover_url = image.get('small') or image.get('thumbnail') or image.get('large')
            duration = album.get('duration')
            # Quality / sampling info (matches album search "Additional" column)
            # Only show quality when it's genuinely hi-res (above 44.1kHz/24-bit CD/Enhanced baseline)
            additional_parts = []
            
            # Add track count
            tc = album.get('tracks_count')
            if tc:
                additional_parts.append(f"1 track" if tc == 1 else f"{tc} tracks")

            if 'maximum_sampling_rate' in album:
                sr = album.get('maximum_sampling_rate')
                bd = album.get('maximum_bit_depth')
                if sr and bd:
                    if sr == 44.1 and (bd == 16 or bd == 24):
                        pass
                    else:
                        is_hi_res = (bd == 24 and sr >= 88.2) or (bd > 24)
                        if is_hi_res:
                            additional_parts.extend(["🅷 HI-RES", f"{sr}kHz/{bd}bit"])
                        else:
                            additional_parts.append(f"{sr}kHz/{bd}bit")
                elif sr:
                    if sr > 44.1:
                        additional_parts.extend(["🅷 HI-RES", f"{sr}kHz"])
                    else:
                        additional_parts.append(f"{sr}kHz")
            
            additional = additional_parts if additional_parts else None

            albums_out.append({
                'id': album_id,
                'name': name,
                'artist': artist_name,
                'release_year': release_year,
                'cover_url': cover_url,
                'duration': duration,
                'additional': additional,
            })

        if not albums_out:
            albums_out = [str(a['id']) for a in (label_data.get('albums') or {}).get('items', [])]

        return ArtistInfo(
            name=label_name,
            artist_id=label_id,
            albums=albums_out,
        )

    def get_track_credits(self, track_id, data=None):
        track_data = data.get(track_id) if data else None
        if not track_data or not track_data.get('performers'):
            track_data = self.session.get_track(track_id)

        track_contributors = track_data.get('performers')

        # Normalize roles to standard tagging keys
        role_mapping = {
            'Lyricist': 'Lyricist',
            'Lyricists': 'Lyricist',
            'Vocals': 'Lyricist',
            'Composer': 'Composer',
            'Composers': 'Composer',
            'Producer': 'Producer',
            'Producers': 'Producer'
        }

        # Credits look like: {name}, {type1}, {type2} - {name2}, {type2}
        credits_dict = {}
        if track_contributors:
            for credit in track_contributors.split(' - '):
                contributor_role = [role_mapping.get(r, r) for r in credit.split(', ')[1:]]
                contributor_name = credit.split(', ')[0]

                for role in contributor_role:
                    # Check if the dict contains no list, create one
                    if role not in credits_dict:
                        credits_dict[role] = []
                    # Now add the name to the type list
                    if contributor_name not in credits_dict[role]:
                        credits_dict[role].append(contributor_name)

        # Convert the dictionary back to a list of CreditsInfo
        return [CreditsInfo(k, v) for k, v in credits_dict.items()]

    def search(self, query_type: DownloadTypeEnum, query, track_info: TrackInfo = None, limit: int = 10):
        # Fallback to Public Web App ID for guest searching if the primary one is restricted.
        GUEST_APP_ID = '712109809'
        GUEST_APP_SECRET = '589be88e4538daea11f509d29e4a23b1'
        
        results = {}
        if track_info and track_info.tags.isrc:
            try:
                results = self.session.search(query_type.name, track_info.tags.isrc, limit)
            except Exception as e:
                # If we get a 401, it might be a stale token or restricted App ID. Try guest fallback.
                is_401 = '"code":401' in str(e) or "authentication is required" in str(e).lower()
                if is_401:
                    orig_token = self.session.auth_token
                    orig_app_id = self.session.app_id
                    orig_app_secret = self.session.app_secret
                    
                    self.session.auth_token = None
                    self.session.app_id = GUEST_APP_ID
                    self.session.app_secret = GUEST_APP_SECRET
                    try:
                        results = self.session.search(query_type.name, track_info.tags.isrc, limit)
                    except Exception:
                        results = {}
                    finally:
                        # Restore original credentials for potential future download attempts
                        self.session.auth_token = orig_token
                        self.session.app_id = orig_app_id
                        self.session.app_secret = orig_app_secret
                else:
                    raise

        if not results:
            try:
                results = self.session.search(query_type.name, query, limit)
            except Exception as e:
                # If we get a 401, it might be a stale token or restricted App ID. Try guest fallback.
                is_401 = '"code":401' in str(e) or "authentication is required" in str(e).lower()
                if is_401:
                    orig_token = self.session.auth_token
                    orig_app_id = self.session.app_id
                    orig_app_secret = self.session.app_secret
                    
                    self.session.auth_token = None
                    self.session.app_id = GUEST_APP_ID
                    self.session.app_secret = GUEST_APP_SECRET
                    try:
                        results = self.session.search(query_type.name, query, limit)
                    except Exception as e2:
                        # Even GUEST_APP_ID failed (401 or 400). Fallback to Apple Music Search Proxy.
                        err_msg = str(e2).lower()
                        is_auth_error = '"code":401' in err_msg or '"code":400' in err_msg or "authentication" in err_msg or "invalid app_id" in err_msg
                        if is_auth_error:
                            logging.debug("Qobuz: Guest search restricted. Falling back to Apple Music Search Proxy.")
                            return self._search_apple_music_proxy(query_type, query, limit)
                        results = {}
                    finally:
                        # Restore original credentials for potential future download attempts
                        self.session.auth_token = orig_token
                        self.session.app_id = orig_app_id
                        self.session.app_secret = orig_app_secret
                elif query_type is DownloadTypeEnum.label:
                    return []  # catalog/search does not support type=labels; use Download tab with label URL
                else:
                    raise

        if not results:
            return []

        result_key = query_type.name + 's'
        if result_key not in results or not results[result_key].get('items'):
            if query_type is DownloadTypeEnum.label:
                return []  # API returns no labels; use Download tab with label URL (e.g. play.qobuz.com/label/12444)
            items_raw = []
        else:
            items_raw = results[result_key]['items']

        return self._format_search_items(items_raw, query_type)

    def _format_search_items(self, items_raw, query_type):
        """Helper to format raw Qobuz API JSON into a list of SearchResult objects."""
        # Batch fetch missing album metadata (tracks_count) using ThreadPoolExecutor
        if query_type is DownloadTypeEnum.album:
            missing_metadata = [idx for idx, i in enumerate(items_raw) if not i.get('tracks_count')]
            if missing_metadata:
                a_meta = {}
                def _fetch_qobuz_album_meta(aid):
                    try: return aid, self.session.get_album(aid)
                    except: return aid, None

                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
                    fetch_ids = [items_raw[idx]['id'] for idx in missing_metadata]
                    for aid, full_data in executor.map(_fetch_qobuz_album_meta, fetch_ids):
                        if full_data: a_meta[str(aid)] = full_data
                
                for idx in missing_metadata:
                    aid = str(items_raw[idx]['id'])
                    if aid in a_meta: items_raw[idx].update(a_meta[aid])

        # Pre-fetch preview URLs natively where possible
        preview_map = {}
        if query_type is DownloadTypeEnum.track:
            # Parallel native preview fetch for all results (including guests)
            def _fetch_native_preview(i):
                try:
                    p_url = self.session.get_sample_url(str(i['id']))
                    if p_url and isinstance(p_url, str) and p_url.startswith('http'):
                        return str(i['id']), p_url
                except: pass
                return str(i['id']), None

            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
                for iid, p_url in executor.map(_fetch_native_preview, items_raw):
                    if p_url: preview_map[iid] = p_url

            # Second-tier iTunes fallback ONLY for remaining tracks without native preview
            missing_idx = [idx for idx, i in enumerate(items_raw) if not preview_map.get(str(i['id']))]
            if missing_idx:
                def _fetch_itunes_preview(idx):
                    i = items_raw[idx]
                    try:
                        import requests
                        from urllib.parse import quote_plus
                        artist = i.get('performer', {}).get('name') or i.get('album', {}).get('artist', {}).get('name', '')
                        search_term = f"{artist} {i.get('title', '')}".strip()
                        itunes_url = f"https://itunes.apple.com/search?term={quote_plus(search_term)}&media=music&entity=song&limit=1"
                        res = requests.get(itunes_url, timeout=2).json()
                        if res.get('results') and res['results'][0].get('previewUrl'):
                            return str(i['id']), res['results'][0]['previewUrl']
                    except: pass
                    return str(i['id']), None
                
                with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
                    for iid, p_url in executor.map(_fetch_itunes_preview, missing_idx):
                        if p_url: preview_map[iid] = p_url

        items = []
        for i in items_raw:
            duration = None
            image_url = None
            preview_url = None
            additional = None
            playlist_track_count = None

            if query_type is DownloadTypeEnum.artist:
                artists = None
                year = None
                if i.get('image'):
                    image_url = i['image'].get('small') or i['image'].get('medium') or i['image'].get('large')
            elif query_type is DownloadTypeEnum.playlist:
                artists = [i['owner']['name']] if 'owner' in i and 'name' in i['owner'] else []
                year_raw = i.get('created_at')
                year = None
                if year_raw:
                    year = self._get_year(year_raw)
                duration = i.get('duration')
                playlist_track_count = i.get('tracks_count') or (i.get('tracks') or {}).get('total')
                track_label = f"1 track" if playlist_track_count == 1 else f"{playlist_track_count} tracks"
                tags_list = i.get('tags') or []
                is_hires = any(t.get('slug') == 'hi-res' for t in tags_list)
                additional = [track_label, "🅷 HI-RES"] if is_hires else ([track_label] if playlist_track_count is not None else None)
                if i.get('images300'):
                    image_url = i['images300'][0]
                elif i.get('image_rectangle'):
                    image_url = i['image_rectangle'][0] if isinstance(i['image_rectangle'], list) else i['image_rectangle']
            elif query_type is DownloadTypeEnum.track:
                artists = [i.get('performer', {}).get('name')] if i.get('performer') else ([i.get('album', {}).get('artist', {}).get('name')] if i.get('album') and i['album'].get('artist') else [])
                released_at = (i['album'].get('released_at') if 'album' in i else None) or i.get('released_at')
                year = None
                if released_at:
                    if isinstance(released_at, (int, float)):
                        year = self._get_year(released_at)
                    else:
                        year = str(released_at).split('-')[0]
                duration = i.get('duration')
                preview_url = preview_map.get(str(i['id']))
                if i.get('album') and i['album'].get('image'):
                    img = i['album']['image']
                    image_url = img.get('small') or img.get('thumbnail') or img.get('large') if isinstance(img, dict) else img
                if not image_url:
                    img = i.get('image') or i.get('image_thumbnail') or i.get('image_small') or i.get('image_large')
                    if img: image_url = img.get('small') or img.get('thumbnail') or img.get('large') if isinstance(img, dict) else img
            elif query_type is DownloadTypeEnum.album:
                artists = [i['artist']['name']] if 'artist' in i and 'name' in i['artist'] else []
                image_url = i['image']['small'] if i.get('image') and isinstance(i['image'], dict) else (i.get('image') or i.get('cover'))
                released_at = i.get('released_at') or i.get('release_date_original')
                year = None
                if released_at:
                    if isinstance(released_at, (int, float)):
                        year = self._get_year(released_at)
                    else:
                        year = str(released_at).split('-')[0]
                duration = i.get('duration')
                album_additional = []
                tc = i.get('tracks_count')
                if tc: album_additional.append(f"1 track" if tc == 1 else f"{tc} tracks")
                _sr = i.get("maximum_sampling_rate")
                _bd = i.get("maximum_bit_depth")
                _is_hi_res = _sr is not None and ((_bd == 24 and _sr >= 88.2) or (_bd is not None and _bd > 24))
                if _is_hi_res: album_additional.extend(["🅷 HI-RES", f"{_sr}kHz/{_bd}bit"])
                elif _sr: album_additional.append(f"{_sr}kHz/{_bd}bit")
                additional = album_additional if album_additional else None
            elif query_type is DownloadTypeEnum.label:
                artists = []; year = None; duration = None
                if i.get('image'): image_url = i['image'].get('small') or i['image'].get('medium') or i['image'].get('large')
            else: raise Exception('Query type is invalid')

            if query_type is DownloadTypeEnum.playlist and (playlist_track_count is None or playlist_track_count == 0):
                continue
            
            name = i.get('name') or i.get('title')
            name += f" ({i.get('version')})" if i.get('version') else ''
            final_additional = additional
            if not final_additional and query_type not in (DownloadTypeEnum.album, DownloadTypeEnum.playlist):
                _sr = i.get("maximum_sampling_rate")
                _bd = i.get("maximum_bit_depth")
                _is_hi_res = _sr is not None and ((_bd == 24 and _sr >= 88.2) or (_bd is not None and _bd > 24))
                if _is_hi_res: final_additional = ["🅷 HI-RES", f"{_sr}kHz/{_bd}bit"]
                elif _sr: final_additional = [f"{_sr}kHz/{_bd}bit"]

            item = SearchResult(
                name=name, artists=artists, year=year, result_id=str(i['id']),
                explicit=bool(i.get('parental_warning')), additional=final_additional,
                duration=duration, image_url=image_url, preview_url=preview_url,
                extra_kwargs={'data': {str(i['id']): i}} if query_type is DownloadTypeEnum.track else {}
            )
            items.append(item)
        return items

    def _search_apple_music_proxy(self, query_type: DownloadTypeEnum, query: str, limit: int):
        """
        Search via Apple Music as a proxy for Qobuz guests.
        Retrieves public metadata and prepares it for delayed matching during download.
        """
        try:
            from orpheus import module_controller
            am_module = module_controller.get_module_by_name('applemusic')
            if not am_module:
                logging.debug("Qobuz Proxy: Apple Music module not found.")
                return []

            # Perform search on Apple Music
            logging.debug(f"Qobuz Proxy: Searching Apple Music for '{query}'...")
            am_results = am_module.search(query_type, query, limit=limit)
            
            if not am_results:
                return []

            # We return Apple Music results directly as SearchResult objects.
            # We store the Apple Music ID in extra_kwargs so that
            # we can match it back to Qobuz correctly when a user logs in to download.
            results = []
            for am_item in am_results:
                # Add a hint in the name or additional field to inform the user it's a proxy result.
                am_item.additional = am_item.additional or []
                if "matched via Apple Music" not in str(am_item.additional):
                    am_item.additional.append("Guest Search Proxy (Apple Music)")
                
                # Store the proxy data for delayed matching
                am_item.extra_kwargs = am_item.extra_kwargs or {}
                am_item.extra_kwargs['proxy_platform'] = 'applemusic'
                am_item.extra_kwargs['proxy_id'] = am_item.result_id
                
                results.append(am_item)

            logging.debug(f"Qobuz Proxy: Returning {len(results)} proxy results.")
            return results

        except Exception as e:
            logging.debug(f"Qobuz Proxy: Failed: {e}")
            import traceback
            logging.debug(traceback.format_exc())
            return []


    def _search_scraper(self, query_type: DownloadTypeEnum, query: str, limit: int):
        """Perform a search on the Qobuz website and extract results from the preloaded state."""
        try:
            import json
            import re
            import requests
            
            # Map DownloadTypeEnum to Qobuz web search types
            type_map = {
                DownloadTypeEnum.track: 'tracks',
                DownloadTypeEnum.album: 'albums',
                DownloadTypeEnum.artist: 'artists',
                DownloadTypeEnum.playlist: 'playlists'
            }
            q_type = type_map.get(query_type, 'tracks')
            
            # Use a realistic User-Agent to avoid being blocked
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept-Language': 'en-US,en;q=0.9',
                'Referer': 'https://www.qobuz.com/',
            }
            
            # We use a specific region (gb-en) to ensure English results
            # Adding a trailing slash sometimes avoids an extra 301/302 redirect
            url = f"https://www.qobuz.com/gb-en/search?q={query}&type={q_type}"
            logging.debug(f"Qobuz Scraper: Scraping {url}...")
            
            # Use a fresh requests session to avoid any sticky 401/400 headers from the main session
            resp = requests.get(url, headers=headers, timeout=10, allow_redirects=True)
            resp.raise_for_status()
            
            # Extract the preloaded state JSON blob.
            # Qobuz puts its data in window.__PRELOADED_STATE__
            json_blob = None
            
            # Pattern 1: Look for the variable assignment in the HTML
            # We look for window.__PRELOADED_STATE__ = { ... }
            match = re.search(r'__PRELOADED_STATE__\s*=\s*(\{.*?\})\s*;\s*', resp.text, re.DOTALL)
            if not match:
                # Pattern 2: Greedily capture from assignment until a potential end of script or next statement
                match = re.search(r'__PRELOADED_STATE__\s*=\s*(\{.*?\})\s*(?:window\.|</script>|;|$)', resp.text, re.DOTALL)
            
            if match:
                json_blob = match.group(1).strip()
            
            if not json_blob:
                logging.debug("Qobuz Scraper: No JSON result blob found in HTML.")
                return []
                
            try:
                state = json.loads(json_blob)
            except Exception as je:
                logging.debug(f"Qobuz Scraper: JSON parse error: {je}")
                # Try to clean up the blob (sometimes it has extra trailing data)
                try:
                    # Very basic "find the matching closing brace" logic
                    brace_count = 0
                    for i, char in enumerate(json_blob):
                        if char == '{': brace_count += 1
                        elif char == '}': brace_count -= 1
                        if brace_count == 0:
                            state = json.loads(json_blob[:i+1])
                            break
                    else: raise Exception("Incomplete JSON")
                except:
                    return []
            
            # Navigate the state object to find results
            items_raw = []
            
            # Search results can be in multiple places depending on the page layout
            # 1. search.responses (multi-type search)
            # 2. search.results (single-type search)
            # 3. album.data.tracks (direct redirect to album)
            
            s_field = state.get('search', {})
            
            # Check responses (usually keyed by a hash or query)
            responses = s_field.get('responses', {})
            for r_val in responses.values():
                if isinstance(r_val, dict) and q_type in r_val:
                    items_raw.extend(r_val[q_type].get('items', []))
                    
            # Check direct results
            r_field = s_field.get('results', {})
            if q_type in r_field:
                items_raw.extend(r_field[q_type].get('items', []))
            
            # Check redirected album content
            if not items_raw and q_type == 'tracks':
                a_data = state.get('album', {}).get('data', {})
                if a_data and 'tracks' in a_data:
                    items_raw.extend(a_data['tracks'].get('items', []))
            
            if not items_raw:
                logging.debug(f"Qobuz Scraper: Found no items for type {q_type}.")
                return []
                
            logging.debug(f"Qobuz Scraper: Successfully extracted {len(items_raw)} items.")
            
            # De-duplicate by ID
            seen = set()
            unique = []
            for it in items_raw:
                iid = str(it.get('id', ''))
                if iid and iid not in seen:
                    seen.add(iid)
                    unique.append(it)
            
            return self._format_search_items(unique[:limit], query_type)
            
        except Exception as e:
            logging.debug(f"Qobuz Scraper: Error: {e}")
            return []
