/*
 * Energy Trend chart - shared by the Console (index.html) and the History
 * page (history.html) so fixes land once.
 *
 * Draws solar / home / battery / grid kW (left axis, translucent fill to
 * zero) and battery level % (dashed, fixed 0-100 % right axis) from
 * /api/timeseries/trend points, with a hover crosshair and tooltip.
 * Battery kW is positive when discharging, grid kW positive when importing.
 *
 * Usage:
 *     const chart = EnergyTrend.create(canvas, tooltip, panel);
 *     chart.update(points, { t0, t1 });   // epoch seconds; null clears
 *     chart.toggle('grid_kw');            // show/hide a series
 *     chart.redraw();                     // e.g. after a resize
 *
 * The tooltip element is positioned inside `panel`, which must be
 * position: relative (or absolute). No dependencies; works offline.
 */
(function () {
    'use strict';

    const SERIES = [
        { key: 'solar_kw', label: 'Solar', color: '#f0c000', axis: 'kw' },
        { key: 'home_kw', label: 'Home', color: '#58a6ff', axis: 'kw' },
        { key: 'battery_kw', label: 'Battery', color: '#3fb950', axis: 'kw' },
        { key: 'grid_kw', label: 'Grid', color: '#8b949e', axis: 'kw' },
        { key: 'battery_level', label: 'Battery Level', color: '#3fb950', axis: 'pct' },
    ];
    const PAD = { l: 48, r: 44, t: 12, b: 24 };
    const pad2 = n => String(n).padStart(2, '0');

    function hoverTime(ts) {
        const d = new Date(ts * 1000);
        const h = d.getHours() % 12 || 12;
        const ap = d.getHours() >= 12 ? 'pm' : 'am';
        return `${d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })} ${h}:${pad2(d.getMinutes())}${ap}`;
    }

    function create(canvas, tooltip, panel, opts) {
        const hidden = new Set((opts && opts.hidden) || []);
        let points = null;
        let domain = null;
        let hover = null;

        function dom() {
            return domain || { t0: points[0].ts, t1: points[points.length - 1].ts };
        }

        function draw() {
            const rect = canvas.getBoundingClientRect();
            const dpr = window.devicePixelRatio || 1;
            canvas.width = Math.round(rect.width * dpr);
            canvas.height = Math.round(rect.height * dpr);
            const ctx = canvas.getContext('2d');
            ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
            const W = rect.width, H = rect.height;
            ctx.clearRect(0, 0, W, H);
            if (!points || points.length < 2 || W < 100) {
                // No drawable data - no stale tooltip either
                hover = null;
                tooltip.style.display = 'none';
                return;
            }

            const plotW = W - PAD.l - PAD.r, plotH = H - PAD.t - PAD.b;
            const { t0, t1 } = dom();
            const xSpan = (t1 - t0) || 1;
            const X = ts => PAD.l + ((ts - t0) / xSpan) * plotW;

            // kW scale across visible series, zero included
            let minKw = 0, maxKw = 0;
            for (const p of points) {
                for (const s of SERIES) {
                    if (s.axis !== 'kw' || hidden.has(s.key) || p[s.key] == null) continue;
                    minKw = Math.min(minKw, p[s.key]);
                    maxKw = Math.max(maxKw, p[s.key]);
                }
            }
            if (maxKw === minKw) maxKw = minKw + 1;
            const Y = kw => PAD.t + (1 - (kw - minKw) / (maxKw - minKw)) * plotH;
            const Ypct = pct => PAD.t + (1 - Math.max(0, Math.min(100, pct)) / 100) * plotH;

            // Gridlines, left kW labels, right % labels
            ctx.font = '11px system-ui, sans-serif';
            ctx.textBaseline = 'middle';
            for (let i = 0; i <= 4; i++) {
                const kw = minKw + ((maxKw - minKw) * i) / 4;
                const y = Y(kw);
                ctx.strokeStyle = 'rgba(139, 148, 158, 0.15)';
                ctx.lineWidth = 1;
                ctx.beginPath(); ctx.moveTo(PAD.l, y); ctx.lineTo(W - PAD.r, y); ctx.stroke();
                ctx.fillStyle = 'rgba(139, 148, 158, 0.8)';
                ctx.textAlign = 'right';
                // Skip a label that would collide with the "0" zero-line label
                if (!(minKw < 0 && Math.abs(y - Y(0)) < 12)) ctx.fillText(kw.toFixed(1), PAD.l - 6, y);
                ctx.textAlign = 'left';
                ctx.fillText(`${Math.round(i * 25)}%`, W - PAD.r + 6, y);
            }
            // Zero line (battery/grid swing negative)
            if (minKw < 0) {
                const zy = Y(0);
                ctx.strokeStyle = 'rgba(139, 148, 158, 0.4)';
                ctx.setLineDash([4, 4]);
                ctx.beginPath(); ctx.moveTo(PAD.l, zy); ctx.lineTo(W - PAD.r, zy); ctx.stroke();
                ctx.setLineDash([]);
                ctx.fillStyle = 'rgba(139, 148, 158, 0.9)';
                ctx.textAlign = 'right';
                ctx.fillText('0', PAD.l - 6, zy);
            }
            // Time axis: ~6 ticks, "4p" (with minutes on windows up to 6h)
            ctx.fillStyle = 'rgba(139, 148, 158, 0.8)';
            ctx.textAlign = 'center';
            ctx.textBaseline = 'top';
            const ticks = Math.min(6, points.length - 1);
            for (let i = 0; i <= ticks; i++) {
                const ts = t0 + (xSpan * i) / ticks;
                const d = new Date(ts * 1000);
                const h = d.getHours();
                const hh = `${h % 12 || 12}`;
                const ap = h >= 12 ? 'p' : 'a';
                const label = xSpan <= 6 * 3600 && d.getMinutes() ? `${hh}:${pad2(d.getMinutes())}${ap}` : `${hh}${ap}`;
                ctx.fillText(label, X(ts), H - PAD.b + 6);
            }

            const drawSeries = (key, color, dashed, yScale, fill) => {
                // Contiguous segments (gaps where the value is null)
                const segments = [];
                let seg = [];
                for (const p of points) {
                    const v = p[key];
                    if (v == null) { if (seg.length > 1) segments.push(seg); seg = []; }
                    else seg.push([X(p.ts), yScale(v)]);
                }
                if (seg.length > 1) segments.push(seg);
                for (const sg of segments) {
                    if (fill) {
                        const zy = Y(0);
                        ctx.fillStyle = color;
                        ctx.globalAlpha = 0.12;
                        ctx.beginPath();
                        ctx.moveTo(sg[0][0], zy);
                        for (const [sx, sy] of sg) ctx.lineTo(sx, sy);
                        ctx.lineTo(sg[sg.length - 1][0], zy);
                        ctx.closePath();
                        ctx.fill();
                        ctx.globalAlpha = 1;
                    }
                    ctx.strokeStyle = color;
                    ctx.lineWidth = 1.8;
                    if (dashed) ctx.setLineDash([5, 4]);
                    ctx.beginPath();
                    ctx.moveTo(sg[0][0], sg[0][1]);
                    for (let i = 1; i < sg.length; i++) ctx.lineTo(sg[i][0], sg[i][1]);
                    ctx.stroke();
                    ctx.setLineDash([]);
                }
            };
            for (const s of SERIES) {
                if (hidden.has(s.key)) continue;
                drawSeries(s.key, s.color, s.axis === 'pct', s.axis === 'pct' ? Ypct : Y, s.axis === 'kw');
            }

            // Hover crosshair + value dots
            if (hover) {
                const hx = X(hover.ts);
                ctx.strokeStyle = 'rgba(139, 148, 158, 0.5)';
                ctx.lineWidth = 1;
                ctx.setLineDash([3, 3]);
                ctx.beginPath(); ctx.moveTo(hx, PAD.t); ctx.lineTo(hx, H - PAD.b); ctx.stroke();
                ctx.setLineDash([]);
                for (const s of SERIES) {
                    if (hidden.has(s.key) || hover[s.key] == null) continue;
                    ctx.fillStyle = s.color;
                    ctx.beginPath();
                    ctx.arc(hx, s.axis === 'pct' ? Ypct(hover[s.key]) : Y(hover[s.key]), 3.5, 0, Math.PI * 2);
                    ctx.fill();
                    ctx.strokeStyle = 'rgba(13, 17, 23, 0.9)';
                    ctx.lineWidth = 1.5;
                    ctx.stroke();
                }
            }
        }

        function updateTooltip() {
            if (!hover) { tooltip.style.display = 'none'; return; }
            const rows = [`<div class="tt-time">${hoverTime(hover.ts)}</div>`];
            for (const s of SERIES) {
                if (hidden.has(s.key) || hover[s.key] == null) continue;
                const v = hover[s.key];
                rows.push(`<div><span class="tt-dot" style="background:${s.color}"></span>${s.label}: <b>${v.toFixed(s.axis === 'pct' ? 0 : 2)} ${s.axis === 'pct' ? '%' : 'kW'}</b></div>`);
            }
            tooltip.innerHTML = rows.join('');
            tooltip.style.display = 'block';
        }

        function leave() {
            hover = null;
            tooltip.style.display = 'none';
            draw();
        }

        function move(e) {
            if (!points || points.length < 2) return;
            const rect = canvas.getBoundingClientRect();
            const x = e.clientX - rect.left;
            const plotW = rect.width - PAD.l - PAD.r;
            if (plotW <= 0 || x < PAD.l || x > rect.width - PAD.r) { leave(); return; }
            const { t0, t1 } = dom();
            const ts = t0 + ((x - PAD.l) / plotW) * ((t1 - t0) || 1);
            let best = null, bestD = Infinity;
            for (const p of points) {
                const d = Math.abs(p.ts - ts);
                if (d < bestD) { bestD = d; best = p; }
            }
            if (best !== hover) { hover = best; draw(); updateTooltip(); }
            // Near the cursor, clamped inside the panel
            const pr = panel.getBoundingClientRect();
            const tipW = tooltip.offsetWidth, tipH = tooltip.offsetHeight;
            let left = e.clientX - pr.left + 14;
            if (left + tipW > pr.width - 4) left = e.clientX - pr.left - tipW - 14;
            let top = e.clientY - pr.top - tipH - 10;
            if (top < 2) top = e.clientY - pr.top + 14;
            tooltip.style.left = `${Math.max(0, left)}px`;
            tooltip.style.top = `${top}px`;
        }

        // Pointer events: mouse hover plus tap/drag on touch screens
        canvas.addEventListener('pointermove', move);
        canvas.addEventListener('pointerdown', move);
        canvas.addEventListener('pointerleave', leave);

        return {
            hidden,
            update(newPoints, newDomain) {
                points = newPoints && newPoints.length ? newPoints : null;
                domain = newDomain || null;
                if (hover && (!points || !points.includes(hover))) hover = null;
                draw();
                updateTooltip();
            },
            toggle(key) {
                if (hidden.has(key)) hidden.delete(key); else hidden.add(key);
                draw();
                updateTooltip();
                return !hidden.has(key);
            },
            redraw: draw,
        };
    }

    window.EnergyTrend = { SERIES, create };
})();
