import unicodedata
import re
from datetime import datetime
from urllib.parse import urlparse

from utils.models import *
from .qobuz_api import Qobuz


module_information = ModuleInformation(
    service_name = 'Qobuz',
    module_supported_modes = ModuleModes.download | ModuleModes.credits,
    global_settings = {'app_id': '798273057', 'app_secret': 'abb21364945c0583309667d13ca3d93a', 'quality_format': '{sample_rate}kHz {bit_depth}bit'},
    session_settings = {'username': '', 'password': '', 'user_id': '', 'auth_token': '', 'use_id_token': 'false'},
    session_storage_variables = ['token'],
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
        self.session = Qobuz(settings['app_id'], settings['app_secret'], module_controller.module_error) # TODO: get rid of this module_error thing
        # Use saved auth_token from settings (ID/Token mode) or from session storage (email/password login)
        self.session.auth_token = (
            module_controller.temporary_settings_controller.read('token')
            or settings.get('auth_token')
        )
        self.module_controller = module_controller

        # 5 = 320 kbps MP3, 6 = 16-bit FLAC, 7 = 24-bit / =< 96kHz FLAC, 27 =< 192 kHz FLAC
        self.quality_parse = {
            QualityEnum.MINIMUM: 5,
            QualityEnum.LOW: 5,
            QualityEnum.MEDIUM: 5,
            QualityEnum.HIGH: 5,
            QualityEnum.LOSSLESS: 6,
            QualityEnum.HIFI: 27
        }
        self.quality_tier = module_controller.orpheus_options.quality_tier
        self.quality_format = settings.get('quality_format')

    def _ensure_credentials(self):
        """Require valid user credentials before download/metadata that leads to download.
        Without this, only previews would be downloaded. Matches Spotify behavior: show
        what's missing and where to fill it in."""
        if getattr(self.session, 'auth_token', None):
            return
        settings = self.module_controller.module_settings
        username = (settings.get('username') or '').strip()
        password = (settings.get('password') or '').strip()
        user_id = (settings.get('user_id') or '').strip()
        auth_token = (settings.get('auth_token') or '').strip()
        has_email_pass = username and password
        has_id_token = user_id and auth_token
        if has_id_token:
            self.session.auth_token = auth_token
            self.module_controller.temporary_settings_controller.set('token', auth_token)
            return
        if has_email_pass:
            self.login(username, password)
            return
        error_msg = (
            "Qobuz credentials are missing in settings.json. "
            "Please fill in either username and password, or user_id and auth token. "
            "Use the OrpheusDL GUI Settings tab (Qobuz) or edit config/settings.json directly."
        )
        raise self.session.exception(error_msg)

    def login(self, email, password):
        settings = self.module_controller.module_settings
        user_id = (settings.get('user_id') or '').strip()
        auth_token = (settings.get('auth_token') or '').strip()
        # ID/Token mode: use saved auth_token, no login call
        if user_id and auth_token:
            self.session.auth_token = auth_token
            self.module_controller.temporary_settings_controller.set('token', auth_token)
            return True
        # Email/Password mode
        if not email or not password:
            raise self.session.exception(
                'Qobuz credentials are required. Please fill in your email and password in the settings. '
                'Alternatively, you can use ID and Token instead.'
            )
        try:
            token = self.session.login(email, password)
            self.session.auth_token = token
            self.module_controller.temporary_settings_controller.set('token', token)
        except self.session.exception as e:
            error_str = str(e)
            if "'username'" in error_str or "username" in error_str.lower():
                raise self.session.exception(
                    'Qobuz credentials are required. Please fill in your email and password in the settings. '
                    'Alternatively, you can use ID and Token instead.'
                )
            raise

    def get_track_info(self, track_id, quality_tier: QualityEnum, codec_options: CodecOptions, data={}):
        self._ensure_credentials()
        track_data = data[track_id] if track_id in data else self.session.get_track(track_id)
        album_data = track_data['album']

        quality_tier = self.quality_parse[quality_tier]

        main_artist = track_data.get('performer', album_data['artist'])
        artists = [
            unicodedata.normalize('NFKD', main_artist['name'])
            .encode('ascii', 'ignore')
            .decode('utf-8')
        ]

        # Filter MainArtist and FeaturedArtist from performers
        if track_data.get('performers'):
            performers = []
            for credit in track_data['performers'].split(' - '):
                contributor_role = credit.split(', ')[1:]
                contributor_name = credit.split(', ')[0]

                for contributor in ['MainArtist', 'FeaturedArtist', 'Artist']:
                    if contributor in contributor_role:
                        if contributor_name not in artists:
                            artists.append(contributor_name)
                        contributor_role.remove(contributor)

                if not contributor_role:
                    continue
                performers.append(f"{contributor_name}, {', '.join(contributor_role)}")
            track_data['performers'] = ' - '.join(performers)
        artists[0] = main_artist['name']

        tags = Tags(
            album_artist = album_data['artist']['name'],
            composer = track_data['composer']['name'] if 'composer' in track_data else None,
            release_date = album_data.get('release_date_original'),
            track_number = track_data['track_number'],
            total_tracks = album_data['tracks_count'],
            disc_number = track_data['media_number'],
            total_discs = album_data['media_count'],
            isrc = track_data.get('isrc'),
            upc = album_data.get('upc'),
            label = album_data.get('label').get('name') if album_data.get('label') else None,
            copyright = album_data.get('copyright'),
            genres = [album_data['genre']['name']],
        )

        stream_data = self.session.get_file_url(track_id, quality_tier)
        # uncompressed PCM bitrate calculation, not quite accurate for FLACs due to the up to 60% size improvement
        bitrate = 320
        if stream_data.get('format_id') in {6, 7, 27}:
            bitrate = int((stream_data['sampling_rate'] * 1000 * stream_data['bit_depth'] * 2) // 1000)
        elif not stream_data.get('format_id'):
            bitrate = stream_data.get('format_id')

        # track and album title fix to include version tag
        track_name = f"{track_data.get('work')} - " if track_data.get('work') else ""
        track_name += track_data.get('title').rstrip()
        track_name += f' ({track_data.get("version")})' if track_data.get("version") else ''

        album_name = album_data.get('title').rstrip()
        album_name += f' ({album_data.get("version")})' if album_data.get("version") else ''

        return TrackInfo(
            id = str(track_id),
            name = track_name,
            album_id = album_data['id'],
            album = album_name,
            artists = artists,
            artist_id = main_artist['id'],
            bit_depth = stream_data['bit_depth'],
            bitrate = bitrate,
            sample_rate = stream_data['sampling_rate'],
            release_year = int(album_data['release_date_original'].split('-')[0]),
            explicit = track_data['parental_warning'],
            cover_url = album_data['image']['large'].split('_')[0] + '_org.jpg',
            tags = tags,
            codec = CodecEnum.FLAC if stream_data.get('format_id') in {6, 7, 27} else CodecEnum.NONE if not stream_data.get('format_id') else CodecEnum.MP3,
            duration = track_data.get('duration'),
            credits_extra_kwargs = {'data': {track_id: track_data}},
            download_extra_kwargs = {'url': stream_data.get('url')},
            error=f'Track "{track_data["title"]}" is not streamable!' if not track_data['streamable'] else None
        )

    def get_track_download(self, url):
        return TrackDownloadInfo(download_type=DownloadEnum.URL, file_url=url)

    def get_album_info(self, album_id):
        self._ensure_credentials()
        album_data = self.session.get_album(album_id)
        booklet_url = album_data['goodies'][0]['url'] if 'goodies' in album_data and len(album_data['goodies']) != 0 else None

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

        return AlbumInfo(
            name = album_name,
            artist = album_data['artist']['name'],
            artist_id = album_data['artist']['id'],
            tracks = tracks,
            release_year = int(album_data['release_date_original'].split('-')[0]),
            explicit = album_data['parental_warning'],
            quality = self.quality_format.format(**quality_tags) if self.quality_format != '' else None,
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
            release_year = datetime.utcfromtimestamp(playlist_data['created_at']).strftime('%Y'),
            description = playlist_data.get('description'),
            duration = playlist_data.get('duration'),
            tracks = tracks,
            track_extra_kwargs = {'data': extra_kwargs}
        )

    def get_artist_info(self, artist_id, get_credited_albums):
        """
        Return artist info plus a list of album objects with metadata for GUI display.
        The downloader still works because it only requires that each album item expose an ID
        (either as a plain string, dict['id'], or object.id).
        """
        self._ensure_credentials()
        artist_data = self.session.get_artist(artist_id)
        albums_raw = (artist_data.get('albums') or {}).get('items') or []
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
                    release_year = int(str(release_date).split('-')[0])
                except (ValueError, TypeError, AttributeError):
                    release_year = None

            # Album cover image - mirror search() album logic
            cover_url = None
            image = album.get('image')
            if isinstance(image, dict):
                cover_url = image.get('small') or image.get('thumbnail') or image.get('large')

            # Duration in seconds (for GUI to format)
            duration = album.get('duration')

            # Quality / sampling info (matches album search "Additional" column)
            additional = None
            if 'maximum_sampling_rate' in album:
                sr = album.get('maximum_sampling_rate')
                bd = album.get('maximum_bit_depth')
                if sr and bd:
                    additional = f"{sr}kHz/{bd}bit"
                elif sr:
                    additional = f"{sr}kHz"

            albums_out.append({
                'id': album_id,
                'name': name,
                'artist': artist_name,
                'release_year': release_year,
                'cover_url': cover_url,
                'duration': duration,
                'additional': additional,
            })

        # Fallback: if we couldn't parse metadata, keep old behaviour (IDs only)
        if not albums_out:
            albums_out = [str(album['id']) for album in artist_data.get('albums', {}).get('items', [])]

        return ArtistInfo(
            name = artist_data['name'],
            albums = albums_out
        )

    def get_label_info(self, label_id: str, get_credited_albums: bool = True, **kwargs) -> ArtistInfo:
        """Return label metadata and albums as ArtistInfo (same shape as artist for download flow)."""
        self._ensure_credentials()
        label_data = self.session.get_label(label_id)
        label_name = label_data.get('name') or 'Unknown Label'
        albums_raw = (label_data.get('albums') or {}).get('items') or []
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
            release_year = int(str(release_date).split('-')[0]) if release_date else None
            cover_url = None
            image = album.get('image')
            if isinstance(image, dict):
                cover_url = image.get('small') or image.get('thumbnail') or image.get('large')
            duration = album.get('duration')
            additional = None
            if 'maximum_sampling_rate' in album:
                sr = album.get('maximum_sampling_rate')
                bd = album.get('maximum_bit_depth')
                if sr and bd:
                    additional = f"{sr}kHz/{bd}bit"
                elif sr:
                    additional = f"{sr}kHz"
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
        track_data = data[track_id] if track_id in data else self.session.get_track(track_id)
        track_contributors = track_data.get('performers')

        # Credits look like: {name}, {type1}, {type2} - {name2}, {type2}
        credits_dict = {}
        if track_contributors:
            for credit in track_contributors.split(' - '):
                contributor_role = credit.split(', ')[1:]
                contributor_name = credit.split(', ')[0]

                for role in contributor_role:
                    # Check if the dict contains no list, create one
                    if role not in credits_dict:
                        credits_dict[role] = []
                    # Now add the name to the type list
                    credits_dict[role].append(contributor_name)

        # Convert the dictionary back to a list of CreditsInfo
        return [CreditsInfo(k, v) for k, v in credits_dict.items()]

    def search(self, query_type: DownloadTypeEnum, query, track_info: TrackInfo = None, limit: int = 10):
        # Require valid session or credentials on every search so we show a clear message instead of allowing unauthenticated search (30s preview on download)
        settings = self.module_controller.module_settings
        if not getattr(self.session, 'auth_token', None):
            username = (settings.get('username') or '').strip()
            password = (settings.get('password') or '').strip()
            user_id = (settings.get('user_id') or '').strip()
            auth_token = (settings.get('auth_token') or '').strip()
            has_email_pass = username and password
            has_id_token = user_id and auth_token
            if not has_email_pass and not has_id_token:
                raise self.session.exception(
                    'Qobuz credentials are required. Please fill in your email and password in the settings. '
                    'Alternatively, you can use ID and Token instead.'
                )
            self.login(username, password)

        results = {}
        if track_info and track_info.tags.isrc:
            results = self.session.search(query_type.name, track_info.tags.isrc, limit)
        if not results:
            try:
                results = self.session.search(query_type.name, query, limit)
            except Exception:
                if query_type is DownloadTypeEnum.label:
                    return []  # catalog/search does not support type=labels; use Download tab with label URL
                raise

        result_key = query_type.name + 's'
        if result_key not in results or not results[result_key].get('items'):
            if query_type is DownloadTypeEnum.label:
                return []  # API returns no labels; use Download tab with label URL (e.g. play.qobuz.com/label/12444)
            items = []
        else:
            items = []
            for i in results[result_key]['items']:
                duration = None
                image_url = None
                preview_url = None
                additional = None
                playlist_track_count = None

                if query_type is DownloadTypeEnum.artist:
                    artists = None
                    year = None
                    # Artist image (use small for search thumbnails)
                    if i.get('image'):
                        image_url = i['image'].get('small') or i['image'].get('medium') or i['image'].get('large')
                elif query_type is DownloadTypeEnum.playlist:
                    artists = [i['owner']['name']]
                    year = datetime.utcfromtimestamp(i['created_at']).strftime('%Y')
                    duration = i['duration']
                    # Playlist track count in additional
                    playlist_track_count = i.get('tracks_count') or (i.get('tracks') or {}).get('total')
                    additional = [f"1 track" if playlist_track_count == 1 else f"{playlist_track_count} tracks"] if playlist_track_count is not None else None
                    # Playlist cover image
                    if i.get('images300'):
                        image_url = i['images300'][0] if i['images300'] else None
                    elif i.get('image_rectangle'):
                        image_url = i['image_rectangle'][0] if isinstance(i['image_rectangle'], list) else i['image_rectangle']
                elif query_type is DownloadTypeEnum.track:
                    # Handle missing 'performer' key safely
                    if 'performer' in i and 'name' in i['performer']:
                        artists = [i['performer']['name']]
                    elif 'album' in i and 'artist' in i['album'] and 'name' in i['album']['artist']:
                        artists = [i['album']['artist']['name']]
                    else:
                        artists = ['Unknown Artist']
                    year = int(i['album']['release_date_original'].split('-')[0])
                    duration = i['duration']
                    if i.get('album') and i['album'].get('image'):
                        image_url = i['album']['image'].get('small') or i['album']['image'].get('thumbnail') or i['album']['image'].get('large')
                    sample_field = i.get('sample')
                    preview_url = (
                        i.get('sample_url') or i.get('preview_url') or i.get('previewable_url') or
                        (sample_field.get('url') if isinstance(sample_field, dict) else sample_field)
                    )
                elif query_type is DownloadTypeEnum.album:
                    artists = [i['artist']['name']]
                    year = int(i['release_date_original'].split('-')[0])
                    duration = i['duration']
                    if i.get('image'):
                        image_url = i['image'].get('small') or i['image'].get('thumbnail') or i['image'].get('large')
                elif query_type is DownloadTypeEnum.label:
                    artists = []
                    year = None
                    duration = None
                    if i.get('image'):
                        image_url = i['image'].get('small') or i['image'].get('medium') or i['image'].get('large')
                else:
                    raise Exception('Query type is invalid')
                if query_type is DownloadTypeEnum.playlist and (playlist_track_count is None or playlist_track_count == 0):
                    continue
                name = i.get('name') or i.get('title')
                name += f" ({i.get('version')})" if i.get('version') else ''
                # additional: for playlist use track count (set in branch); for others use sampling rate when present
                additional_for_sr = (additional if (query_type is DownloadTypeEnum.playlist and additional is not None) else
                    ([f'{i["maximum_sampling_rate"]}kHz/{i["maximum_bit_depth"]}bit'] if "maximum_sampling_rate" in i else None))
                item = SearchResult(
                    name=name,
                    artists=artists,
                    year=year,
                    result_id=str(i['id']),
                    explicit=bool(i.get('parental_warning')),
                    additional=additional_for_sr,
                    duration=duration,
                    image_url=image_url,
                    preview_url=preview_url,
                    extra_kwargs={'data': {str(i['id']): i}} if query_type is DownloadTypeEnum.track else {}
                )
                items.append(item)

        return items
