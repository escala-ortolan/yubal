# ruff: noqa: E501

"""Small browser dashboard for the fresh intake API.

It is deliberately only a client for /v1: it never receives, stores, or logs a
bearer token on the server. The operator enters a provisioned device UUID and
token into the browser for the current page session.
"""

from fastapi import APIRouter
from starlette.responses import HTMLResponse

router = APIRouter(include_in_schema=False)

DASHBOARD = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Yubal intake</title><style>
body{font:16px system-ui,sans-serif;max-width:780px;margin:3rem auto;padding:0 1rem;color:#1d2530}h1{margin-bottom:.2rem}section{border:1px solid #ccd3dc;border-radius:8px;padding:1rem;margin:1rem 0}label{display:block;margin:.65rem 0}input,select,button{font:inherit;padding:.45rem;width:100%;box-sizing:border-box}button{width:auto;cursor:pointer}pre{white-space:pre-wrap;word-break:break-word;background:#f4f6f8;padding:1rem;border-radius:6px}.hint{color:#536171}
</style></head><body>
<h1>Yubal intake</h1><p class="hint">Submit a YouTube video ID and check its state. The token stays in this browser page and is never sent anywhere except this server.</p>
<section><h2>Device credentials</h2>
<label>Device UUID <input id="device" autocomplete="off" placeholder="UUID printed by intake_devices.py provision"></label>
<label>Device token <input id="token" type="password" autocomplete="off" placeholder="Token printed once during provisioning"></label>
</section>
<section><h2>Submit one video</h2>
<label>Video ID <input id="video" maxlength="11" pattern="[A-Za-z0-9_-]{11}" placeholder="jNQXAC9IVRw"></label>
<button id="submit">Submit</button></section>
<section><h2>Check status</h2><button id="status">Refresh status</button><pre id="output">Ready.</pre></section>
<p class="hint"><a href="/docs">API documentation</a> · <a href="/v1/health">Health</a></p>
<script>
const out=document.querySelector('#output'),id=()=>document.querySelector('#video').value.trim(),headers=()=>({Authorization:'Bearer '+document.querySelector('#token').value.trim(),'Content-Type':'application/json'});
const show=x=>out.textContent=JSON.stringify(x,null,2);
async function request(path,options={}){const r=await fetch(path,{...options,headers:{...headers(),...(options.headers||{})}});const b=await r.json().catch(()=>({error:'invalid_response',message:'Server did not return JSON'}));show(b);return b}
document.querySelector('#submit').onclick=()=>request('/v1/intakes',{method:'POST',body:JSON.stringify({request_id:crypto.randomUUID(),device_id:document.querySelector('#device').value.trim(),mode:'manual_song',source_context:{kind:'song'},tracks:[{video_id:id(),position:0}]})});
document.querySelector('#status').onclick=()=>request('/v1/tracks/'+encodeURIComponent(id()));
</script></body></html>"""


@router.get("/", response_class=HTMLResponse)
def dashboard() -> str:
    """Serve the token-local browser dashboard."""
    return DASHBOARD
