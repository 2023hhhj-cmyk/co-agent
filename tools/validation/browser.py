"""Render customer HTML in a networkless, read-only disposable container."""
import asyncio
import json
import os
import re
import uuid
from tools.deploy.customer_container import validate_docker_name

IMAGE = "coagent-browser-check:20261030"
_BROWSER_SCRIPT = r'''
import json,sys
from pathlib import Path
from urllib.parse import quote
from playwright.sync_api import sync_playwright
path=sys.argv[1]
result={"file":path,"visible_text":"","title":"","page_errors":[],"console_errors":[],"external_requests":[],"loaded":False}
with sync_playwright() as p:
    browser=p.chromium.launch(executable_path="/usr/bin/chromium",headless=True,args=["--no-sandbox","--disable-dev-shm-usage"])
    context=browser.new_context()
    page=context.new_page()
    page.on("pageerror",lambda e: result["page_errors"].append(str(e)[:300]))
    page.on("console",lambda m: result["console_errors"].append(m.text[:300]) if m.type=="error" else None)
    def route_request(route):
        url=route.request.url
        if url.startswith("file:///workspace/") or url.startswith("data:"):
            route.continue_()
        else:
            result["external_requests"].append(url[:300]); route.abort()
    page.route("**/*",route_request)
    try:
        page.goto("file:///workspace/"+quote(path,safe="/"),wait_until="load",timeout=15000)
        page.wait_for_timeout(500)
        result["visible_text"]=page.locator("body").inner_text(timeout=3000)[:5000]
        result["title"]=page.title()[:500]
        result["viewport_checks"]=[]
        for width,height in ((1280,720),(375,667)):
            page.set_viewport_size({"width":width,"height":height})
            page.wait_for_timeout(100)
            overflow=page.evaluate("() => document.documentElement.scrollWidth > window.innerWidth || document.body.scrollWidth > window.innerWidth")
            vertical=page.evaluate("() => document.documentElement.scrollHeight > window.innerHeight || document.body.scrollHeight > window.innerHeight")
            result["viewport_checks"].append({"width":width,"height":height,"horizontal_overflow":overflow,"vertical_overflow":vertical})
        result["script_count"]=page.locator("script").count()
        result["inline_event_handler_count"]=page.locator("*").evaluate_all("els => els.reduce((n,e) => n + [...e.attributes].filter(a => /^on/i.test(a.name)).length, 0)")
        result["javascript_url_count"]=page.locator("*").evaluate_all("els => els.reduce((n,e) => n + [...e.attributes].filter(a => /^(?:href|src|action)$/i.test(a.name) && /^\\s*javascript:/i.test(a.value)).length, 0)")
        result["loaded"]=True
    except Exception as e:
        result["load_error"]=str(e)[:500]
    finally:
        browser.close()
print(json.dumps(result,ensure_ascii=False))
'''

async def check_html(volume: str, path: str, published_root: str = "") -> dict:
    volume = validate_docker_name(volume)
    parts = path.split('/')
    if not path or path.startswith('/') or any(not x or x in ('.','..') or x.startswith('.') for x in parts) or not path.lower().endswith(('.html','.htm')):
        raise ValueError('invalid browser artifact path')
    if published_root:
        if (not re.fullmatch(r'/var/www/muzhu-ai/c/[a-z0-9_-]+/d[0-9a-f]{12}', published_root)
                or os.path.realpath(published_root) != published_root or not os.path.isdir(published_root)):
            raise ValueError('invalid published artifact root')
        mount = f'type=bind,source={published_root},target=/workspace,readonly'
    else:
        mount = f'type=volume,source={volume},target=/workspace,readonly'
    name = 'coagent-browser-' + uuid.uuid4().hex[:20]
    cmd = ['docker','run','--rm','--init','--pull=never','--name',name,
           '--network','none','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges',
           '--memory','768m','--cpus','1','--pids-limit','128','--shm-size','128m',
           '--tmpfs','/tmp:rw,nosuid,nodev,size=256m,mode=1777',
           '--mount',mount,
           '--entrypoint','python',IMAGE,'-c',_BROWSER_SCRIPT,path]
    proc = await asyncio.create_subprocess_exec(*cmd,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
    try:
        try:
            out,err = await asyncio.wait_for(proc.communicate(),timeout=45)
        except (asyncio.TimeoutError,asyncio.CancelledError):
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
            raise
        if proc.returncode:
            raise RuntimeError('browser container failed: '+err.decode(errors='replace')[-500:])
        facts=json.loads(out.decode('utf-8'))
        if not isinstance(facts,dict) or not facts.get('loaded'):
            raise RuntimeError('browser failed to load page')
        return facts
    finally:
        cleanup=await asyncio.create_subprocess_exec('docker','rm','-f',name,stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL)
        await cleanup.wait()
