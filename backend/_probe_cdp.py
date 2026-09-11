"""Read-only CDP probe #6: ancestor structure of the betting widgets."""
import asyncio
import json
import urllib.request

import websockets

WS = None


async def evaluate(expression: str) -> dict:
    async with websockets.connect(WS, max_size=2**24) as ws:
        await ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
                                  "params": {"expression": expression,
                                             "returnByValue": True}}))
        while True:
            msg = json.loads(await ws.recv())
            if msg.get("id") == 1:
                return msg.get("result", {})


DRIVER = """() => {
  const f = document.querySelector('iframe[src*="aviator-next"]');
  if (!f || !f.contentDocument || !f.contentDocument.body) return {err: 'no-aviator-next'};
  const d = f.contentDocument;
  const out = { blocks: [], payoutsParent: '', payoutsPrevSib: '', balParent: '' };
  d.querySelectorAll('.bet-block').forEach((b, i) => {
    if (out.blocks.length > 1) return;
    const chain = [];
    let n = b;
    while (n && n !== d.body) { chain.push((n.tagName||'').toLowerCase() + (n.className ? '.' + (n.className||'').toString().split(/\\s+/).join('.') : '')); n = n.parentElement; }
    const btn = b.querySelector('.btn-success.bet');
    const inp = b.querySelector('input');
    const path = (el) => { const p=[]; let m=el; while(m && m!==d.body){ p.push((m.tagName||'').toLowerCase()+(m.className?'.'+(m.className||'').toString().split(/\\s+/).join('.'):'')); m=m.parentElement;} return p.join(' < '); };
    out.blocks.push({
      idx: i,
      chain: chain.slice(0,6).join(' < '),
      btnText: btn ? (btn.textContent||'').trim() : null,
      btnDisabled: btn ? btn.disabled || btn.getAttribute('aria-disabled') : null,
      btnPath: btn ? path(btn) : null,
      inputPath: inp ? path(inp) : null,
      inputPh: inp ? inp.placeholder : null,
      presets: Array.from(b.querySelectorAll('.bet-opt')).map(x=>x.textContent.trim()),
      minus: !!b.querySelector('.minus'), plus: !!b.querySelector('.plus'),
      autoTab: Array.from(b.querySelectorAll('.tab')).map(x=>(x.textContent||'').trim()).join(','),
      betInputs: d.querySelectorAll('input').length
    });
  });
  const pb = d.querySelector('.payouts-block');
  if (pb) { out.payoutsParent = (pb.parentElement ? pb.parentElement.className.toString().slice(0,80) : ''); out.payoutsPrevSib = (pb.previousElementSibling ? pb.previousElementSibling.className.toString().slice(0,80): ''); }
  return out;
}"""


async def main() -> int:
    global WS
    targets = json.load(urllib.request.urlopen("http://127.0.0.1:9222/json", timeout=5))
    for t in targets:
        if t.get("type") == "iframe" and "aviaport" in t.get("url", ""):
            WS = t.get("webSocketDebuggerUrl")
            break
    if not WS:
        print("NO AVIAport TARGET")
        return 1
    res = await evaluate(f"({DRIVER})()")
    val = res.get("result", {}).get("value")
    print(json.dumps(val, indent=1)[:5000] if val is not None else json.dumps(res)[:500])
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

