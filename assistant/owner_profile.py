"""Load an optional local identity profile without following symlinks."""

import json
import os
from pathlib import Path
import re
import stat


def load_owner_profile(root: Path, bound_open_id: str, profile_path=None):
    """Read the explicit path or the project's default local profile."""
    path = Path(profile_path) if profile_path else Path('.local/owner-profile.json')
    if not path.is_absolute():
        path = root / path
    if not path.exists() and not path.is_symlink():
        return None
    if any(parent.is_symlink() for parent in path.parents):
        raise ValueError('Owner profile directory must not be a symlink')
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise ValueError('Owner profile file cannot be opened safely') from exc
    with os.fdopen(fd, 'r', encoding='utf-8') as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 8192:
            raise ValueError('Owner profile file must be a small regular file')
        try:
            profile = json.load(source)
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise ValueError('Owner profile JSON is invalid') from exc
    if not isinstance(profile, dict) or set(profile) - {'name', 'nickname', 'work_email', 'aliases'}:
        raise ValueError('Owner profile has unsupported fields')
    for field in ('name', 'nickname', 'work_email'):
        if field not in profile:
            continue
        value = profile[field]
        if (not isinstance(value, str) or len(value) > 200
                or any(ord(char) < 32 for char in value)):
            raise ValueError(f'Owner profile field {field} is invalid')
    if profile.get('work_email') and not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', profile['work_email']):
        raise ValueError('Owner profile work_email is invalid')
    aliases = profile.get('aliases', [])
    if (not isinstance(aliases, list) or len(aliases) > 30
            or any(not isinstance(value, str) or not value.strip() or len(value) > 40
                   or any(ord(char) < 32 for char in value) for value in aliases)):
        raise ValueError('Owner profile aliases are invalid')
    if 'aliases' in profile:
        profile['aliases'] = list(dict.fromkeys(value.strip() for value in aliases))
    return {'owner_open_id': bound_open_id, **profile}
