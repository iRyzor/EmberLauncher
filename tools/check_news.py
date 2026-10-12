"""Check public STOVE news using an isolated Rust engine; no account/game access."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

root = Path(__file__).resolve().parents[1]
binary = root / 'src-tauri/engine' / ('stove-engine.exe' if sys.platform == 'win32' else 'stove-engine')
with tempfile.TemporaryDirectory(prefix='ember-news-check-') as folder:
    env = dict(os.environ)
    for kind in ('CONFIG', 'CACHE', 'DATA'):
        env[f'EMBER_{kind}_DIR'] = str(Path(folder) / kind.lower())
    process = subprocess.Popen([str(binary), '--root', str(Path(folder)/'absent')], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding='utf-8', env=env)
    def request(action, payload=None):
        process.stdin.write(json.dumps({'action': action, 'payload': payload})+'\n')
        process.stdin.flush()
        return json.loads(process.stdout.readline())
    def await_news(article_id=None):
        deadline = time.monotonic()+35
        payload = {'game_id': 'chaos-zero-nightmare'}
        if article_id:
            payload['article_id'] = article_id
        while time.monotonic() < deadline:
            response = request('news', payload)
            if not response.get('loading'):
                return response
            time.sleep(.15)
        raise AssertionError('News request timed out')
    try:
        request('news', {'game_id': 'chaos-zero-nightmare'})
        start = time.monotonic()
        state = request('state')
        latency = time.monotonic()-start
        assert latency < 1 and not state['connected'] and not state['game_installed']
        feed = await_news()
        items = feed['news']['items']
        assert items, feed.get('error')
        detail = await_news(items[0]['id'])
        article = detail['news']['article']
        assert article['content'] and article['id'] == items[0]['id']
        saved = list((Path(folder)/'cache/news').glob('*.json'))
        assert len(saved) == 2
        for file in saved:
            content = file.read_text(encoding='utf-8')
            assert all(key not in content for key in ('access_token', 'refresh_token', 'member_no'))
        assert not (Path(folder)/'absent').exists()
        print(json.dumps({'public_news_count': len(items), 'categories': sorted(set(v['category'] for v in items)), 'article_loaded': True, 'cache_files': len(saved), 'state_latency_ms': round(latency*1000), 'account_used': False}))
    finally:
        request('shutdown')
        process.wait(timeout=10)
