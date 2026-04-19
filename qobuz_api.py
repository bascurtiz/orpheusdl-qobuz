import hashlib
import time
import re
import base64
from collections import OrderedDict

from utils.utils import create_requests_session


class Qobuz:
    def __init__(self, app_id: str, app_secret: str, exception):
        self.api_base = 'https://www.qobuz.com/api.json/0.2/'
        self._app_id = str(app_id)
        self._app_secret = app_secret
        # Web player guest credentials for previews
        self.guest_app_id = '712109809'
        self.guest_app_secret = '589be88e4538daea11f509d29e4a23b1'
        self._auth_token = None
        self.exception = exception
        self._bundle_info = None

        # Create session with persistent headers — exactly like qobuz-dl
        self.s = create_requests_session()
        self.s.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'X-App-Id': self._app_id,
        })

    @property
    def app_id(self):
        return self._app_id

    @app_id.setter
    def app_id(self, value):
        self._app_id = str(value)
        self.s.headers.update({'X-App-Id': self._app_id})

    @property
    def app_secret(self):
        return self._app_secret

    @app_secret.setter
    def app_secret(self, value):
        self._app_secret = value

    @property
    def auth_token(self):
        return self._auth_token

    @auth_token.setter
    def auth_token(self, value):
        self._auth_token = value
        if value:
            self.s.headers.update({'X-User-Auth-Token': value})
        else:
            self.s.headers.pop('X-User-Auth-Token', None)


    def validate_token(self):
        """Check if current auth_token is valid by making a lightweight authenticated call."""
        if not self.auth_token:
            return False
        try:
            # user/get is the standard way to verify a session/retrieve user info
            self.api_call('user/get', signed=True)
            return True
        except Exception as e:
            # Only invalidate if it's explicitly an authentication error (401/400 with "User authentication is required")
            err_msg = str(e).lower()
            is_auth_error = '"code":401' in err_msg or "authentication" in err_msg or "invalid" in err_msg
            if is_auth_error:
                return False
            # For other errors (network timeout, etc.), assume the token might still be valid to avoid clearing it
            return True

    def get_bundle_info(self):
        """Scrapes app_id, secrets, and private_key from the Qobuz web player."""
        if self._bundle_info:
            return self._bundle_info

        base_url = "https://play.qobuz.com"
        logger_debug = lambda msg: print(f"DEBUG: {msg}") # Simple logging

        # 1. Get login page to find bundle.js URL
        r = self.s.get(f"{base_url}/login")
        r.raise_for_status()

        bundle_regex = re.compile(r'<script src="(/resources/\d+\.\d+\.\d+-[a-z]\d{3}/bundle\.js)"></script>')
        match = bundle_regex.search(r.text)
        if not match:
            raise self.exception("Could not find Qobuz bundle.js URL")

        bundle_url = base_url + match.group(1)

        # 2. Fetch bundle.js
        r = self.s.get(bundle_url)
        r.raise_for_status()
        bundle_text = r.text

        # 3. Extract info
        app_id_regex = re.compile(r'production:{api:{appId:"(?P<app_id>\d{9})",appSecret:"\w{32}"')
        private_key_regex = re.compile(r'privateKey:\s*"(?P<key>[A-Za-z0-9]{6,30})"')
        seed_timezone_regex = re.compile(r'[a-z]\.initialSeed\("(?P<seed>[\w=]+)",window\.utimezone\.(?P<timezone>[a-z]+)\)')
        info_extras_regex_template = r'name:"\w+/(?P<timezone>{timezones})",info:"(?P<info>[\w=]+)",extras:"(?P<extras>[\w=]+)"'

        app_id_match = app_id_regex.search(bundle_text)
        app_id = app_id_match.group("app_id") if app_id_match else self._app_id

        private_key_match = private_key_regex.search(bundle_text)
        private_key = private_key_match.group("key") if private_key_match else None

        # Extract secrets
        seed_matches = seed_timezone_regex.finditer(bundle_text)
        secrets = OrderedDict()
        for m in seed_matches:
            seed, timezone = m.group("seed", "timezone")
            secrets[timezone] = [seed]

        if len(secrets) >= 2:
            keypairs = list(secrets.items())
            secrets.move_to_end(keypairs[1][0], last=False)
            
            tz_pattern = "|".join([tz.capitalize() for tz in secrets])
            info_extras_matches = re.finditer(info_extras_regex_template.format(timezones=tz_pattern), bundle_text)
            for m in info_extras_matches:
                timezone, info, extras = m.group("timezone", "info", "extras")
                secrets[timezone.lower()] += [info, extras]

            for tz in secrets:
                raw_secret = "".join(secrets[tz])
                # Qobuz secrets are base64 encoded strings hidden in the bundle
                # We need to decode them correctly (strip the trailing 44 chars usually)
                try:
                    decoded = base64.standard_b64decode(raw_secret[:-44]).decode("utf-8")
                    secrets[tz] = decoded
                except:
                    pass

        self._bundle_info = {
            'app_id': app_id,
            'secrets': list(secrets.values()) if secrets else [],
            'private_key': private_key
        }
        return self._bundle_info

    def login_with_oauth_code(self, code, private_key=None):
        """Exchange OAuth code for a token and initialize session."""
        if not private_key:
            info = self.get_bundle_info()
            private_key = info['private_key']
            if not info['app_id'] == self._app_id:
                self.app_id = info['app_id']

        params = {
            "code": code,
            "private_key": private_key,
            "app_id": self._app_id,
        }
        
        # 1. Exchange code for token
        r = self.s.get(self.api_base + "oauth/callback", params=params)
        if r.status_code != 200:
            raise self.exception(f"OAuth callback failed: {r.text}")
        
        token = r.json().get("token")
        if not token:
            raise self.exception("No token in OAuth callback response")
        
        self.auth_token = token

        # 2. Finalize login (partner login)
        # This is CRITICAL for the token to be fully activated for library access
        r = self.s.post(
            self.api_base + "user/login",
            headers={"Content-Type": "text/plain;charset=UTF-8"},
            data="extra=partner"
        )
        if r.status_code != 200:
            raise self.exception(f"Partner login failed: {r.text}")
            
        return r.json()

    def _get_request_sig(self, epoint, params):
        """Web player signature pattern: {object}{method}{sorted_params}{timestamp}{secret}"""
        unix = str(int(time.time()))
        
        # Determine which secret to use
        secret = self.guest_app_secret if not self.auth_token else "abb21364945c0583309667d13ca3d93a"
        
        # Pattern for signature: alphabetically sorted key-values, then timestamp, then secret
        sig_base = epoint.replace('/', '')
        # Sort params by key and concatenate them. app_id is excluded if sent as header.
        # However, for guest signatures on web player, only specific parameters are included.
        # Based on reverse engineering, it signs all params sent in the query.
        sorted_keys = sorted([k for k in params.keys() if k != 'app_id'])
        for k in sorted_keys:
            sig_base += f"{k}{params[k]}"
        sig_base += f"{unix}{secret}"
        
        return unix, hashlib.md5(sig_base.encode('utf-8')).hexdigest()

    def api_call(self, epoint, params=None, post=False, signed=False):
        """Generic API call matching the working qobuz-dl pattern."""
        if params is None:
            params = {}
            
        # Select correct App ID based on session state: Guest ID for guests, Production ID for logged-in users
        # This ensures signatures match the App ID being used.
        if not self.auth_token:
            params['app_id'] = self.guest_app_id
        elif 'app_id' not in params:
            params['app_id'] = self.app_id

        if signed:
            unix, sig = self._get_request_sig(epoint, params)
            params['request_ts'] = unix
            params['request_sig'] = sig

        if post:
            r = self.s.post(self.api_base + epoint, data=params, timeout=15)
        else:
            r = self.s.get(self.api_base + epoint, params=params, timeout=15)

        if r.status_code not in [200, 201, 202]:
            raise self.exception(r.text)

        return r.json()

    def login(self, email: str, password: str):
        # If the password looks like a token (very long), use it directly
        if len(password) > 60:
            self.auth_token = password
            self.s.headers.update({'X-User-Auth-Token': self.auth_token})
            return self.auth_token

        # Standard login — use raw password with email
        data_plain = {
            'email': email,
            'password': password,
            'app_id': self.app_id,
        }
        r_plain = self.s.post(self.api_base + 'user/login', data=data_plain)

        if r_plain.status_code in [200, 201, 202]:
            result = r_plain.json()
        elif r_plain.status_code in [401, 400]:
            # Try with MD5-hashed password and username + extra:partner parameter as fallback
            data_md5 = {
                'username': email,
                'password': hashlib.md5(password.encode('utf-8')).hexdigest(),
                'extra': 'partner',
                'app_id': self.app_id,
            }
            r_md5 = self.s.post(self.api_base + 'user/login', data=data_md5)
            if r_md5.status_code not in [200, 201, 202]:
                raise self.exception(r_md5.text)
            result = r_md5.json()
        else:
            raise self.exception(r_plain.text)


        if 'user_auth_token' not in result:
            raise self.exception('Login failed: no auth token in response')

        if not result.get('user', {}).get('credential', {}).get('parameters'):
            raise self.exception("Free accounts are not eligible for downloading")

        self.auth_token = result['user_auth_token']
        self.s.headers.update({'X-User-Auth-Token': self.auth_token})
        return self.auth_token

    def search(self, query_type: str, query: str, limit: int = 10):
        # Standard call pattern from qobuz-dl: include app_id in params
        params = {
            'query': query,
            'type': query_type + 's',
            'limit': str(limit),
        }
        return self.api_call('catalog/search', params, signed=True)

    def get_file_url(self, track_id: str, quality_id=27):
        # Always use guest ID for quality_id=5 (previews) if not logged in
        is_guest_preview = not self.auth_token and str(quality_id) == '5'
        
        params = {
            'track_id': str(track_id),
            'format_id': str(quality_id),
            'intent': 'stream'
        }
        
        # Determine App ID
        target_app_id = self.guest_app_id if is_guest_preview else self.app_id

        # Update session header with the target app_id for this call
        orig_app_id_header = self.s.headers.get('X-App-Id')
        self.s.headers.update({'X-App-Id': target_app_id})

        try:
            # Generate signature (exclude app_id since it's now a header)
            unix, sig = self._get_request_sig('track/getFileUrl', params)
            
            # Parameters for the API call
            params['request_ts'] = unix
            params['request_sig'] = sig

            # Make the call
            return self.api_call('track/getFileUrl', params)
        finally:
            # Restore original header
            if orig_app_id_header: self.s.headers.update({'X-App-Id': orig_app_id_header})
            else: self.s.headers.pop('X-App-Id', None)

    def get_sample_url(self, track_id: str):
        """Get the sample/preview URL for a track."""
        try:
            # Set Referer for guest previews to bypass blocks
            orig_referer = self.s.headers.get('Referer')
            if not self.auth_token:
                self.s.headers.update({'Referer': 'https://open.qobuz.com/'})
            
            result = self.get_file_url(track_id, 5)
            
            # Clean up header
            if not self.auth_token:
                if orig_referer: self.s.headers.update({'Referer': orig_referer})
                else: self.s.headers.pop('Referer', None)
                
            return result.get('url')
        except Exception:
            return None

    def get_track(self, track_id: str):
        return self.api_call('track/get', params={
            'track_id': track_id,
        }, signed=True)

    def get_track_by_isrc(self, isrc: str):
        """Fetch track metadata by ISRC. This endpoint is often more 'guest-friendly' when signed."""
        try:
            return self.api_call('catalog/get', params={
                'track_isrc': isrc,
                'extra': 'focusAll',
            }, signed=True)
        except Exception:
            return None

    def get_playlist(self, playlist_id: str, limit: int = 500, offset: int = 0):
        return self.api_call('playlist/get', params={
            'playlist_id': playlist_id,
            'limit': str(limit),
            'offset': str(offset),
            'extra': 'tracks,subscribers,focusAll',
        }, signed=True)

    def get_album(self, album_id: str):
        return self.api_call('album/get', params={
            'album_id': album_id,
            'extra': 'albumsFromSameArtist,focusAll',
        }, signed=True)

    def get_artist(self, artist_id: str):
        return self.api_call('artist/get', params={
            'artist_id': artist_id,
            'extra': 'albums,playlists,tracks_appears_on,albums_with_last_release,focusAll',
            'limit': '1000',
            'offset': '0',
        }, signed=True)

    def get_label(self, label_id: str, limit: int = 500, offset: int = 0):
        """Fetch label metadata and albums."""
        return self.api_call('label/get', params={
            'label_id': label_id,
            'extra': 'albums,focusAll',
            'limit': str(limit),
            'offset': str(offset),
        }, signed=True)
