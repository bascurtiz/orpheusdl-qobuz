import hashlib
import time

from utils.utils import create_requests_session


class Qobuz:
    def __init__(self, app_id: str, app_secret: str, exception):
        self.api_base = 'https://www.qobuz.com/api.json/0.2/'
        self.app_id = str(app_id)
        self.app_secret = app_secret
        self._auth_token = None
        self.exception = exception

        # Create session with persistent headers — exactly like qobuz-dl
        self.s = create_requests_session()
        self.s.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'X-App-Id': self.app_id,
        })

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
            self.get_file_url('52151405', 5)
            return True
        except Exception:
            return False

    def api_call(self, epoint, params=None, post=False):
        """Generic API call matching the working qobuz-dl pattern."""
        if params is None:
            params = {}

        if post:
            r = self.s.post(self.api_base + epoint, data=params)
        else:
            r = self.s.get(self.api_base + epoint, params=params)

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
            'app_id': self.app_id,
            'query': query,
            'type': query_type + 's',
            'limit': str(limit),
        }
        return self.api_call('catalog/search', params)

    def get_file_url(self, track_id: str, quality_id=27):
        # Signed call — modern web web-player uses timezone-based secret for this specific call
        # We extracted the 'berlin' timezone secret directly from the live bundle.js and base64-decoded it
        timezone_secret = "abb21364945c0583309667d13ca3d93a"
        
        fmt_id = str(quality_id)
        unix = str(int(time.time()))
        
        # The specific pattern expected by the modern web API for signatures
        r_sig = f"trackgetFileUrlformat_id{fmt_id}intentstreamtrack_id{track_id}{unix}{timezone_secret}"
        request_sig = hashlib.md5(r_sig.encode('utf-8')).hexdigest()

        params = {
            'track_id': track_id,
            'format_id': fmt_id,
            'intent': 'stream',
            'request_ts': unix,
            'request_sig': request_sig,
        }
        return self.api_call('track/getFileUrl', params)

    def get_sample_url(self, track_id: str):
        """Get the sample/preview URL for a track."""
        try:
            result = self.get_file_url(track_id, 5)
            return result.get('url')
        except Exception:
            return None

    def get_track(self, track_id: str):
        return self.api_call('track/get', params={
            'app_id': self.app_id,
            'track_id': track_id,
        })

    def get_playlist(self, playlist_id: str, limit: int = 500, offset: int = 0):
        return self.api_call('playlist/get', params={
            'app_id': self.app_id,
            'playlist_id': playlist_id,
            'limit': str(limit),
            'offset': str(offset),
            'extra': 'tracks,subscribers,focusAll',
        })

    def get_album(self, album_id: str):
        return self.api_call('album/get', params={
            'app_id': self.app_id,
            'album_id': album_id,
            'extra': 'albumsFromSameArtist,focusAll',
        })

    def get_artist(self, artist_id: str):
        return self.api_call('artist/get', params={
            'app_id': self.app_id,
            'artist_id': artist_id,
            'extra': 'albums,playlists,tracks_appears_on,albums_with_last_release,focusAll',
            'limit': '1000',
            'offset': '0',
        })

    def get_label(self, label_id: str, limit: int = 500, offset: int = 0):
        """Fetch label metadata and albums."""
        return self.api_call('label/get', params={
            'app_id': self.app_id,
            'label_id': label_id,
            'extra': 'albums,focusAll',
            'limit': str(limit),
            'offset': str(offset),
        })
