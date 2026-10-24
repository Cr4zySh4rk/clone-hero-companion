"""Install all 27 cloud favourites through the real downloader and job queue."""
import io
from contextlib import ExitStack
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from chcompanion import api
from chcompanion.app import Companion
from chcompanion.library import Library
from chcompanion.sng import write_sng
from chcompanion.store import Store


class BatchTest(unittest.TestCase):
    def test_cloud_install_all(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with ExitStack() as stack:
                for module in ('paths', 'libmgr', 'cloud', 'playstats'):
                    stack.enter_context(mock.patch('chcompanion.' + module + '.config_dir', return_value=root))
                stack.enter_context(mock.patch('chcompanion.sources.install.cache_dir', return_value=root))
                c = Companion(root, Library([root / 'Songs']), Store(root / 'store.json'), use_youtube=False)
                c.cloud = types.SimpleNamespace(signed_in=True)
                c.libmgr.on_fav = None
                c.libmgr.fav_setlist_enabled = lambda: False
                c.settings = lambda: {'download_subdir': '', 'auto_process_downloads': False}
                rows, items, data = [], {}, {}
                for i in range(27):
                    # Alphabetic labels keep the fuzzy matcher from treating numbered
                    # titles as alternate versions of the same song.
                    label = chr(65 + i // 26) + chr(65 + i % 26)
                    item = dict(source='encore', artist=f'Artist {label}', name=f'Title {label}',
                                charter='Charter', md5=f'{i:032x}',
                                downloadUrl=f'https://files.enchor.us/{i}.sng')
                    items[f'k{i}'] = item
                    rows.append(dict(song_key=f'k{i}', artist=item['artist'], name=item['name'],
                                     charter='Charter', checksum='', source={}))
                    src = root / f'{i}.sng'
                    write_sng(str(src), {k: item[k] for k in ('artist', 'name', 'charter')},
                              {'notes.chart': f'[Song]\n{i}'.encode()})
                    data[item['downloadUrl']] = src.read_bytes()
                c.cloudsync.cloud_only = rows

                def open_response(url, timeout=60):
                    r = io.BytesIO(data[url])
                    r.headers = {'Content-Length': str(len(data[url])), 'Content-Type': 'application/octet-stream'}
                    r.geturl = lambda: url
                    return r

                stack.enter_context(mock.patch('chcompanion.sources.hosts._open', side_effect=open_response))
                stack.enter_context(mock.patch('chcompanion.cloud.resolve_favourite',
                                               side_effect=lambda r: items[r['song_key']]))
                c.jobs.start()
                try:
                    self.assertEqual(api._install_cloud_favs(c, {})['queued'], 27)
                    end = time.monotonic() + 20
                    while time.monotonic() < end:
                        snap = c.jobs.snapshot()
                        if sum(snap['counts'].get(k, 0) for k in ('done', 'failed', 'cancelled')) == 27:
                            break
                        time.sleep(.02)
                    self.assertEqual(snap['counts'], {'done': 27},
                                     [(j['title'], j['error']) for j in snap['jobs'] if j['error']])
                    self.assertEqual(len(list((root / 'Songs').glob('*.sng'))), 27)
                    c.library.scan()
                    c.libmgr._reindex()
                    self.assertEqual(sum(e['fav'] for e in c.libmgr.entries()), 27)
                finally:
                    with c.jobs.lock:
                        c.jobs.target_workers = 0
                        c.jobs.lock.notify_all()
                    for t in c.jobs.threads:
                        t.join(5)


if __name__ == '__main__':
    unittest.main()
