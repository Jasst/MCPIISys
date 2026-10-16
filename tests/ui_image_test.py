import base64, json, struct, time, zlib, sys
from playwright.sync_api import sync_playwright

ROOT = '/tmp/proj'

def png(w, h, rgb):
    raw = b''.join(b'\x00' + bytes(rgb) * w for _ in range(h))
    def chunk(t, d): return struct.pack('>I', len(d)) + t + d + struct.pack('>I', zlib.crc32(t + d) & 0xffffffff)
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b'')

IMG1 = 'data:image/png;base64,' + base64.b64encode(png(300, 200, (200, 60, 60))).decode()
IMG2 = 'data:image/png;base64,' + base64.b64encode(png(300, 200, (60, 120, 220))).decode()
NOW = int(time.time() * 1000)
STATE = {
    'updated': 5,
    'sessions': [{'id': 's1', 'name': 'Тест', 'created': NOW, 'updated': NOW}],
    'messages': {'s1': [
        {'id': 'u1', 'role': 'user', 'text': 'вот фото', 'time': NOW, 'image': IMG1},
        {'id': 'b1', 'role': 'assistant', 'text': 'Вот картинка', 'time': NOW + 1, 'pending': False, 'images': [IMG2]},
    ]},
    'deleted_chats': [],
}

def route(r):
    url = r.request.url
    if url.startswith('http://app.test/static/'):
        p = ROOT + '/static/' + url.split('/static/', 1)[1].split('?')[0]
        ct = 'text/css' if p.endswith('.css') else 'application/javascript' if p.endswith('.js') else 'application/octet-stream'
        return r.fulfill(path=p, content_type=ct)
    if url.split('?')[0] in ('http://app.test/ai', 'http://app.test/ai/', 'http://app.test/ai.html'):
        return r.fulfill(path=ROOT + '/static/ai.html', content_type='text/html')
    if '/auth/status' in url:
        return r.fulfill(json={'required': False, 'authenticated': True, 'uid': 'tester'})
    if url.endswith('/ai/ui-state') and r.request.method == 'GET':
        return r.fulfill(json=STATE, headers={'ETag': '"v5"'})
    if '/ai/ui-state/version' in url:
        return r.fulfill(json={'updated': 5})
    if '/ai/gen-status' in url:
        return r.fulfill(json={'active': False, 'mine': False, 'may_watch': False, 'may_write': True, 'ttl_ms': 0})
    if url.startswith('http://app.test/'):
        return r.fulfill(status=404, json={})
    return r.abort()           # внешние CDN недоступны в песочнице

def run(name, ctx_args, mobile):
    results = {}
    errors = []
    with sync_playwright() as p:
        b = p.chromium.launch()
        ctx = b.new_context(**ctx_args)
        page = ctx.new_page()
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.route('**/*', route)
        page.goto('http://app.test/ai')
        page.wait_for_selector('.bubble img.gen', timeout=15000)
        page.wait_for_timeout(400)
        results['bubble_images'] = page.locator('.bubble img.gen').count()
        results['save_buttons'] = page.locator('.img-save').count()
        page.screenshot(path=f'/tmp/uitest/{name}_chat.png')

        # открыть первое изображение
        page.locator('.bubble img.gen').nth(1).click()
        page.wait_for_selector('.lightbox:not([hidden])', timeout=5000)
        page.wait_for_timeout(300)
        box = page.locator('.lightbox').bounding_box()
        results['lightbox_pos'] = page.evaluate("getComputedStyle(document.querySelector('.lightbox')).position")
        results['lightbox_covers_viewport'] = bool(box and box['width'] >= ctx_args['viewport']['width'] - 1 and box['height'] >= ctx_args['viewport']['height'] - 1)
        results['close_visible'] = page.locator('.lb-close').is_visible()
        results['save_visible'] = page.locator('.lb-save').is_visible()
        page.screenshot(path=f'/tmp/uitest/{name}_lightbox.png')

        # скачивание из лайтбокса
        with page.expect_download(timeout=5000) as dl:
            page.locator('.lb-save').click()
        results['download_name'] = dl.value.suggested_filename

        # двойной тап (мобильный) / двойной клик (ПК) — зум
        img = page.locator('.lightbox img')
        if mobile:
            page.touchscreen.tap(195, 400)
            page.wait_for_timeout(80)
            page.touchscreen.tap(195, 400)
        else:
            img.dblclick()
        page.wait_for_timeout(300)
        results['zoom_after_double'] = page.locator('.lightbox img').evaluate("e => e.style.transform")

        # закрыть Escape
        page.keyboard.press('Escape')
        page.wait_for_timeout(200)
        results['closed_by_escape'] = page.locator('.lightbox').is_hidden()
        results['page_errors'] = errors
        b.close()
    return results

desktop = run('desktop', dict(viewport={'width': 1280, 'height': 800}), mobile=False)
mobile = run('mobile', dict(viewport={'width': 390, 'height': 844}, is_mobile=True, has_touch=True, device_scale_factor=2), mobile=True)
print(json.dumps({'desktop': desktop, 'mobile': mobile}, ensure_ascii=False, indent=2))
