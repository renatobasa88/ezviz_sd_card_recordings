/*
 * EZVIZ Timeline Card for Home Assistant
 * Simplified: live view by default, playback panel on demand.
 * Includes: player zoom, fullscreen, PTZ, and dynamic entity panel.
 * Licensed under the MIT License.
 */
const CARD_TAG = 'ezviz-timeline-card';

class EzvizTimelineCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    this._hass = null;
    this._config = null;
    this._started = false;
    this._records = [];
    this._date = '';
    this._zoomHours = 24;
    this._winStart = 0;
    this._winEnd = 0;
    this._center = 0;
    this._activeRecord = null;
    this._waitSeq = 0;
    this._loading = false;
    this._drag = null;
    this._refreshTimer = null;
    this._tab = 'live';
    this._zoom = { scale: 1, tx: 0, ty: 0 };
    this._origControls = undefined;
    this._pointers = new Map();
    this._pinchStartDist = 0;
    this._pinchStartScale = 1;
    this._panStart = null;
    this._otherEntities = [];
    this._onDocFullscreenChange = null;
    this._onDocKeydown = null;
  }

  setConfig(config) {
    const camera = config.camera_entity || config.entity;
    const entryId = config.ezviz_entry_id;
    if (!camera) throw new Error('EZVIZ Timeline Card: camera_entity is required');
    if (!entryId) throw new Error('EZVIZ Timeline Card: ezviz_entry_id is required');

    const ptz = config.ptz && typeof config.ptz === 'object' ? {
      up: config.ptz.up || '',
      down: config.ptz.down || '',
      left: config.ptz.left || '',
      right: config.ptz.right || '',
    } : null;
    const other = Array.isArray(config.other)
      ? config.other.filter(o => o && o.entity).map(o => ({ entity: String(o.entity), name: o.name || String(o.entity) }))
      : [];

    this._config = {
      camera_entity: camera,
      ezviz_entry_id: String(entryId),
      title: config.title || 'EZVIZ Camera',
      subtitle: config.subtitle || '',
      theme: config.theme === 'light' ? 'light' : 'dark',
      accent_color: config.accent_color || '#56d4c6',
      default_date: config.default_date || '',
      window_hours: [6, 12, 24].includes(Number(config.window_hours)) ? Number(config.window_hours) : 24,
      refresh_seconds: Math.max(15, Number(config.refresh_seconds || 60)),
      ptz,
      other,
    };
    this._otherEntities = other.map(o => o.entity);
    this._zoomHours = this._config.window_hours;
    this._renderShell();
    if (this._hass && !this._started) this._start();
  }

  set hass(hass) {
    const oldHass = this._hass;
    this._hass = hass;
    if (!this._config) return;
    const player = this.shadowRoot.querySelector('#live-slot ha-hls-player, #live-slot ha-camera-stream');
    if (player) player.hass = hass;
    if (!this._started) { this._start(); return; }

    // Re-mount live player if the camera availability changed while on Live tab
    if (oldHass && this._tab === 'live') {
      const oldState = oldHass.states[this._config.camera_entity];
      const newState = hass.states[this._config.camera_entity];
      const oldOffline = !oldState || oldState.state === 'unavailable' || oldState.state === 'unknown';
      const newOffline = !newState || newState.state === 'unavailable' || newState.state === 'unknown';
      if (oldOffline !== newOffline) this._mountLivePlayer();
    }

    if (oldHass && this._tab === 'other' && this._otherEntities.length) {
      const changed = this._otherEntities.some(e => oldHass.states[e] !== hass.states[e]);
      if (changed) this._renderOtherTab();
    }
  }

  getCardSize() { return 6; }

  disconnectedCallback() {
    if (this._refreshTimer) clearInterval(this._refreshTimer);
    this._refreshTimer = null;
    this._waitSeq++;
    if (this._onDocFullscreenChange) document.removeEventListener('fullscreenchange', this._onDocFullscreenChange);
    if (this._onDocKeydown) document.removeEventListener('keydown', this._onDocKeydown);
  }

  async _start() {
    if (this._started || !this._hass || !this._config) return;
    this._started = true;
    const today = this._dateString(new Date());
    this._date = this._config.default_date || today;
    this._setWindowForDate(true);
    this._mountLivePlayer();
    this._wireControls();
    await this._loadDay();
    this._refreshTimer = setInterval(() => {
      if (this._date === this._dateString(new Date()) && !this._loading) this._loadDay();
    }, this._config.refresh_seconds * 1000);
  }

  _renderShell() {
    const hasPtz = !!(this._config.ptz && Object.values(this._config.ptz).some(v => v));
    const hasOther = this._config.other && this._config.other.length > 0;
    const tab = this._tab;
    this.shadowRoot.innerHTML = `
      <style>
        :host{display:block;color:var(--primary-text-color);font-family:var(--paper-font-body1_-_font-family,Roboto,Arial,sans-serif);--ez-accent:${this._config?.accent_color||'#56d4c6'};}
        *{box-sizing:border-box}button,input{font:inherit}button{cursor:pointer}
        .card{overflow:hidden;border-radius:18px;background:${this._config?.theme==='light'?'#f7f9fc':'#10151d'};color:${this._config?.theme==='light'?'#17212c':'#e8eef6'};border:1px solid ${this._config?.theme==='light'?'#dce3eb':'#26303d'};box-shadow:0 10px 32px rgba(0,0,0,.16)}
        .head{padding:12px 16px;border-bottom:1px solid ${this._config?.theme==='light'?'#e0e6ed':'#28313d'}}
        .title{font-weight:750;font-size:15px;letter-spacing:.1px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
        .player{position:relative;aspect-ratio:16/9;background:#05070a;overflow:hidden;display:flex;justify-content:center;align-items:center}
        .player:fullscreen{aspect-ratio:unset;width:100vw;height:100vh;background:#000;border-radius:0}
        .player-layer{position:absolute;inset:0;transition:transform .05s linear;will-change:transform}
        .player-layer.hidden{display:none}
        .player-layer ha-hls-player,.player-layer ha-camera-stream,.player-layer video{display:block;width:100%;height:100%;object-fit:contain;background:#05070a;border:0}
        .player-layer video{max-height:none;min-height:0}
        .placeholder{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;padding:22px;gap:10px;color:#aab7c7;background:#05070a}
        .placeholder svg{width:36px;height:36px;color:var(--ez-accent)}
        .placeholder.offline svg{color:#ff8f8f}
        .placeholder strong{color:#eef5fc;font-size:14px}
        .placeholder small{line-height:1.5;max-width:440px}
        .placeholder code{background:rgba(255,255,255,.06);padding:1px 5px;border-radius:4px;font-size:.9em}
        .player-ctrls{position:absolute;top:10px;right:10px;z-index:10;display:flex;gap:4px;background:rgba(5,9,14,.72);backdrop-filter:blur(10px);border-radius:10px;padding:4px;border:1px solid rgba(255,255,255,.08)}
        .pctrl{background:transparent;border:0;color:#f3f7fa;width:28px;height:28px;border-radius:6px;font-size:16px;display:grid;place-items:center;line-height:1;padding:0}
        .pctrl:hover{background:rgba(255,255,255,.16)}
        .pctrl svg{width:15px;height:15px;pointer-events:none}
        .zoom-hint{position:absolute;bottom:10px;left:10px;z-index:10;background:rgba(5,9,14,.72);backdrop-filter:blur(10px);color:#f3f7fa;padding:4px 8px;border-radius:6px;font-size:10px;font-weight:700;display:none;border:1px solid rgba(255,255,255,.08)}
        .zoom-hint.visible{display:block}
        .mode-bar{display:flex;align-items:center;gap:6px;padding:10px 12px;border-top:1px solid ${this._config?.theme==='light'?'#e0e6ed':'#28313d'};overflow-x:auto;scrollbar-width:none}
        .mode-bar::-webkit-scrollbar{display:none}
        .mode-btn{flex:1 0 auto;display:flex;align-items:center;justify-content:center;gap:6px;background:${this._config?.theme==='light'?'#e9eef4':'#1a222e'};color:inherit;border:0;border-radius:10px;padding:9px 14px;font-size:12px;font-weight:650;white-space:nowrap;transition:background .15s}
        .mode-btn:hover{background:rgba(127,149,170,.18)}
        .mode-btn.active{background:var(--ez-accent);color:#071b1a}
        .mode-btn svg{width:14px;height:14px;flex:none}
        .tab-content{border-top:1px solid ${this._config?.theme==='light'?'#e0e6ed':'#28313d'};display:none}
        .tab-content.visible{display:block}
        .tab-content:empty{display:none !important}
        /* Playback panel */
        .tools{display:flex;align-items:center;gap:8px;flex-wrap:wrap;padding:12px 16px 10px}
        .group{display:flex;align-items:center;gap:4px;background:${this._config?.theme==='light'?'#e9eef4':'#1a222e'};padding:4px;border-radius:10px}
        .tool{background:transparent;color:inherit;border:0;border-radius:7px;padding:7px 9px;font-size:11px;font-weight:650}
        .tool:hover{background:rgba(127,149,170,.18)}
        .tool.active{background:var(--ez-accent);color:#071b1a}
        .date{background:${this._config?.theme==='light'?'#fff':'#1a222e'};color:inherit;border:1px solid ${this._config?.theme==='light'?'#d8e0e8':'#303c4c'};border-radius:9px;padding:7px 9px;min-height:34px;color-scheme:${this._config?.theme==='light'?'light':'dark'}}
        .spacer{flex:1}
        .caption{font-size:11px;opacity:.65}
        .timeline-wrap{padding:0 16px 12px}
        .range-head{display:flex;align-items:center;gap:8px;font-size:11px;margin:0 1px 8px}
        .range-head strong{font-size:12px}
        .range-head .subrange{opacity:.6;margin-left:auto;font-variant-numeric:tabular-nums}
        .timeline{position:relative;height:70px;border-radius:10px;background:${this._config?.theme==='light'?'#e9eef4':'#1a222c'};border:1px solid ${this._config?.theme==='light'?'#d9e1e9':'#2c3746'};overflow:hidden;touch-action:pan-y;user-select:none;cursor:crosshair}
        .timeline:before{content:'';position:absolute;inset:0;background:repeating-linear-gradient(to right,transparent 0,transparent calc(12.5% - 1px),${this._config?.theme==='light'?'rgba(70,84,101,.13)':'rgba(180,199,220,.12)'} calc(12.5% - 1px),${this._config?.theme==='light'?'rgba(70,84,101,.13)':'rgba(180,199,220,.12)'} 12.5%);pointer-events:none}
        .rec{position:absolute;top:12px;height:38px;border-radius:4px;background:linear-gradient(180deg,color-mix(in srgb,var(--ez-accent) 96%,white 4%),color-mix(in srgb,var(--ez-accent) 62%,#07534c 38%));min-width:2px;box-shadow:0 0 0 1px rgba(0,0,0,.13) inset;opacity:.92;cursor:pointer;z-index:1}
        .rec:hover{filter:brightness(1.22);z-index:3}
        .rec.uncached{background:repeating-linear-gradient(135deg,#667482 0 5px,#465362 5px 10px);opacity:.7}
        .rec.selected{outline:2px solid #f8fbff;outline-offset:1px;z-index:4}
        .axis{display:flex;justify-content:space-between;font-size:9px;opacity:.62;margin:6px 2px 0;font-variant-numeric:tabular-nums}
        .needle{position:absolute;top:0;bottom:0;width:2px;background:#ff6f79;z-index:5;box-shadow:0 0 9px rgba(255,111,121,.4);pointer-events:none}
        .needle:before{content:'';position:absolute;top:0;left:-3px;width:8px;height:8px;background:#ff6f79;border-radius:50%}
        .timeline-foot{display:flex;gap:10px;align-items:center;flex-wrap:wrap;padding:8px 2px 0;font-size:9px;opacity:.68}
        .legend{display:flex;align-items:center;gap:5px}
        .swatch{width:8px;height:8px;border-radius:2px;background:var(--ez-accent)}
        .swatch.grey{background:#667482}
        .note{margin-left:auto}
        .bottom{display:grid;grid-template-columns:minmax(0,1.15fr) minmax(0,1fr);border-top:1px solid ${this._config?.theme==='light'?'#e0e6ed':'#28313d'}}
        .section{min-width:0;padding:12px 14px}
        .section+ .section{border-left:1px solid ${this._config?.theme==='light'?'#e0e6ed':'#28313d'}}
        .section-title{display:flex;align-items:center;gap:8px;font-size:10px;font-weight:800;letter-spacing:.8px;text-transform:uppercase;opacity:.72;margin-bottom:8px}
        .count{margin-left:auto;background:${this._config?.theme==='light'?'#e6edf4':'#222d3a'};border-radius:999px;padding:2px 6px;font-size:9px;letter-spacing:0}
        .record-list{display:flex;flex-direction:column;gap:5px;max-height:200px;overflow:auto}
        .record{display:flex;align-items:center;gap:9px;width:100%;text-align:left;border:1px solid transparent;background:${this._config?.theme==='light'?'#fff':'#171f2a'};color:inherit;border-radius:9px;padding:7px 9px}
        .record:hover{border-color:color-mix(in srgb,var(--ez-accent) 60%,transparent)}
        .record.active{border-color:var(--ez-accent);background:color-mix(in srgb,var(--ez-accent) 10%,transparent)}
        .rec-icon{width:26px;height:26px;display:grid;place-items:center;background:color-mix(in srgb,var(--ez-accent) 15%,transparent);border-radius:7px;color:var(--ez-accent);flex:none}
        .rec-icon svg{width:14px;height:14px}
        .rec-text{min-width:0;flex:1}
        .rec-time{font-size:11px;font-weight:700;font-variant-numeric:tabular-nums}
        .rec-meta{font-size:9px;opacity:.6;margin-top:2px}
        .rec-state{font-size:9px;opacity:.6;white-space:nowrap}
        .empty{border:1px dashed ${this._config?.theme==='light'?'#cfd9e3':'#344252'};border-radius:9px;padding:14px;text-align:center;font-size:11px;line-height:1.5;opacity:.7}
        .message{font-size:11px;line-height:1.5;color:#ffd98d;white-space:pre-wrap;margin-top:8px}
        /* PTZ panel */
        .ptz-panel{padding:16px;display:flex;flex-direction:column;align-items:center;gap:12px}
        .ptz-dpad{display:grid;grid-template-columns:repeat(3,56px);grid-template-rows:repeat(3,56px);gap:6px}
        .ptz-btn{background:${this._config?.theme==='light'?'#fff':'#1a222e'};color:inherit;border:1px solid ${this._config?.theme==='light'?'#d8e0e8':'#303c4c'};border-radius:12px;font-size:20px;font-weight:700;display:grid;place-items:center;transition:all .1s}
        .ptz-btn:hover{background:var(--ez-accent);color:#071b1a;border-color:var(--ez-accent)}
        .ptz-btn:active{transform:scale(.94)}
        .ptz-btn:disabled{opacity:.25;cursor:not-allowed;background:transparent}
        .ptz-up{grid-area:1/2}.ptz-left{grid-area:2/1}.ptz-center{grid-area:2/2;font-size:9px;letter-spacing:1px;font-weight:800;opacity:.4;cursor:default}.ptz-right{grid-area:2/3}.ptz-down{grid-area:3/2}
        .ptz-hint{font-size:10px;opacity:.55;text-align:center}
        /* Other panel */
        .other-panel{padding:8px}
        .other-list{display:flex;flex-direction:column;gap:6px}
        .other-item{display:flex;align-items:center;gap:12px;padding:10px 12px;background:${this._config?.theme==='light'?'#fff':'#171f2a'};border:1px solid ${this._config?.theme==='light'?'#e0e6ed':'#28313d'};border-radius:10px}
        .other-text{min-width:0;flex:1}
        .other-name{font-size:12px;font-weight:700;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
        .other-state{font-size:10px;opacity:.65;margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
        .toggle{position:relative;width:42px;height:24px;border-radius:12px;background:${this._config?.theme==='light'?'#cbd5e0':'#303c4c'};border:0;flex:none;transition:background .15s;padding:0}
        .toggle:before{content:'';position:absolute;top:3px;left:3px;width:18px;height:18px;border-radius:50%;background:#fff;transition:left .15s;box-shadow:0 1px 3px rgba(0,0,0,.3)}
        .toggle.on{background:var(--ez-accent)}
        .toggle.on:before{left:21px}
        .other-btns{display:flex;gap:4px;flex:none}
        .siren-btn{background:${this._config?.theme==='light'?'#e9eef4':'#1a222e'};color:inherit;border:1px solid ${this._config?.theme==='light'?'#d8e0e8':'#303c4c'};border-radius:8px;padding:6px 10px;font-size:11px;font-weight:650}
        .siren-btn:hover{background:rgba(127,149,170,.18)}
        .siren-btn.active{background:var(--ez-accent);color:#071b1a;border-color:var(--ez-accent)}
        .other-btn{background:var(--ez-accent);color:#071b1a;border:0;border-radius:8px;padding:7px 12px;font-size:11px;font-weight:700;flex:none}
        .other-btn:hover{filter:brightness(1.1)}
        .other-btn:active{transform:scale(.97)}
        .other-empty{border:1px dashed ${this._config?.theme==='light'?'#cfd9e3':'#344252'};border-radius:9px;padding:16px;text-align:center;font-size:11px;opacity:.65}
        @media(max-width:650px){
          .bottom{grid-template-columns:1fr}
          .section+.section{border-left:0;border-top:1px solid ${this._config?.theme==='light'?'#e0e6ed':'#28313d'}}
          .head{padding:10px 12px}
          .tools{padding:10px 12px 8px}
          .timeline-wrap{padding:0 12px 10px}
          .section{padding:10px}
          .title{font-size:14px}
          .mode-btn{padding:8px 10px;font-size:11px}
          .date{max-width:130px}
        }
      </style>
      <div class="card">
        <div class="head">
          <div class="title">${this._escape(this._config?.subtitle || this._config?.title || 'EZVIZ Camera')}</div>
        </div>
        <div class="player" id="player">
          <div class="player-layer" id="live-layer">
            <div id="live-slot" style="position:absolute;inset:0"></div>
          </div>
          <div class="player-layer hidden" id="playback-layer">
            <div id="playback-slot" style="position:absolute;inset:0"></div>
          </div>
          <div class="player-ctrls">
            <button class="pctrl" data-player-action="zoom-out" title="Zoom out">−</button>
            <button class="pctrl" data-player-action="zoom-in" title="Zoom in">+</button>
            <button class="pctrl" data-player-action="fullscreen" title="Fullscreen">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M8 3H5a2 2 0 0 0-2 2v3M21 8V5a2 2 0 0 0-2-2h-3M3 16v3a2 2 0 0 0 2 2h3M16 21h3a2 2 0 0 0 2-2v-3"/></svg>
            </button>
          </div>
          <div class="zoom-hint" id="zoom-hint">100%</div>
        </div>
        <div class="mode-bar" id="mode-bar">
          <button class="mode-btn ${tab === 'live' ? 'active' : ''}" data-tab="live">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M5 3l14 9-14 9z"/></svg>Live
          </button>
          <button class="mode-btn ${tab === 'playback' ? 'active' : ''}" data-tab="playback">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18M9 21V9"/></svg>Playback
          </button>
          ${hasPtz ? `<button class="mode-btn ${tab === 'ptz' ? 'active' : ''}" data-tab="ptz">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2v20M2 12h20"/><circle cx="12" cy="12" r="4"/></svg>PTZ
          </button>` : ''}
          ${hasOther ? `<button class="mode-btn ${tab === 'other' ? 'active' : ''}" data-tab="other">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/><circle cx="5" cy="12" r="1"/></svg>Other
          </button>` : ''}
        </div>
        <div class="tab-content ${tab === 'playback' ? 'visible' : ''}" id="tab-playback">
          <div class="tools">
            <div class="group">
              <button class="tool" data-action="prev-day" title="Previous day">‹</button>
              <button class="tool" data-action="today">Today</button>
              <button class="tool" data-action="next-day" title="Next day">›</button>
            </div>
            <input class="date" type="date" id="date-input" aria-label="Recording date">
            <span class="spacer"></span>
            <div class="group" aria-label="Timeline zoom">
              <button class="tool" data-zoom="6">6h</button>
              <button class="tool" data-zoom="12">12h</button>
              <button class="tool active" data-zoom="24">24h</button>
            </div>
            <button class="tool" data-action="refresh" title="Refresh recordings">↻</button>
          </div>
          <div class="timeline-wrap">
            <div class="range-head">
              <strong>Recording timeline</strong>
              <span class="caption" id="timeline-state">Loading archive…</span>
              <span class="subrange" id="range-label"></span>
            </div>
            <div class="timeline" id="timeline" aria-label="Recording timeline"><div id="segments"></div></div>
            <div class="axis" id="axis"></div>
            <div class="timeline-foot">
              <span class="legend"><i class="swatch"></i>Available</span>
              <span class="legend"><i class="swatch grey"></i>Not cached</span>
              <span class="note">Click to play · Drag to pan</span>
            </div>
          </div>
          <div class="bottom">
            <div class="section">
              <div class="section-title">Recordings <span class="count" id="record-count">0 clips</span></div>
              <div class="record-list" id="record-list"><div class="empty">Loading EZVIZ archive…</div></div>
              <div id="status-message" class="message" hidden></div>
            </div>
            <div class="section">
              <div class="section-title">Selected</div>
              <div id="selected-info" class="empty">Choose a segment on the timeline.</div>
            </div>
          </div>
        </div>
        ${hasPtz ? `<div class="tab-content ${tab === 'ptz' ? 'visible' : ''}" id="tab-ptz">
          <div class="ptz-panel">
            <div class="ptz-dpad">
              <button class="ptz-btn ptz-up" data-ptz="up" title="Up">▲</button>
              <button class="ptz-btn ptz-left" data-ptz="left" title="Left">◀</button>
              <button class="ptz-btn ptz-center" disabled>PTZ</button>
              <button class="ptz-btn ptz-right" data-ptz="right" title="Right">▶</button>
              <button class="ptz-btn ptz-down" data-ptz="down" title="Down">▼</button>
            </div>
            <div class="ptz-hint">Tap a direction to move the camera</div>
          </div>
        </div>` : ''}
        ${hasOther ? `<div class="tab-content ${tab === 'other' ? 'visible' : ''}" id="tab-other">
          <div class="other-panel"><div class="other-list" id="other-list"></div></div>
        </div>` : ''}
      </div>`;
    this.shadowRoot.querySelector('#date-input').value = this._config?.default_date || this._dateString(new Date());
    this.shadowRoot.querySelectorAll('[data-zoom]').forEach(b => b.classList.toggle('active', Number(b.dataset.zoom) === (this._config?.window_hours || 24)));
    if (hasOther) this._renderOtherTab();
    this._setupDocumentListeners();
  }

  _setupDocumentListeners() {
    this._onDocFullscreenChange = () => {
      const player = this.shadowRoot?.querySelector('#player');
      if (!player) return;
      const isFs = document.fullscreenElement === player;
      if (!isFs) this._resetZoom();
    };
    document.addEventListener('fullscreenchange', this._onDocFullscreenChange);
    this._onDocKeydown = (ev) => {
      if (ev.key === 'Escape' && document.fullscreenElement) {
        // browser handles exit, our fullscreenchange listener resets zoom
      }
    };
    document.addEventListener('keydown', this._onDocKeydown);
  }

  _wireControls() {
    if (this._wired) return;
    this._wired = true;
    this.shadowRoot.addEventListener('click', (ev) => {
      const tabBtn = ev.target.closest('[data-tab]');
      if (tabBtn) { this._setTab(tabBtn.dataset.tab); return; }

      const pAction = ev.target.closest('[data-player-action]');
      if (pAction) {
        const action = pAction.dataset.playerAction;
        if (action === 'zoom-in') this._stepZoom(1.4);
        else if (action === 'zoom-out') this._stepZoom(1 / 1.4);
        else if (action === 'fullscreen') this._toggleFullscreen();
        return;
      }

      const ptzBtn = ev.target.closest('[data-ptz]');
      if (ptzBtn) {
        const dir = ptzBtn.dataset.ptz;
        const entityId = this._config.ptz?.[dir];
        if (entityId && this._hass) {
          this._hass.callService('button', 'press', { entity_id: entityId }).catch(err => {
            console.warn('PTZ press failed:', err);
          });
        }
        return;
      }

      const otherToggle = ev.target.closest('[data-other-toggle]');
      if (otherToggle) {
        const idx = Number(otherToggle.dataset.otherToggle);
        const item = this._config.other[idx];
        if (item && this._hass) {
          const domain = item.entity.split('.')[0];
          this._hass.callService(domain, 'toggle', { entity_id: item.entity }).catch(err => {
            console.warn('Toggle failed:', err);
          });
        }
        return;
      }

      const otherSiren = ev.target.closest('[data-other-siren]');
      if (otherSiren) {
        const idx = Number(otherSiren.dataset.otherIdx);
        const action = otherSiren.dataset.otherSiren;
        const item = this._config.other[idx];
        if (item && this._hass) {
          this._hass.callService('siren', action === 'on' ? 'turn_on' : 'turn_off', { entity_id: item.entity }).catch(err => {
            console.warn('Siren failed:', err);
          });
        }
        return;
      }

      const otherPress = ev.target.closest('[data-other-press]');
      if (otherPress) {
        const idx = Number(otherPress.dataset.otherPress);
        const item = this._config.other[idx];
        if (item && this._hass) {
          this._hass.callService('button', 'press', { entity_id: item.entity }).catch(err => {
            console.warn('Button press failed:', err);
          });
        }
        return;
      }

      const otherScript = ev.target.closest('[data-other-script]');
      if (otherScript) {
        const idx = Number(otherScript.dataset.otherScript);
        const item = this._config.other[idx];
        if (item && this._hass) {
          this._hass.callService('script', 'turn_on', { entity_id: item.entity }).catch(err => {
            console.warn('Script failed:', err);
          });
        }
        return;
      }

      const zoom = ev.target.closest('[data-zoom]');
      if (zoom) {
        this._zoomHours = Number(zoom.dataset.zoom);
        this._updateWindowAroundCenter();
        this._setZoomButtons();
        this._renderTimeline();
        return;
      }
      const recButton = ev.target.closest('[data-record-index]');
      if (recButton) {
        const rec = this._records[Number(recButton.dataset.recordIndex)];
        if (rec) this._playRecord(rec, 0);
        return;
      }
      const btn = ev.target.closest('[data-action]');
      if (!btn) return;
      switch (btn.dataset.action) {
        case 'prev-day': this._shiftDate(-1); break;
        case 'next-day': this._shiftDate(1); break;
        case 'today':
          this._date = this._dateString(new Date());
          this.shadowRoot.querySelector('#date-input').value = this._date;
          this._setWindowForDate(true);
          this._loadDay();
          break;
        case 'refresh': this._loadDay(); break;
      }
    });

    const dateInput = this.shadowRoot.querySelector('#date-input');
    dateInput.addEventListener('change', (ev) => {
      if (!ev.target.value) return;
      this._date = ev.target.value;
      this._setWindowForDate(true);
      this._loadDay();
    });

    const track = this.shadowRoot.querySelector('#timeline');
    track.addEventListener('pointerdown', (ev) => {
      if (ev.button !== undefined && ev.button !== 0) return;
      const rect = track.getBoundingClientRect();
      this._drag = { x: ev.clientX, start: this._winStart, end: this._winEnd, moved: false, rectWidth: rect.width };
      try { track.setPointerCapture(ev.pointerId); } catch (_) {}
    });
    track.addEventListener('pointermove', (ev) => {
      if (!this._drag) return;
      const dx = ev.clientX - this._drag.x;
      if (!this._drag.moved && Math.abs(dx) < 5) return;
      this._drag.moved = true;
      const span = this._drag.end - this._drag.start;
      const shift = Math.round(-dx / Math.max(1, this._drag.rectWidth) * span);
      const dayStart = this._dayStartEpoch();
      const dayEnd = dayStart + 86400;
      let start = this._drag.start + shift;
      let end = this._drag.end + shift;
      if (start < dayStart) { end += dayStart - start; start = dayStart; }
      if (end > dayEnd) { start -= end - dayEnd; end = dayEnd; }
      this._winStart = Math.max(dayStart, start);
      this._winEnd = Math.min(dayEnd, end);
      this._center = (this._winStart + this._winEnd) / 2;
      this._renderTimeline();
    });
    const finishDrag = (ev) => {
      if (!this._drag) return;
      const wasDrag = this._drag.moved;
      this._drag = null;
      if (!wasDrag) {
        const rect = track.getBoundingClientRect();
        const f = Math.min(1, Math.max(0, (ev.clientX - rect.left) / Math.max(1, rect.width)));
        const ts = this._winStart + f * (this._winEnd - this._winStart);
        const rec = this._records.find(r => ts >= r.start && ts <= Math.max(r.start + 1, r.end));
        if (rec) this._playRecord(rec, Math.max(0, Math.floor(ts - rec.start)));
        else this._toast(`No recording segment at ${this._formatTime(ts)}.`);
      } else {
        this._updateWindowLabel();
      }
    };
    track.addEventListener('pointerup', finishDrag);
    track.addEventListener('pointercancel', () => { this._drag = null; });

    this._setupZoomGestures();
  }

  /* ============ Zoom & Fullscreen ============ */

  _getActiveLayer() {
    return this._tab === 'playback'
      ? this.shadowRoot?.querySelector('#playback-layer')
      : this.shadowRoot?.querySelector('#live-layer');
  }

  _findVideo(root) {
    if (!root) return null;
    if (root.tagName === 'VIDEO') return root;
    if (root.shadowRoot) {
      const v = this._findVideo(root.shadowRoot);
      if (v) return v;
    }
    for (const child of (root.children || [])) {
      const v = this._findVideo(child);
      if (v) return v;
    }
    return null;
  }

  _setZoom(scale, tx, ty) {
    const layer = this._getActiveLayer();
    if (!layer) return;
    scale = Math.max(1, Math.min(6, scale));
    const W = layer.clientWidth || 1;
    const H = layer.clientHeight || 1;
    const maxTx = (W * (scale - 1)) / 2;
    const maxTy = (H * (scale - 1)) / 2;
    tx = Math.max(-maxTx, Math.min(maxTx, tx));
    ty = Math.max(-maxTy, Math.min(maxTy, ty));
    this._zoom = { scale, tx, ty };
    const player = this.shadowRoot.querySelector('#player');
    const hint = this.shadowRoot.querySelector('#zoom-hint');
    const video = this._findVideo(layer);
    if (scale < 1.01) {
      layer.style.transform = '';
      player?.classList.remove('zoomed');
      hint?.classList.remove('visible');
      if (video && this._origControls !== undefined) {
        try { video.controls = this._origControls; } catch (_) {}
      }
    } else {
      layer.style.transform = `translate(${tx}px, ${ty}px) scale(${scale})`;
      layer.style.transformOrigin = 'center center';
      player?.classList.add('zoomed');
      if (hint) {
        hint.classList.add('visible');
        hint.textContent = `${Math.round(scale * 100)}%`;
      }
      if (video) {
        if (this._origControls === undefined) this._origControls = video.controls;
        try { video.controls = false; } catch (_) {}
      }
    }
  }

  _stepZoom(factor) {
    const newScale = this._zoom.scale * factor;
    this._setZoom(newScale, this._zoom.tx, this._zoom.ty);
  }

  _resetZoom() {
    this._zoom = { scale: 1, tx: 0, ty: 0 };
    this._origControls = undefined;
    ['#live-layer', '#playback-layer'].forEach(sel => {
      const layer = this.shadowRoot?.querySelector(sel);
      if (!layer) return;
      layer.style.transform = '';
      const v = this._findVideo(layer);
      if (v) { try { v.controls = true; } catch (_) {} }
    });
    const hint = this.shadowRoot?.querySelector('#zoom-hint');
    if (hint) hint.classList.remove('visible');
    const player = this.shadowRoot?.querySelector('#player');
    if (player) player.classList.remove('zoomed', 'panning');
  }

  _toggleFullscreen() {
    const player = this.shadowRoot.querySelector('#player');
    if (!player) return;
    if (document.fullscreenElement === player) {
      document.exitFullscreen?.();
    } else {
      const req = player.requestFullscreen || player.webkitRequestFullscreen || player.mozRequestFullScreen || player.msRequestFullscreen;
      if (req) req.call(player).catch(err => console.warn('Fullscreen failed:', err));
    }
  }

  _setupZoomGestures() {
    const player = this.shadowRoot.querySelector('#player');
    if (!player) return;

    // Wheel zoom (Ctrl+wheel always zooms; plain wheel zooms only when already zoomed)
    player.addEventListener('wheel', (ev) => {
      if (this._zoom.scale <= 1.01 && !ev.ctrlKey) return;
      ev.preventDefault();
      const layer = this._getActiveLayer();
      if (!layer) return;
      const rect = layer.getBoundingClientRect();
      const cx = ev.clientX - rect.left - rect.width / 2;
      const cy = ev.clientY - rect.top - rect.height / 2;
      const factor = ev.deltaY < 0 ? 1.15 : 1 / 1.15;
      const newScale = Math.max(1, Math.min(6, this._zoom.scale * factor));
      const ratio = newScale / this._zoom.scale;
      const newTx = (this._zoom.tx - cx) * ratio + cx;
      const newTy = (this._zoom.ty - cy) * ratio + cy;
      this._setZoom(newScale, newTx, newTy);
    }, { passive: false });

    // Double-click / double-tap: reset zoom
    player.addEventListener('dblclick', (ev) => {
      ev.preventDefault();
      if (this._zoom.scale > 1.01) this._resetZoom();
    });

    // Pointer Events for pinch (2 fingers) and pan (when zoomed)
    player.addEventListener('pointerdown', (ev) => {
      if (ev.pointerType === 'mouse' && ev.button !== 0) return;
      this._pointers.set(ev.pointerId, { x: ev.clientX, y: ev.clientY });
      if (this._pointers.size === 2) {
        const pts = Array.from(this._pointers.values());
        this._pinchStartDist = Math.hypot(pts[0].x - pts[1].x, pts[0].y - pts[1].y) || 1;
        this._pinchStartScale = this._zoom.scale;
        this._panStart = null;
        try { player.setPointerCapture(ev.pointerId); } catch (_) {}
      } else if (this._pointers.size === 1 && this._zoom.scale > 1.01) {
        this._panStart = { x: ev.clientX, y: ev.clientY, tx: this._zoom.tx, ty: this._zoom.ty };
        player.classList.add('panning');
        try { player.setPointerCapture(ev.pointerId); } catch (_) {}
      }
    });

    player.addEventListener('pointermove', (ev) => {
      if (!this._pointers.has(ev.pointerId)) return;
      this._pointers.set(ev.pointerId, { x: ev.clientX, y: ev.clientY });
      if (this._pointers.size === 2 && this._pinchStartDist) {
        const pts = Array.from(this._pointers.values());
        const dist = Math.hypot(pts[0].x - pts[1].x, pts[0].y - pts[1].y) || 1;
        const newScale = Math.max(1, Math.min(6, this._pinchStartScale * (dist / this._pinchStartDist)));
        this._setZoom(newScale, this._zoom.tx, this._zoom.ty);
      } else if (this._pointers.size === 1 && this._panStart) {
        const dx = ev.clientX - this._panStart.x;
        const dy = ev.clientY - this._panStart.y;
        this._setZoom(this._zoom.scale, this._panStart.tx + dx, this._panStart.ty + dy);
      }
    });

    const endPointer = (ev) => {
      this._pointers.delete(ev.pointerId);
      if (this._pointers.size < 2) this._pinchStartDist = 0;
      if (this._pointers.size === 0) {
        this._panStart = null;
        player.classList.remove('panning');
      }
    };
    player.addEventListener('pointerup', endPointer);
    player.addEventListener('pointercancel', endPointer);
    player.addEventListener('pointerleave', (ev) => {
      if (this._pointers.size > 0) endPointer(ev);
    });
  }

  /* ============ Tab switching ============ */

  _setTab(tab) {
    if (this._tab === tab) return;
    const prevTab = this._tab;
    this._tab = tab;

    const liveLayer = this.shadowRoot.querySelector('#live-layer');
    const playbackLayer = this.shadowRoot.querySelector('#playback-layer');

    if (tab === 'playback') {
      liveLayer?.classList.add('hidden');
      playbackLayer?.classList.remove('hidden');
      if (prevTab !== 'playback') {
        if (this._activeRecord) this._playRecord(this._activeRecord, 0);
        else this._showPlaybackEmpty();
      }
    } else {
      liveLayer?.classList.remove('hidden');
      playbackLayer?.classList.add('hidden');
      const vid = playbackLayer?.querySelector('video');
      if (vid) try { vid.pause(); } catch (_) {}
      if (!this.shadowRoot.querySelector('#live-slot').children.length) {
        this._mountLivePlayer();
      }
    }

    this.shadowRoot.querySelectorAll('[data-tab]').forEach(b => {
      b.classList.toggle('active', b.dataset.tab === tab);
    });
    this.shadowRoot.querySelectorAll('.tab-content').forEach(el => {
      el.classList.toggle('visible', el.id === `tab-${tab}`);
    });

    this._resetZoom();
    if (tab === 'other') this._renderOtherTab();
  }

  _renderOtherTab() {
    const list = this.shadowRoot.querySelector('#other-list');
    if (!list) return;
    if (!this._config.other || !this._config.other.length) {
      list.innerHTML = '<div class="other-empty">No entities configured. Add `other:` to your card YAML.</div>';
      return;
    }
    list.innerHTML = this._config.other.map((item, i) => this._renderOtherItem(item, i)).join('');
  }

  _renderOtherItem(item, idx) {
    const entityId = item.entity;
    const name = item.name || entityId;
    const state = this._hass?.states?.[entityId];
    if (!state) {
      return `<div class="other-item">
        <div class="other-text">
          <div class="other-name">${this._escape(name)}</div>
          <div class="other-state" style="color:#ff8f8f">Entity not found</div>
        </div>
      </div>`;
    }
    const domain = entityId.split('.')[0];
    const s = state.state;
    const attrs = state.attributes || {};

    if (['switch', 'input_boolean', 'light', 'fan'].includes(domain)) {
      const isOn = s === 'on';
      return `<div class="other-item">
        <div class="other-text">
          <div class="other-name">${this._escape(name)}</div>
          <div class="other-state">${isOn ? 'On' : 'Off'}</div>
        </div>
        <button class="toggle ${isOn ? 'on' : ''}" data-other-toggle="${idx}" aria-label="Toggle"></button>
      </div>`;
    }

    if (domain === 'siren') {
      const isOn = s === 'on';
      return `<div class="other-item">
        <div class="other-text">
          <div class="other-name">${this._escape(name)}</div>
          <div class="other-state">${isOn ? 'On' : 'Off'}</div>
        </div>
        <div class="other-btns">
          <button class="siren-btn ${isOn ? 'active' : ''}" data-other-siren="on" data-other-idx="${idx}">🔊 On</button>
          <button class="siren-btn ${!isOn ? 'active' : ''}" data-other-siren="off" data-other-idx="${idx}">🔇 Off</button>
        </div>
      </div>`;
    }

    if (domain === 'button') {
      return `<div class="other-item">
        <div class="other-text">
          <div class="other-name">${this._escape(name)}</div>
          <div class="other-state">${this._escape(s)}</div>
        </div>
        <button class="other-btn" data-other-press="${idx}">Press</button>
      </div>`;
    }

    if (domain === 'script') {
      return `<div class="other-item">
        <div class="other-text">
          <div class="other-name">${this._escape(name)}</div>
          <div class="other-state">${this._escape(s)}</div>
        </div>
        <button class="other-btn" data-other-script="${idx}">Run</button>
      </div>`;
    }

    const unit = attrs.unit_of_measurement || '';
    return `<div class="other-item">
      <div class="other-text">
        <div class="other-name">${this._escape(name)}</div>
        <div class="other-state">${this._escape(s)}${unit ? ' ' + this._escape(unit) : ''}</div>
      </div>
    </div>`;
  }

  /* ============ Live player ============ */

  _mountLivePlayer() {
    const slot = this.shadowRoot.querySelector('#live-slot');
    if (!slot || !this._hass) return;
    slot.innerHTML = '';
    this._origControls = undefined;

    const entity = this._config.camera_entity;
    const state = this._hass.states[entity];

    // Show offline state when the camera entity is unavailable/unknown/missing
    const isOffline = !state || state.state === 'unavailable' || state.state === 'unknown';
    if (isOffline) {
      slot.innerHTML = `<div class="placeholder offline">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7">
          <path d="M2 2l20 20"/>
          <rect x="2" y="6" width="13" height="12" rx="2"/>
          <path d="M16 10l5-3v10l-5-3"/>
        </svg>
        <strong>Device Offline</strong>
        <small>Camera entity <code>${this._escape(entity)}</code> is unavailable.<br>The live view will resume automatically once the camera comes back online.</small>
      </div>`;
      return;
    }

    const useHls = !!customElements.get('ha-hls-player');
    const player = document.createElement(useHls ? 'ha-hls-player' : 'ha-camera-stream');
    player.hass = this._hass;
    if (useHls) {
      player.entityid = entity;
      player.autoPlay = true;
      player.playsInline = true;
    } else {
      const attrs = { ...(state.attributes || {}) };
      delete attrs.frontend_stream_type;
      player.stateObj = { ...state, attributes: attrs };
    }
    player.controls = true;
    player.muted = true;
    player.style.cssText = 'width:100%;height:100%;display:block;background:#05070a';
    slot.appendChild(player);
  }

  _showPlaybackEmpty() {
    const slot = this.shadowRoot.querySelector('#playback-slot');
    if (!slot) return;
    slot.innerHTML = `<div class="placeholder">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18M9 21V9"/></svg>
      <strong>Select a recording</strong>
      <small>Pick a segment on the timeline or a clip from the list below.</small>
    </div>`;
    const info = this.shadowRoot.querySelector('#selected-info');
    if (info) {
      info.className = 'empty';
      info.textContent = 'Choose a segment on the timeline or a recording from the list.';
    }
    this._showMessage('');
  }

  _showArchivePlayer(rec, offset) {
    const slot = this.shadowRoot.querySelector('#playback-slot');
    if (!slot) return;
    this._origControls = undefined;
    slot.innerHTML = `<div class="placeholder"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><circle cx="12" cy="12" r="9"/><path d="m10 8 6 4-6 4z"/></svg><strong>Preparing recording…</strong><small>${this._escape(rec.timeLabel)} · Resolve media</small></div>`;
    this._activeRecord = rec;
    const info = this.shadowRoot.querySelector('#selected-info');
    if (info) {
      info.className = 'empty';
      info.innerHTML = `<strong>${this._escape(rec.timeLabel)}</strong><br>${this._escape(this._date)}<br><span style="opacity:.65">${rec.cached ? 'Cached locally' : 'Download requested / checking cache'}</span>`;
    }
    this._renderTimeline();
    this._renderRecordList();
  }

  async _playRecord(rec, offsetSeconds = 0) {
    if (!this._hass || !rec) return;
    const seq = ++this._waitSeq;
    this._activeRecord = rec;
    this._showArchivePlayer(rec, offsetSeconds);
    this._showMessage('Requesting media from Home Assistant…');
    try {
      const media = await this._hass.callWS({
        type: 'media_source/resolve_media',
        media_content_id: rec.mediaContentId,
        expires: 3600,
      });
      if (seq !== this._waitSeq) return;
      await this._attachVideo(media.url, media.mime_type || 'video/mp4', rec, offsetSeconds, seq);
      this._showMessage('');
    } catch (err) {
      if (seq !== this._waitSeq) return;
      const details = String(err?.message || err || 'Unknown media resolution error');
      const isDownload = /download runs in the background|downloading|uncached|download.*background|check this notification/i.test(details);
      if (isDownload) {
        this._showMessage('Recording is not in cache yet. Download is running in the background on Home Assistant; the card will poll the cache automatically.');
        this._showWaitingPlayer(rec);
        this._waitForCache(rec, offsetSeconds, seq);
      } else {
        this._showMessage(`Failed to open recording: ${details}`);
        this._showErrorPlayer('Recording cannot be opened', details);
      }
    }
  }

  async _attachVideo(rawUrl, mimeType, rec, offsetSeconds, seq) {
    let url = rawUrl;
    if (typeof url !== 'string' || !url) throw new Error('Home Assistant did not return a media URL.');
    if (url.startsWith('/')) {
      try {
        const signed = await this._hass.callWS({ type: 'auth/sign_path', path: url, expires: 3600 });
        url = signed?.path || url;
      } catch (_) {}
    }
    if (seq !== this._waitSeq) return;
    const slot = this.shadowRoot.querySelector('#playback-slot');
    this._origControls = undefined;
    slot.innerHTML = `<video id="archive-video" controls playsinline preload="metadata"></video><div style="position:absolute;left:10px;bottom:10px;padding:5px 8px;background:rgba(0,0,0,.7);border-radius:7px;color:#fff;font-size:10px;pointer-events:none">${this._escape(rec.timeLabel)} · ${this._escape(mimeType)}</div>`;
    const video = slot.querySelector('#archive-video');
    video.style.cssText = 'width:100%;height:100%;object-fit:contain;background:#05070a';
    video.src = url;
    if (offsetSeconds > 0) {
      video.addEventListener('loadedmetadata', () => {
        try { video.currentTime = Math.min(offsetSeconds, Math.max(0, video.duration - 1)); } catch (_) {}
      }, { once: true });
    }
    video.addEventListener('error', () => {
      if (seq !== this._waitSeq) return;
      this._showMessage('Video resolved successfully, but the browser failed to play its format/codec. Check the H.264/H.265 codec of the EZVIZ recording.');
    }, { once: true });
    video.addEventListener('loadedmetadata', () => {
      const info = this.shadowRoot.querySelector('#selected-info');
      if (info) {
        info.className = 'empty';
        info.innerHTML = `<strong>${this._escape(rec.timeLabel)}</strong><br>${this._escape(this._date)}<br><span style="opacity:.65">Duration: ${this._fmtDuration(video.duration)}</span>`;
      }
    }, { once: true });
    try { await video.play(); } catch (_) {}
  }

  _showWaitingPlayer(rec) {
    const slot = this.shadowRoot.querySelector('#playback-slot');
    if (!slot) return;
    slot.innerHTML = `<div class="placeholder"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M12 3v10m0 0 4-4m-4 4-4-4"/><path d="M5 15v4a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-4"/></svg><strong>Downloading to HA cache</strong><small>${this._escape(rec.timeLabel)}<br>Do not close the card if you want to keep waiting. You can also press Refresh after the download success notification.</small></div>`;
  }

  _showErrorPlayer(title, details) {
    const slot = this.shadowRoot.querySelector('#playback-slot');
    if (!slot) return;
    slot.innerHTML = `<div class="placeholder"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><circle cx="12" cy="12" r="9"/><path d="M12 7v6m0 4h.01"/></svg><strong>${this._escape(title)}</strong><small>${this._escape(details)}</small><button id="try-again" style="background:var(--ez-accent);border:0;border-radius:8px;padding:8px 12px;color:#071b1a;font-weight:700">Try again</button></div>`;
    slot.querySelector('#try-again')?.addEventListener('click', () => this._playRecord(this._activeRecord, 0));
  }

  async _waitForCache(rec, offsetSeconds, seq) {
    for (let n = 0; n < 60; n++) {
      await new Promise(resolve => setTimeout(resolve, 5000));
      if (seq !== this._waitSeq || !this.isConnected) return;
      try {
        const tree = await this._hass.callWS({ type: 'media_source/browse_media', media_content_id: this._dayMediaId(this._date) });
        const found = (tree.children || []).find(item => this._extractKey(item.media_content_id || item.identifier) === rec.key);
        if (found && (/^✓\s*/.test(found.title || '') || found.thumbnail)) {
          rec.cached = true;
          rec.title = found.title;
          rec.mediaContentId = found.media_content_id || rec.mediaContentId;
          this._renderRecordList();
          const media = await this._hass.callWS({ type: 'media_source/resolve_media', media_content_id: rec.mediaContentId, expires: 3600 });
          if (seq !== this._waitSeq) return;
          await this._attachVideo(media.url, media.mime_type || 'video/mp4', rec, offsetSeconds, seq);
          this._showMessage('');
          return;
        }
      } catch (_) {}
      if (n % 3 === 2) this._showMessage(`Waiting for EZVIZ cache… (${Math.floor((n + 1) * 5 / 60)} minutes). Download status is in Home Assistant Notifications.`);
    }
    if (seq === this._waitSeq) {
      this._showMessage('Card stopped waiting after 5 minutes. Check the EZVIZ SD Card Recordings notification; if it succeeded, press Refresh then click the segment again.');
      this._showErrorPlayer('Waited for cache too long', 'Download may have failed or is still in progress. Check Notifications and the EZVIZ integration log.');
    }
  }

  async _loadDay() {
    if (!this._hass || !this._date || this._loading) return;
    this._loading = true;
    this._showMessage('');
    this._setTimelineState('Loading recordings…');
    try {
      const result = await this._hass.callWS({ type: 'media_source/browse_media', media_content_id: this._dayMediaId(this._date) });
      const children = Array.isArray(result?.children) ? result.children : [];
      this._records = children.map(item => this._normalizeRecord(item)).filter(Boolean).sort((a, b) => a.start - b.start);
      this._records.forEach((r, i) => { r.index = i; });
      this._renderTimeline();
      this._renderRecordList();
      this._setTimelineState(this._records.length ? `${this._records.length} archive segments` : 'No recordings in this day');
      if (!this._records.length) this._showMessage('No recordings were reported by the Media Source for this date. Make sure this date has an EZVIZ archive.');
    } catch (err) {
      this._records = [];
      this._renderTimeline();
      this._renderRecordList();
      this._setTimelineState('Could not load archive');
      this._showMessage(`Could not read the EZVIZ archive: ${String(err?.message || err)}. Make sure the EZVIZ integration is active and the entry ID is correct.`);
    } finally {
      this._loading = false;
    }
  }

  _normalizeRecord(item) {
    const mediaContentId = item.media_content_id || this._contentIdFromIdentifier(item.identifier);
    if (!mediaContentId) return null;
    const key = this._extractKey(mediaContentId);
    if (!key) return null;
    const parsed = key.match(/^(\d{6})_(\d{6})_([A-Za-z0-9]+)$/);
    let startClock, endClock;
    if (parsed) { startClock = parsed[1]; endClock = parsed[2]; }
    else {
      const clean = String(item.title || '').replace(/^✓\s*/, '');
      const m = clean.match(/(\d{2}):(\d{2}):(\d{2})\s*[–-]\s*(?:(\d{4}-\d{2}-\d{2})\s+)?(\d{2}):(\d{2}):(\d{2})/);
      if (!m) return null;
      startClock = `${m[1]}${m[2]}${m[3]}`; endClock = `${m[5]}${m[6]}${m[7]}`;
    }
    let start = this._wallTimeEpoch(this._date, startClock);
    let end = this._wallTimeEpoch(this._date, endClock);
    if (end < start) end += 86400;
    if (!Number.isFinite(start) || !Number.isFinite(end)) return null;
    const cached = /^✓\s*/.test(item.title || '') || !!item.thumbnail;
    return {
      key, start, end, cached, title: item.title || '',
      mediaContentId,
      timeLabel: `${this._clockLabel(start)} – ${this._clockLabel(end)}`,
      raw: item,
    };
  }

  _renderTimeline() {
    const track = this.shadowRoot.querySelector('#timeline');
    const segments = this.shadowRoot.querySelector('#segments');
    const axis = this.shadowRoot.querySelector('#axis');
    if (!track || !segments || !axis) return;
    const span = Math.max(1, this._winEnd - this._winStart);
    let html = '';
    const visible = this._records.filter(r => r.end >= this._winStart && r.start <= this._winEnd);
    visible.forEach(r => {
      const a = Math.max(r.start, this._winStart);
      const b = Math.min(Math.max(r.end, r.start + 1), this._winEnd);
      const left = ((a - this._winStart) / span) * 100;
      const width = Math.max(0.22, ((b - a) / span) * 100);
      const active = this._activeRecord?.key === r.key;
      html += `<div class="rec ${r.cached ? '' : 'uncached'} ${active ? 'selected' : ''}" data-segment-index="${r.index}" style="left:${left}%;width:${Math.min(width,100-left)}%" title="${this._escape(r.timeLabel)}${r.cached ? '' : ' · not cached yet'}"></div>`;
    });
    const now = Date.now() / 1000;
    const current = now >= this._winStart && now <= this._winEnd;
    if (current) html += `<div class="needle" style="left:${((now - this._winStart) / span) * 100}%"></div>`;
    segments.innerHTML = html;
    const marks = 6;
    let labels = '';
    for (let i = 0; i <= marks; i++) {
      const ts = this._winStart + span * i / marks;
      labels += `<span>${this._formatTime(ts, {hour:'2-digit',minute:'2-digit'})}</span>`;
    }
    axis.innerHTML = labels;
    this._updateWindowLabel();
  }

  _renderRecordList() {
    const list = this.shadowRoot.querySelector('#record-list');
    const count = this.shadowRoot.querySelector('#record-count');
    if (!list) return;
    if (count) count.textContent = `${this._records.length} clip${this._records.length === 1 ? '' : 's'}`;
    if (!this._records.length) {
      list.innerHTML = '<div class="empty">No recordings on this date.</div>';
      return;
    }
    const newest = [...this._records].sort((a,b) => b.start - a.start);
    list.innerHTML = newest.map(r => `<button class="record ${this._activeRecord?.key === r.key ? 'active' : ''}" data-record-index="${r.index}">
      <span class="rec-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="m9 6 10 6-10 6z"/></svg></span>
      <span class="rec-text"><span class="rec-time">${this._escape(r.timeLabel)}</span><span class="rec-meta">${this._escape(this._date)} · ${this._fmtDuration(Math.max(0,r.end-r.start))}</span></span>
      <span class="rec-state">${r.cached ? 'CACHED' : 'DOWNLOAD'}</span>
    </button>`).join('');
  }

  _showMessage(message) {
    const el = this.shadowRoot.querySelector('#status-message');
    if (!el) return;
    el.textContent = message || '';
    el.hidden = !message;
  }

  _setTimelineState(text) {
    const el = this.shadowRoot.querySelector('#timeline-state');
    if (el) el.textContent = text;
  }
  _toast(text) { this._showMessage(text); }

  _setWindowForDate(resetCenter) {
    const dayStart = this._dayStartEpoch();
    const now = Date.now() / 1000;
    const dayEnd = Math.min(dayStart + 86400, now);
    if (resetCenter || !this._center) this._center = this._date === this._dateString(new Date()) ? now : dayStart + 43200;
    this._updateWindowAroundCenter(dayStart, dayEnd);
  }

  _updateWindowAroundCenter(dayStart = this._dayStartEpoch(), dayEnd = dayStart + 86400) {
    const now = Date.now() / 1000;
    if (this._date === this._dateString(new Date())) dayEnd = Math.min(dayEnd, now);
    if (this._zoomHours === 24) {
      this._winStart = dayStart;
      this._winEnd = Math.max(dayStart + 1, dayEnd);
      return;
    }
    const span = this._zoomHours * 3600;
    let start = this._center - span / 2;
    let end = start + span;
    if (start < dayStart) { end += dayStart - start; start = dayStart; }
    if (end > dayEnd) { start -= end - dayEnd; end = dayEnd; }
    this._winStart = Math.max(dayStart, start);
    this._winEnd = Math.max(this._winStart + 1, Math.min(dayEnd, end));
  }

  _shiftDate(amount) {
    const [y,m,d] = this._date.split('-').map(Number);
    const next = new Date(Date.UTC(y, m - 1, d + amount, 12, 0, 0));
    const nextText = `${next.getUTCFullYear()}-${String(next.getUTCMonth()+1).padStart(2,'0')}-${String(next.getUTCDate()).padStart(2,'0')}`;
    if (nextText > this._dateString(new Date())) return;
    this._date = nextText;
    this.shadowRoot.querySelector('#date-input').value = this._date;
    this._setWindowForDate(true);
    this._loadDay();
  }

  _dayStartEpoch() { return this._wallTimeEpoch(this._date, '000000'); }
  _dayMediaId(dateStr) { return `media-source://ezviz_sd_card_recordings/${encodeURIComponent(this._config.ezviz_entry_id)}/day/${dateStr}`; }

  _contentIdFromIdentifier(identifier) {
    if (!identifier) return '';
    const id = String(identifier);
    return id.startsWith('media-source://') ? id : `media-source://ezviz_sd_card_recordings/${id.replace(/^\/+/, '')}`;
  }

  _extractKey(mediaId) {
    if (!mediaId) return '';
    const segments = String(mediaId).split('/');
    try { return decodeURIComponent(segments[segments.length - 1]); } catch (_) { return segments[segments.length - 1]; }
  }

  _wallTimeEpoch(dateText, clock) {
    const [y, mo, d] = dateText.split('-').map(Number);
    const hh = Number(clock.slice(0,2)), mm = Number(clock.slice(2,4)), ss = Number(clock.slice(4,6));
    const target = Date.UTC(y, mo - 1, d, hh, mm, ss);
    const zone = this._hass?.config?.time_zone || Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
    let guess = target;
    try {
      const fmt = new Intl.DateTimeFormat('en-CA', { timeZone: zone, year:'numeric', month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit', second:'2-digit', hourCycle:'h23' });
      for (let i = 0; i < 4; i++) {
        const parts = Object.fromEntries(fmt.formatToParts(new Date(guess)).filter(p => p.type !== 'literal').map(p => [p.type, Number(p.value)]));
        const observed = Date.UTC(parts.year, parts.month - 1, parts.day, parts.hour, parts.minute, parts.second);
        const delta = target - observed;
        guess += delta;
        if (delta === 0) break;
      }
      return Math.floor(guess / 1000);
    } catch (_) {
      return Math.floor(new Date(y, mo - 1, d, hh, mm, ss).getTime() / 1000);
    }
  }

  _dateString(date) {
    try {
      const zone = this._hass?.config?.time_zone || Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
      const parts = Object.fromEntries(new Intl.DateTimeFormat('en-CA', {timeZone:zone,year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(date).filter(p => p.type !== 'literal').map(p => [p.type, p.value]));
      return `${parts.year}-${parts.month}-${parts.day}`;
    } catch (_) { return `${date.getFullYear()}-${String(date.getMonth()+1).padStart(2,'0')}-${String(date.getDate()).padStart(2,'0')}`; }
  }
  _formatTime(epoch, options = {hour:'2-digit',minute:'2-digit',second:'2-digit'}) {
    const zone = this._hass?.config?.time_zone || Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
    try { return new Intl.DateTimeFormat('en-GB', {...options, timeZone:zone, hourCycle:'h23'}).format(new Date(epoch * 1000)); }
    catch (_) { return new Date(epoch * 1000).toLocaleTimeString('en-GB', options); }
  }
  _clockLabel(epoch) { return this._formatTime(epoch); }
  _fmtDuration(seconds) { if (!Number.isFinite(seconds)) return 'unknown'; const n = Math.max(0, Math.floor(seconds)); const h = Math.floor(n/3600), m=Math.floor(n%3600/60), s=n%60; return h ? `${h}:${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}` : `${m}:${String(s).padStart(2,'0')}`; }

  _updateWindowLabel() {
    const el = this.shadowRoot.querySelector('#range-label');
    if (!el) return;
    const fmt = t => this._formatTime(t, {hour:'2-digit',minute:'2-digit'});
    el.textContent = `${fmt(this._winStart)} – ${fmt(this._winEnd)}`;
  }

  _escape(value) {
    return String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
  }
}

if (!customElements.get(CARD_TAG)) customElements.define(CARD_TAG, EzvizTimelineCard);
window.customCards = window.customCards || [];
if (!window.customCards.some(card => card.type === CARD_TAG)) {
  window.customCards.push({ type: CARD_TAG, name: 'EZVIZ Timeline Card', description: 'Live camera with playback timeline, PTZ, and custom entity controls.' });
}
