"""Account playback state shared by the web app and native clients."""
import re
import time

import podcasts


VIDEO = re.compile(r'(?:[A-Za-z0-9_-]{11}|france-tv-[A-Za-z0-9_-]{1,80})')
MUTATION = re.compile(r'[0-9a-fA-F-]{36}')


def episode_id(key):
    if not isinstance(key, str) or not podcasts.ID.fullmatch(key):
        raise ValueError('Invalid episode.')
    return key


def change(state, body):
    operation = body.get('operation')
    if operation == 'speed':
        rate = body.get('rate')
        if type(rate) not in (int, float) or not .75 <= rate <= 2:
            raise ValueError('Invalid playback speed.')
        stamp = podcasts.timestamp(body.get('updatedAt', time.time()))
        settings = state.setdefault('playbackPreferences', {})
        if stamp >= settings.get('speedAt', -1):
            settings.update(speed=rate, speedAt=stamp)
        return
    if operation == 'reset':
        ids = body.get('ids')
        if not isinstance(ids, list) or len(ids) > 300:
            raise ValueError('Select up to 300 episodes.')
        value = {'position': 0, 'finished': False,
                 'updatedAt': podcasts.timestamp(body.get('updatedAt', time.time()))}
        for key in ids:
            podcasts.change_progress(state, {'id': key, 'progress': value})
        return
    key = episode_id(body.get('id'))
    if operation in ('save', 'unsave', 'importSaved'):
        saved = state.setdefault('savedEpisodes', {})
        if operation == 'importSaved' and key in saved:
            return
        if len(saved) >= 100_000 and key not in saved:
            raise ValueError('Too many saved episodes.')
        saved[key] = None if operation == 'unsave' else (
            podcasts.timestamp(body.get('savedAt')) if operation == 'importSaved' else time.time())
        return
    queue = state.setdefault('queue', [])
    if operation == 'remove':
        if key in queue:
            queue.remove(key)
    elif operation == 'next':
        if key in queue:
            queue.remove(key)
        if len(queue) >= 300:
            raise ValueError('The queue holds up to 300 episodes.')
        queue.insert(0, key)
    elif operation in ('up', 'down'):
        if key in queue:
            index = queue.index(key)
            target = index + (-1 if operation == 'up' else 1)
            if 0 <= target < len(queue):
                queue[index], queue[target] = queue[target], queue[index]
    else:
        raise ValueError('Unknown playback action.')


def apply(state, body):
    identifier = body.get('mutationID')
    if identifier is None:
        return change(state, body)
    if not isinstance(identifier, str) or not MUTATION.fullmatch(identifier):
        raise ValueError('Invalid mutation identifier.')
    applied = state.setdefault('listeningMutations', [])
    if identifier in applied:
        return
    change(state, body)
    applied.append(identifier)
    del applied[:-4000]


def update_video(state, identifier, body):
    videos = state.setdefault('videoPlayback', {})
    if len(videos) >= 100_000 and identifier not in videos:
        raise ValueError('Too many video positions.')
    video = videos.setdefault(identifier, {})
    if body.get('import') and video:
        return video
    stamp = podcasts.timestamp(body.get('updatedAt'))
    for field in ('position', 'audio', 'subtitle'):
        if field not in body or stamp < video.get(field + 'At', -1):
            continue
        value = body[field]
        if field == 'position':
            value = podcasts.timestamp(value)
            if value > 30 * 86400:
                raise ValueError('Invalid video position.')
        elif value is not None:
            value = podcasts.string(value, 1024)
        video[field] = value
        video[field + 'At'] = stamp
    return video
