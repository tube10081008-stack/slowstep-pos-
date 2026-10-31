/**
 * 매장 PIN 잠금 — POS·대시보드 공용.
 *
 * 서버가 PIN 토큰을 검증하므로(클라이언트 검사만으론 우회 가능) 이 화면은
 * "입력 UI + 토큰 보관" 역할만 한다. 토큰은 localStorage에 저장돼 같은 기기에서는
 * 다시 묻지 않는다(서버 기준 30일). 고객 멤버십 페이지는 잠기지 않는다.
 *
 * 사용법:
 *   const H = StorePin.headers();          // fetch 헤더에 병합
 *   await StorePin.require();              // 인증될 때까지 대기(잠금화면 표시)
 *   StorePin.handle401(err);               // 401이면 잠금화면 재표시
 */
(function () {
  const KEY = "slowstep.store.token";
  const API_BASE = location.origin.includes(":5500")
    ? "http://localhost:8000"
    : location.origin;

  let resolveUnlock = null;

  const getToken = () => localStorage.getItem(KEY) || "";
  const setToken = (t) => localStorage.setItem(KEY, t);
  const clearToken = () => localStorage.removeItem(KEY);

  function injectStyles() {
    if (document.getElementById("pinStyles")) return;
    const css = `
    #pinGate{position:fixed;inset:0;z-index:9999;background:#f4f6f9;
      display:flex;align-items:center;justify-content:center;padding:20px;
      font-family:"Inter","Noto Sans KR",-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
      color:#161a25;}
    #pinGate.hidden{display:none}
    #pinGate .box{background:#fff;border:1px solid #e6e9ef;border-radius:20px;
      padding:32px 28px;width:100%;max-width:360px;text-align:center;
      box-shadow:0 1px 2px rgba(16,24,40,.04),0 12px 32px rgba(16,24,40,.08);}
    #pinGate .logo{width:46px;height:46px;border-radius:12px;margin:0 auto 14px;
      background:linear-gradient(135deg,#5b76ff,#3b5bff);color:#fff;display:flex;
      align-items:center;justify-content:center;font-weight:800;font-size:24px;
      box-shadow:0 6px 16px rgba(59,91,255,.3);}
    #pinGate h2{margin:0 0 4px;font-size:19px;font-weight:800;letter-spacing:-.01em}
    #pinGate .sub{color:#6b7280;font-size:13px;margin-bottom:22px}
    #pinGate .dots{display:flex;gap:12px;justify-content:center;margin-bottom:8px;min-height:18px}
    #pinGate .dot{width:14px;height:14px;border-radius:50%;background:#e6e9ef;transition:all .15s}
    #pinGate .dot.on{background:#3b5bff;transform:scale(1.1)}
    #pinGate .err{color:#ef4444;font-size:13px;min-height:20px;margin-bottom:10px;font-weight:500}
    #pinGate .pad{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}
    #pinGate .pad button{border:1px solid #e6e9ef;background:#fff;color:#161a25;
      border-radius:12px;padding:16px 0;font-size:20px;font-weight:600;cursor:pointer;
      font-family:inherit;transition:all .12s}
    #pinGate .pad button:hover{background:#eef2ff;border-color:#3b5bff}
    #pinGate .pad button:active{transform:translateY(1px)}
    #pinGate .pad button.wide{grid-column:span 1;font-size:14px;color:#6b7280}
    #pinGate .ok{grid-column:span 3;background:#3b5bff!important;color:#fff!important;
      border-color:#3b5bff!important;margin-top:2px;font-size:16px!important}
    #pinGate .ok:hover{background:#2846e6!important}
    #pinGate .ok:disabled{opacity:.45;cursor:default}
    #pinGate .foot{margin-top:16px;font-size:12px;color:#6b7280;line-height:1.6}
    `;
    const s = document.createElement("style");
    s.id = "pinStyles";
    s.textContent = css;
    document.head.appendChild(s);
  }

  function buildGate(label) {
    injectStyles();
    let el = document.getElementById("pinGate");
    if (el) { el.classList.remove("hidden"); return el; }

    el = document.createElement("div");
    el.id = "pinGate";
    el.innerHTML = `
      <div class="box">
        <div class="logo">S</div>
        <h2>슬로우스텝</h2>
        <div class="sub">${label} · 매장 PIN을 입력하세요</div>
        <div class="dots" id="pinDots"></div>
        <div class="err" id="pinErr"></div>
        <div class="pad" id="pinPad"></div>
        <div class="foot">이 기기는 한 번만 입력하면 기억됩니다.</div>
      </div>`;
    document.body.appendChild(el);

    const pad = el.querySelector("#pinPad");
    const mk = (t, cls, fn) => {
      const b = document.createElement("button");
      b.textContent = t;
      if (cls) b.className = cls;
      b.onclick = fn;
      pad.appendChild(b);
      return b;
    };
    for (let i = 1; i <= 9; i++) mk(String(i), "", () => push(String(i)));
    mk("지움", "wide", () => { buf = ""; paint(); });
    mk("0", "", () => push("0"));
    mk("←", "wide", () => { buf = buf.slice(0, -1); paint(); });
    okBtn = mk("확인", "ok", submit);

    document.addEventListener("keydown", onKey);
    return el;
  }

  let buf = "";
  let okBtn = null;
  let busy = false;

  function onKey(e) {
    const gate = document.getElementById("pinGate");
    if (!gate || gate.classList.contains("hidden")) return;
    if (/^[0-9]$/.test(e.key)) { push(e.key); e.preventDefault(); }
    else if (e.key === "Backspace") { buf = buf.slice(0, -1); paint(); e.preventDefault(); }
    else if (e.key === "Enter") { submit(); e.preventDefault(); }
  }

  function push(d) {
    if (buf.length >= 12) return;
    buf += d;
    paint();
  }

  function paint() {
    const dots = document.getElementById("pinDots");
    if (!dots) return;
    dots.innerHTML = Array.from({ length: Math.max(4, buf.length) },
      (_, i) => `<span class="dot ${i < buf.length ? "on" : ""}"></span>`).join("");
    if (okBtn) okBtn.disabled = buf.length === 0 || busy;
  }

  function showErr(m) {
    const e = document.getElementById("pinErr");
    if (e) e.textContent = m || "";
  }

  async function submit() {
    if (busy || !buf) return;
    busy = true;
    if (okBtn) { okBtn.disabled = true; okBtn.textContent = "확인 중…"; }
    showErr("");
    try {
      const r = await fetch(API_BASE + "/api/v1/auth/pin", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ pin: buf }),
      });
      const b = await r.json().catch(() => ({}));
      if (r.ok && b.token) {
        setToken(b.token);
        buf = "";
        const gate = document.getElementById("pinGate");
        if (gate) gate.classList.add("hidden");
        showErr("");
        if (resolveUnlock) { const f = resolveUnlock; resolveUnlock = null; f(); }
      } else {
        buf = "";
        showErr(b.detail || "PIN이 올바르지 않습니다.");
      }
    } catch (err) {
      showErr("서버에 연결할 수 없습니다.");
    } finally {
      busy = false;
      if (okBtn) okBtn.textContent = "확인";
      paint();
    }
  }

  async function verify() {
    try {
      const r = await fetch(API_BASE + "/api/v1/auth/pin", { headers: headers() });
      const b = await r.json().catch(() => ({}));
      return !!b.authorized;
    } catch (e) {
      return false; // 서버 불통 — 잠금 유지
    }
  }

  function headers() {
    const t = getToken();
    return t ? { "X-Store-Token": t } : {};
  }

  function show(label) {
    buf = "";
    buildGate(label || "매장 전용");
    paint();
    return new Promise((res) => { resolveUnlock = res; });
  }

  /** 인증될 때까지 잠금화면을 띄우고 대기. */
  async function require(label) {
    if (getToken() && (await verify())) return;
    clearToken();
    await show(label);
  }

  /** 401 응답이면 잠금화면을 다시 띄운다. true면 재시도 권장. */
  async function handle401(err, label) {
    const msg = String((err && err.message) || err || "");
    if (!/401|인증/.test(msg)) return false;
    clearToken();
    await show(label);
    return true;
  }

  function lock() {
    clearToken();
    location.reload();
  }

  window.StorePin = { require, headers, handle401, lock, show, token: getToken };
})();
